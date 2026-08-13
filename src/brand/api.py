"""Sites, the standard they are held to, what was seen, and what follows.

Routed under /brand so the three modules on this database stay legible from
their addresses alone.
"""

import hashlib
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select

from brand.models import (
    COMPLIANCE_CLASSES,
    OBSERVATION_SOURCES,
    SEVERITIES,
    SITE_KINDS,
    Check,
    Finding,
    Observation,
    ObservationImage,
    Site,
    Standard,
)
from brand.service import ComplianceService, applies_to
from sca.api.deps import ActorDep, SessionDep
from sca.models import AuditLog

router = APIRouter(prefix="/brand", tags=["brand"])

# A photograph of a shop window, not a video. Anything larger is a mistake or a
# different kind of file, and refusing it is cheaper than storing it.
MAX_IMAGE_BYTES = 8_000_000


def _one_of(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{field} must be one of {', '.join(allowed)}",
        )
    return value


class SiteIn(BaseModel):
    code: str
    name: str
    brand: str | None = None
    kind: str = "activation"
    city: str | None = None
    opens_on: date | None = None
    closes_on: date | None = None
    contact: str | None = None
    active: bool = True
    notes: str | None = None


class StandardIn(BaseModel):
    code: str
    title: str
    rule: str
    compliance_class: str
    severity: str = "medium"
    brand: str | None = None
    site_kind: str | None = None


class ObservationIn(BaseModel):
    site_code: str
    source: str = "mobile"
    captured_by: str | None = None
    campaign: str | None = None
    note: str | None = None
    captured_at: datetime | None = None


class CheckIn(BaseModel):
    standard_code: str
    passed: bool
    note: str | None = None


class ReviewIn(BaseModel):
    """The whole review of one observation, sent at once.

    One call rather than one per rule, because a reviewer works through a
    display in a single sitting and a half-submitted review would leave the
    coverage count describing something nobody finished.
    """

    checks: list[CheckIn]
    reviewed_by: str | None = None


class QualityIn(BaseModel):
    quality: str
    reason: str | None = None


class CloseIn(BaseModel):
    status: str = "fixed"
    note: str | None = None
    closed_by: str | None = None


# ------------------------------------------------------------------- sites
@router.post("/sites", status_code=status.HTTP_201_CREATED)
async def upsert_site(body: SiteIn, session: SessionDep, actor: ActorDep) -> dict:
    _one_of(body.kind, SITE_KINDS, "kind")
    site = await session.scalar(select(Site).where(Site.code == body.code))
    if site is None:
        site = Site(code=body.code, name=body.name)
        session.add(site)
    for name, value in body.model_dump().items():
        setattr(site, name, value)
    await session.flush()
    return {"id": site.id, "code": site.code, "name": site.name}


@router.get("/sites")
async def estate(session: SessionDep, actor: ActorDep) -> dict:
    """Every site and what is actually known about it.

    Deliberately not a compliance percentage. Coverage comes first: a site with
    nothing against it is far more often one nobody visited than one in good
    order, and a single score cannot tell those apart.
    """
    return (await ComplianceService(session).estate()).as_dict()


# --------------------------------------------------------------- standards
@router.post("/standards", status_code=status.HTTP_201_CREATED)
async def upsert_standard(body: StandardIn, session: SessionDep, actor: ActorDep) -> dict:
    """Write a rule, or supersede one.

    Editing a live rule would rewrite the meaning of every past judgement made
    under it, so a change publishes the next version and retires the current
    one. Findings keep pointing at the version that was actually applied.
    """
    _one_of(body.compliance_class, COMPLIANCE_CLASSES, "compliance_class")
    _one_of(body.severity, SEVERITIES, "severity")

    current = await session.scalar(
        select(Standard)
        .where(Standard.code == body.code, Standard.retired_at.is_(None))
        .order_by(Standard.version.desc())
        .limit(1)
    )
    version = 1
    if current is not None:
        unchanged = (
            current.title == body.title
            and current.rule == body.rule
            and current.compliance_class == body.compliance_class
            and current.severity == body.severity
            and current.brand == body.brand
            and current.site_kind == body.site_kind
        )
        if unchanged:
            return {"code": current.code, "version": current.version, "changed": False}
        current.retired_at = datetime.now(UTC)
        version = current.version + 1

    standard = Standard(version=version, **body.model_dump())
    session.add(standard)
    session.add(
        AuditLog(
            actor=actor, action="brand.standard", entity="brand_standard",
            entity_id=standard.id, meta={"code": body.code, "version": version},
        )
    )
    await session.flush()
    return {"code": standard.code, "version": standard.version, "changed": True}


@router.get("/standards")
async def list_standards(
    session: SessionDep, actor: ActorDep, include_retired: bool = False
) -> list[dict]:
    query = select(Standard).order_by(Standard.code, Standard.version)
    if not include_retired:
        query = query.where(Standard.retired_at.is_(None))
    return [
        {
            "id": s.id, "code": s.code, "version": s.version, "title": s.title,
            "rule": s.rule, "compliance_class": s.compliance_class,
            "severity": s.severity, "brand": s.brand, "site_kind": s.site_kind,
            "retired_at": s.retired_at.isoformat() if s.retired_at else None,
        }
        for s in await session.scalars(query)
    ]


@router.delete("/standards/{code}")
async def retire_standard(code: str, session: SessionDep, actor: ActorDep) -> dict:
    """Retire rather than delete. Past checks named this rule, and removing it
    would leave them judging against nothing."""
    rows = list(
        await session.scalars(
            select(Standard).where(Standard.code == code, Standard.retired_at.is_(None))
        )
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no live standard {code}")
    for row in rows:
        row.retired_at = datetime.now(UTC)
    await session.flush()
    return {"code": code, "retired": len(rows)}


# ------------------------------------------------------------ observations
@router.post("/observations", status_code=status.HTTP_201_CREATED)
async def add_observation(body: ObservationIn, session: SessionDep, actor: ActorDep) -> dict:
    _one_of(body.source, OBSERVATION_SOURCES, "source")
    site = await session.scalar(select(Site).where(Site.code == body.site_code))
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown site {body.site_code}")
    observation = Observation(
        site_id=site.id,
        captured_at=body.captured_at or datetime.now(UTC),
        source=body.source,
        captured_by=body.captured_by,
        campaign=body.campaign,
        note=body.note,
    )
    session.add(observation)
    await session.flush()
    return {"id": observation.id, "site": site.code, "quality": observation.quality}


@router.post("/observations/{observation_id}/images", status_code=status.HTTP_201_CREATED)
async def add_image(
    observation_id: str,
    session: SessionDep,
    actor: ActorDep,
    file: Annotated[UploadFile, File()],
    de_identified: Annotated[bool, Form()] = False,
) -> dict:
    """Store the photograph as sent.

    Content addressed, so submitting the same file twice records it once. The
    same reasoning as supplier attachments: the evidence behind a judgement has
    to be the file that was judged, not a re-encoding of it.
    """
    observation = await session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")

    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty file")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{file.filename} is larger than {MAX_IMAGE_BYTES} bytes",
        )

    digest = hashlib.sha256(content).hexdigest()
    existing = await session.scalar(
        select(ObservationImage).where(
            ObservationImage.observation_id == observation_id,
            ObservationImage.sha256 == digest,
        )
    )
    if existing is not None:
        return {"id": existing.id, "sha256": digest, "stored": False}

    image = ObservationImage(
        observation_id=observation_id,
        filename=file.filename or "photo",
        content_type=file.content_type or "application/octet-stream",
        byte_size=len(content),
        sha256=digest,
        content=content,
        de_identified=de_identified,
    )
    session.add(image)
    await session.flush()
    return {"id": image.id, "sha256": digest, "stored": True, "bytes": len(content)}


@router.get("/images/{image_id}")
async def get_image(image_id: str, session: SessionDep, actor: ActorDep) -> Response:
    image = await session.get(ObservationImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    return Response(content=image.content, media_type=image.content_type)


@router.post("/observations/{observation_id}/quality")
async def set_quality(
    observation_id: str, body: QualityIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Reject an observation nothing can be judged from.

    An unusable photograph produces a request for another one, never a
    compliance statement. This is the gate that stops a dark, oblique frame
    being scored anyway — which is the largest single source of false alarms in
    systems of this kind, because something asked to judge an unreadable image
    will still answer.
    """
    _one_of(body.quality, ("usable", "unusable"), "quality")
    observation = await session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    if body.quality == "unusable" and not (body.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a rejected observation needs a reason — somebody has to be told what to redo",
        )
    observation.quality = body.quality
    observation.quality_reason = body.reason
    await session.flush()
    return {
        "id": observation.id,
        "quality": observation.quality,
        "recapture_requested": observation.quality == "unusable",
    }


@router.get("/observations")
async def list_observations(
    session: SessionDep, actor: ActorDep, pending: bool = False
) -> list[dict]:
    """What has been captured. ``pending`` narrows to the queue that matters:
    usable observations nobody has reviewed yet."""
    query = select(Observation).order_by(Observation.captured_at.desc()).limit(200)
    if pending:
        query = query.where(
            Observation.quality == "usable", Observation.reviewed_at.is_(None)
        )
    rows = list(await session.scalars(query))
    if not rows:
        return []
    sites = {
        s.id: s
        for s in await session.scalars(
            select(Site).where(Site.id.in_([o.site_id for o in rows]))
        )
    }
    images = dict(
        (
            await session.execute(
                select(ObservationImage.observation_id, func.count(ObservationImage.id))
                .where(ObservationImage.observation_id.in_([o.id for o in rows]))
                .group_by(ObservationImage.observation_id)
            )
        ).all()
    )
    checks = dict(
        (
            await session.execute(
                select(Check.observation_id, func.count(Check.id))
                .where(Check.observation_id.in_([o.id for o in rows]))
                .group_by(Check.observation_id)
            )
        ).all()
    )
    out = []
    for o in rows:
        site = sites.get(o.site_id)
        out.append({
            "id": o.id,
            "site": site.name if site else "unknown",
            "site_code": site.code if site else None,
            "site_kind": site.kind if site else None,
            "brand": site.brand if site else None,
            "captured_at": o.captured_at.isoformat(),
            "source": o.source,
            "captured_by": o.captured_by,
            "campaign": o.campaign,
            "note": o.note,
            "quality": o.quality,
            "quality_reason": o.quality_reason,
            "images": int(images.get(o.id, 0)),
            "checks": int(checks.get(o.id, 0)),
            "reviewed_at": o.reviewed_at.isoformat() if o.reviewed_at else None,
        })
    return out


@router.get("/observations/{observation_id}")
async def get_observation(observation_id: str, session: SessionDep, actor: ActorDep) -> dict:
    """One observation, with the rules that apply to its site and whatever has
    already been said about them. This is what a review screen needs."""
    observation = await session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    site = await session.get(Site, observation.site_id)
    standards = [
        s
        for s in await session.scalars(select(Standard).where(Standard.retired_at.is_(None)))
        if site is not None and applies_to(s, site)
    ]
    done = {
        c.standard_id: c
        for c in await session.scalars(
            select(Check).where(Check.observation_id == observation_id)
        )
    }
    images = list(
        await session.scalars(
            select(ObservationImage).where(
                ObservationImage.observation_id == observation_id
            )
        )
    )
    return {
        "id": observation.id,
        "site": {"code": site.code, "name": site.name, "kind": site.kind,
                 "brand": site.brand, "contact": site.contact} if site else None,
        "captured_at": observation.captured_at.isoformat(),
        "source": observation.source,
        "captured_by": observation.captured_by,
        "campaign": observation.campaign,
        "note": observation.note,
        "quality": observation.quality,
        "quality_reason": observation.quality_reason,
        "reviewed_at": observation.reviewed_at.isoformat() if observation.reviewed_at else None,
        "images": [
            {"id": i.id, "filename": i.filename, "bytes": i.byte_size,
             "de_identified": i.de_identified}
            for i in images
        ],
        "standards": [
            {
                "id": s.id, "code": s.code, "version": s.version, "title": s.title,
                "rule": s.rule, "compliance_class": s.compliance_class,
                "severity": s.severity,
                "passed": done[s.id].passed if s.id in done else None,
                "note": done[s.id].note if s.id in done else None,
            }
            for s in standards
        ],
    }


# --------------------------------------------------------------- reviewing
@router.post("/observations/{observation_id}/review")
async def review(
    observation_id: str, body: ReviewIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Apply the standard to what was seen.

    Every rule the reviewer considered is recorded, passed or failed, because
    the passes are what make coverage measurable — without them a site with no
    findings is indistinguishable from a site nobody looked at. Failures raise a
    finding; a rule failing again on a later observation does not raise a second
    one while the first is still open.
    """
    observation = await session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    if observation.quality != "usable":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this observation was rejected as unusable — nothing can be judged from it",
        )

    codes = [c.standard_code for c in body.checks]
    live = {
        s.code: s
        for s in await session.scalars(
            select(Standard).where(Standard.code.in_(codes), Standard.retired_at.is_(None))
        )
    }
    unknown = sorted({c for c in codes if c not in live})
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no live standard: {', '.join(unknown)}",
        )

    existing = {
        c.standard_id: c
        for c in await session.scalars(
            select(Check).where(Check.observation_id == observation_id)
        )
    }
    open_findings = {
        f.standard_id: f
        for f in await session.scalars(
            select(Finding).where(
                Finding.site_id == observation.site_id, Finding.status == "open"
            )
        )
    }

    raised, passed, failed = [], 0, 0
    for item in body.checks:
        standard = live[item.standard_code]
        check = existing.get(standard.id)
        if check is None:
            check = Check(
                observation_id=observation_id, standard_id=standard.id, passed=item.passed
            )
            session.add(check)
            existing[standard.id] = check
        check.passed = item.passed
        check.note = item.note
        check.judged_by_kind = "human"
        check.judged_by = body.reviewed_by or actor
        await session.flush()

        if item.passed:
            passed += 1
            continue
        failed += 1
        if standard.id in open_findings:
            # Already known and still not fixed. A second finding would make the
            # queue longer without telling anybody anything new.
            continue
        finding = Finding(
            check_id=check.id,
            site_id=observation.site_id,
            standard_id=standard.id,
            severity=standard.severity,
            detail=item.note or standard.title,
        )
        session.add(finding)
        raised.append(standard.code)

    observation.reviewed_at = datetime.now(UTC)
    observation.reviewed_by = body.reviewed_by or actor
    session.add(
        AuditLog(
            actor=actor, action="brand.review", entity="brand_observation",
            entity_id=observation_id,
            meta={"passed": passed, "failed": failed, "raised": raised},
        )
    )
    await session.flush()
    return {
        "observation": observation_id,
        "checked": passed + failed,
        "passed": passed,
        "failed": failed,
        "findings_raised": raised,
        "already_open": sorted(
            {live[c.standard_code].code for c in body.checks
             if not c.passed and live[c.standard_code].id in open_findings}
        ),
    }


@router.get("/findings")
async def list_findings(
    session: SessionDep, actor: ActorDep, status_filter: str = "open"
) -> list[dict]:
    query = select(Finding).order_by(Finding.created_at.desc()).limit(200)
    if status_filter != "all":
        query = query.where(Finding.status == status_filter)
    rows = list(await session.scalars(query))
    if not rows:
        return []
    sites = {
        s.id: s
        for s in await session.scalars(
            select(Site).where(Site.id.in_([f.site_id for f in rows]))
        )
    }
    standards = {
        s.id: s
        for s in await session.scalars(
            select(Standard).where(Standard.id.in_([f.standard_id for f in rows]))
        )
    }
    checks = {
        c.id: c
        for c in await session.scalars(
            select(Check).where(Check.id.in_([f.check_id for f in rows]))
        )
    }
    images = dict(
        (
            await session.execute(
                select(ObservationImage.observation_id, func.min(ObservationImage.id))
                .group_by(ObservationImage.observation_id)
            )
        ).all()
    )
    out = []
    for f in rows:
        site = sites.get(f.site_id)
        standard = standards.get(f.standard_id)
        check = checks.get(f.check_id)
        out.append({
            "id": f.id,
            "site": site.name if site else "unknown",
            "site_code": site.code if site else None,
            "contact": site.contact if site else None,
            "closes_on": site.closes_on.isoformat() if site and site.closes_on else None,
            "standard": standard.code if standard else None,
            "standard_title": standard.title if standard else None,
            # The version applied, so a rule rewritten since cannot make a past
            # finding look like it was judged against today's wording.
            "standard_version": standard.version if standard else None,
            # The rule may have been retired or rewritten since. The finding
            # stands — retiring a rule does not straighten the shelf — but it is
            # marked, so a site reading "1 of 1 checked · 2 open" is explained
            # rather than looking like a miscount.
            "standard_retired": bool(standard and standard.retired_at),
            "compliance_class": standard.compliance_class if standard else None,
            "severity": f.severity,
            "detail": f.detail,
            "status": f.status,
            "raised_at": f.created_at.isoformat(),
            "observation_id": check.observation_id if check else None,
            "image_id": images.get(check.observation_id) if check else None,
            "closed_at": f.closed_at.isoformat() if f.closed_at else None,
            "closed_note": f.closed_note,
        })
    return out


@router.post("/findings/{finding_id}/close")
async def close_finding(
    finding_id: str, body: CloseIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Fixed, or dismissed as not a real failure.

    A dismissal does not delete the check that raised it. The observation still
    says what it said, and the dismissal is a second judgement recorded beside
    the first — which is the only way to notice later that one rule is dismissed
    every time it fires, and is therefore a rule that needs rewriting.
    """
    _one_of(body.status, ("fixed", "dismissed"), "status")
    finding = await session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown finding")
    finding.status = body.status
    finding.closed_at = datetime.now(UTC)
    finding.closed_by = body.closed_by or actor
    finding.closed_note = body.note
    session.add(
        AuditLog(
            actor=actor, action=f"brand.finding.{body.status}", entity="brand_finding",
            entity_id=finding_id, meta={"note": body.note},
        )
    )
    await session.flush()
    return {"id": finding.id, "status": finding.status}


@router.get("/labels")
async def labels(session: SessionDep, actor: ActorDep, limit: int = 500) -> list[dict]:
    """Every human judgement as an image, a rule and a verdict.

    Exposed as data rather than kept inside this module, because the point of
    recording adjudications is that a decision to train something later does not
    have to start by commissioning a labelling campaign.
    """
    return await ComplianceService(session).labels(limit=limit)
