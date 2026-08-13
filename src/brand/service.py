"""Reading a compliance record honestly.

The temptation in this subject is a percentage. One number per site, green above
eighty, and a slide that says the estate is 94% compliant. Every part of that is
wrong on this data: it counts rules nobody applied as passes, it averages a
missing price label with an untidy shelf, and it turns "we have not looked since
March" into a good score.

So the unit here is coverage before conformance. A site reports how many of its
applicable rules were actually checked at its last observation, how many of
those passed, and when anybody last looked. A site nobody has visited reads as
unchecked, which is a different sentence from compliant and is usually the more
useful one.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brand.models import Check, Finding, Observation, ObservationImage, Site, Standard


def applies_to(standard: Standard, site: Site) -> bool:
    """Whether a rule is one this site can be judged against.

    A rule with no brand applies to every brand, and one with no site kind
    applies to every kind. Narrowing is opt-in, so a new rule covers the estate
    until somebody says otherwise — the opposite default would let a rule be
    written and quietly apply to nothing.
    """
    if standard.retired_at is not None:
        return False
    if standard.brand and site.brand and standard.brand != site.brand:
        return False
    return not (standard.site_kind and standard.site_kind != site.kind)


@dataclass
class SiteState:
    """What is actually known about one site, including what is not known."""

    id: str
    code: str
    name: str
    brand: str | None
    kind: str
    city: str | None
    contact: str | None
    # The window an activation is open for. A finding raised after it closes
    # cannot be corrected, which changes what the finding is worth.
    opens_on: str | None = None
    closes_on: str | None = None
    open_now: bool | None = None

    rules_applicable: int = 0
    # From the most recent usable observation only. Aggregating every check ever
    # made would let a rule passed in March cover for the same rule failing in
    # August.
    rules_checked: int = 0
    rules_passed: int = 0
    findings_open: int = 0
    findings_high: int = 0
    # Open findings whose rule has since been retired or narrowed away from this
    # site. Counted apart from the rest rather than folded in, because otherwise
    # a site reads "1 of 1 checked" beside "2 open" and looks like an arithmetic
    # error. The shelf is still wrong; the rule that caught it is simply no
    # longer one this site is held to, and both of those are true at once.
    findings_retired_rule: int = 0
    last_observed_at: str | None = None
    last_observation_id: str | None = None
    # Said in words, because this is the sentence people get wrong.
    reads_as: str = "never observed"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Estate:
    """The whole picture, with the unchecked part of it visible."""

    sites: list[dict] = field(default_factory=list)
    sites_total: int = 0
    sites_never_observed: int = 0
    sites_stale: int = 0
    findings_open: int = 0
    findings_high: int = 0
    findings_retired_rule: int = 0
    standards_active: int = 0
    # Days after which an observation stops meaning anything about now. Reported
    # rather than hidden, so the reader can disagree with it.
    stale_after_days: int = 14

    def as_dict(self) -> dict:
        return asdict(self)


class ComplianceService:
    def __init__(self, session: AsyncSession, *, stale_after_days: int = 14):
        self.session = session
        self.stale_after_days = stale_after_days

    async def estate(self, *, now: datetime | None = None) -> Estate:
        now = now or datetime.now(UTC)
        sites = list(await self.session.scalars(select(Site).where(Site.active.is_(True))))
        standards = [
            s
            for s in await self.session.scalars(select(Standard))
            if s.retired_at is None
        ]

        # Open findings, fetched rather than counted in SQL, because whether one
        # still counts against a site depends on whether its rule is still one
        # that applies there — which is per-site work the database cannot do in
        # a GROUP BY.
        open_findings: dict[str, list[Finding]] = {}
        for finding in await self.session.scalars(
            select(Finding).where(Finding.status == "open")
        ):
            open_findings.setdefault(finding.site_id, []).append(finding)

        out = Estate(
            sites_total=len(sites),
            standards_active=len(standards),
            stale_after_days=self.stale_after_days,
        )
        for site in sites:
            state = await self._site_state(
                site, standards, now, open_findings.get(site.id, [])
            )
            out.sites.append(state.as_dict())
            out.findings_open += state.findings_open
            out.findings_high += state.findings_high
            out.findings_retired_rule += state.findings_retired_rule
            if state.last_observed_at is None:
                out.sites_never_observed += 1
            elif state.reads_as == "stale":
                out.sites_stale += 1

        # Worst first: unchecked sites above clean ones, because an unknown site
        # is the one most likely to be wrong and nobody is looking at it.
        order = {
            "never observed": 0, "not reviewed": 1, "stale": 2,
            "failing": 3, "partly checked": 4, "checked": 5,
        }
        out.sites.sort(key=lambda s: (order.get(s["reads_as"], 9), -s["findings_high"], s["code"]))
        return out

    async def _site_state(
        self,
        site: Site,
        standards: list[Standard],
        now: datetime,
        open_findings: list[Finding],
    ) -> SiteState:
        applicable = [s for s in standards if applies_to(s, site)]
        live = {s.id for s in applicable}
        state = SiteState(
            id=site.id,
            code=site.code,
            name=site.name,
            brand=site.brand,
            kind=site.kind,
            city=site.city,
            contact=site.contact,
            opens_on=site.opens_on.isoformat() if site.opens_on else None,
            closes_on=site.closes_on.isoformat() if site.closes_on else None,
            rules_applicable=len(applicable),
        )
        current = [f for f in open_findings if f.standard_id in live]
        state.findings_open = len(current)
        state.findings_high = sum(1 for f in current if f.severity == "high")
        state.findings_retired_rule = len(open_findings) - len(current)

        today = now.date()
        if site.opens_on or site.closes_on:
            state.open_now = (site.opens_on is None or site.opens_on <= today) and (
                site.closes_on is None or site.closes_on >= today
            )

        # The most recent observation anybody could judge from. An unusable one
        # is not "the last time we looked" — nothing was learned from it.
        latest = await self.session.scalar(
            select(Observation)
            .where(Observation.site_id == site.id, Observation.quality == "usable")
            .order_by(Observation.captured_at.desc())
            .limit(1)
        )
        if latest is None:
            return state

        state.last_observed_at = latest.captured_at.isoformat()
        state.last_observation_id = latest.id
        # Only checks against rules that are still in the standard and still
        # apply here. A rule retired since the review was done is no longer
        # something this site can be judged on, and counting it would report
        # more coverage than there are rules to cover — literally "2 of 1
        # checked", which is what retiring a rule used to produce.
        checks = [
            c
            for c in await self.session.scalars(
                select(Check).where(Check.observation_id == latest.id)
            )
            if c.standard_id in live
        ]
        state.rules_checked = len(checks)
        state.rules_passed = sum(1 for c in checks if c.passed)

        age_days = (now - latest.captured_at).total_seconds() / 86400
        if not checks:
            # Somebody photographed it and nobody applied a rule to it. That is
            # a queue problem rather than a coverage problem, and it is the one
            # state that a person can clear today without visiting anywhere.
            state.reads_as = "not reviewed"
        elif age_days > self.stale_after_days:
            state.reads_as = "stale"
        elif state.rules_passed < state.rules_checked:
            state.reads_as = "failing"
        elif state.rules_checked < state.rules_applicable:
            # Everything looked at passed, but not everything was looked at.
            # Calling this compliant would be counting the rules nobody applied.
            state.reads_as = "partly checked"
        else:
            state.reads_as = "checked"
        return state

    async def labels(self, *, limit: int = 500) -> list[dict]:
        """Every human judgement, as an image and a rule and a verdict.

        This is the training set, accumulated as a by-product of somebody
        reviewing rather than as a labelling project. It is exposed as data
        rather than kept internal precisely so that the decision to train
        something later does not depend on this module still existing.
        """
        rows = list(
            await self.session.scalars(
                select(Check).order_by(Check.created_at.desc()).limit(limit)
            )
        )
        if not rows:
            return []
        observations = {
            o.id: o
            for o in await self.session.scalars(
                select(Observation).where(
                    Observation.id.in_([c.observation_id for c in rows])
                )
            )
        }
        standards = {
            s.id: s
            for s in await self.session.scalars(
                select(Standard).where(Standard.id.in_([c.standard_id for c in rows]))
            )
        }
        images = dict(
            (
                await self.session.execute(
                    select(ObservationImage.observation_id, func.count(ObservationImage.id))
                    .where(ObservationImage.observation_id.in_(list(observations)))
                    .group_by(ObservationImage.observation_id)
                )
            ).all()
        )
        out = []
        for check in rows:
            observation = observations.get(check.observation_id)
            standard = standards.get(check.standard_id)
            if observation is None or standard is None:
                continue
            out.append(
                {
                    "observation_id": observation.id,
                    "site_id": observation.site_id,
                    "captured_at": observation.captured_at.isoformat(),
                    "source": observation.source,
                    "images": int(images.get(observation.id, 0)),
                    "standard": standard.code,
                    "standard_version": standard.version,
                    "compliance_class": standard.compliance_class,
                    "label": "compliant" if check.passed else "violation",
                    "judged_by_kind": check.judged_by_kind,
                    "judged_by": check.judged_by,
                    "note": check.note,
                }
            )
        return out
