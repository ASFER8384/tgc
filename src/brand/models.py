"""What is being presented, what it is supposed to look like, and what was seen.

The shape of this module follows one decision: a judgement about a display is
worthless without the evidence it was made from and the rule it was made
against. So nothing here records a verdict alone. A check names the standard it
applied, the observation it read, and the version of the rule as it stood on the
day — because a standard revised in March must not silently re-judge a photograph
taken in February.

Two absences are deliberate.

There is no compliance score. Absence of a finding is not evidence of
compliance: it far more often means nobody looked. Coverage is therefore
recorded explicitly — every rule considered is written down, whether it passed
or failed — and a site with no checks reads as unchecked rather than as clean.

Nothing here performs de-identification. Any photograph of a retail space
contains customers and staff, and the honest position is that this module stores
what it is given and records what the person capturing it asserted, rather than
implying a protection it does not apply.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy import UniqueConstraint as Unique
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, TimestampMixin, UTCDateTime, new_id

# Where presentation happens. The kinds differ in who controls the space, which
# governs what may be observed there and how: a partner counter cannot be
# photographed on our say-so, and a digital channel has nothing to photograph.
SITE_KINDS = ("activation", "store", "partner", "digital")

# The five verification classes. Kept apart because each is a different question
# with a different error profile: whether a thing is present is nearly
# unambiguous, whether a display is tidy is contested, and a single threshold
# over both would be wrong for one of them.
COMPLIANCE_CLASSES = ("presence", "configuration", "state", "text", "identity")

SEVERITIES = ("high", "medium", "low")

# How the observation arrived. Carried because confidence in a judgement cannot
# exceed confidence in what it was made from — a deliberate photograph and an
# oblique frame from a security camera are not the same evidence.
OBSERVATION_SOURCES = ("mobile", "fixed", "digital", "desk")

FINDING_STATUSES = ("open", "fixed", "dismissed")


class Site(Base, TimestampMixin):
    """A place the brand is presented: an activation, a store, a partner counter
    or a digital channel.

    Activations open and close, which is why the dates are here rather than
    implied. A lapse found after an activation has closed cannot be corrected,
    so knowing the window is part of knowing whether a finding is still worth
    anybody's time.
    """

    __tablename__ = "brand_sites"
    __table_args__ = (Unique("code", name="uq_brand_sites_code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Null means the site carries more than one brand, which a mall activation
    # often does.
    brand: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="activation")
    city: Mapped[str | None] = mapped_column(String(120))
    opens_on: Mapped[date | None] = mapped_column(Date)
    closes_on: Mapped[date | None] = mapped_column(Date)
    # Who to tell. A finding that reaches nobody is a finding that was not made.
    contact: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(String(500))


class Standard(Base, TimestampMixin):
    """One rule from the brand standard, in a form somebody can check.

    Versioned rather than edited. A finding points at the exact version that was
    applied, so revising a rule cannot retroactively change what a past
    judgement meant — and so the question "what did the standard say in March"
    has an answer. Superseding a rule retires the old row instead of mutating
    it.

    ``rule`` is written for the person doing the checking, not for a model. The
    hardest part of this whole subject is that brand standards are qualitative,
    and a rule nobody can apply consistently by hand will not become checkable
    by machine merely by being stored.
    """

    __tablename__ = "brand_standards"
    __table_args__ = (Unique("code", "version", name="uq_brand_standards_code_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    compliance_class: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    # Null applies to every brand. Aleena's folding rule is not Rawash's.
    brand: Mapped[str | None] = mapped_column(String(64))
    # Null applies to every kind of site. Some rules only make sense in a shop.
    site_kind: Mapped[str | None] = mapped_column(String(16))
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Observation(Base, TimestampMixin):
    """One look at one site, at one time.

    An observation is not a verdict. It is the evidence a verdict may later be
    made from, and it can be rejected before any judging happens: a photograph
    too dark or too oblique to read supports no conclusion, and the honest
    response is to ask for another rather than to judge it anyway. That gate is
    the single largest source of false confidence in systems like this, because
    something asked to judge an unreadable image will still return an answer.
    """

    __tablename__ = "brand_observations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_sites.id"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="mobile")
    captured_by: Mapped[str | None] = mapped_column(String(120))
    campaign: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    # usable | unusable. Unusable carries its reason and produces a recapture
    # request rather than any compliance statement at all.
    quality: Mapped[str] = mapped_column(String(16), nullable=False, default="usable")
    quality_reason: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))


class ObservationImage(Base, TimestampMixin):
    """The photograph itself, kept byte for byte.

    Content addressed, so the same image submitted twice is stored once, and so
    a finding's evidence can be shown to be the file that was actually judged
    rather than a copy of it.

    ``de_identified`` records an assertion, not a guarantee. Nothing in this
    module blurs anybody. The flag exists so that the day a capture app does
    perform de-identification, images from before that day are distinguishable
    from images after it — which they would not be if the column were added
    later and backfilled with a hopeful default.
    """

    __tablename__ = "brand_observation_images"
    __table_args__ = (
        Unique("observation_id", "sha256", name="uq_brand_observation_images_sha"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_observations.id"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    de_identified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Check(Base, TimestampMixin):
    """One rule applied to one observation, and what the reviewer concluded.

    This is the table that makes coverage real. Without it the only record is of
    failures, and a site with nothing against it is indistinguishable from a
    site nobody visited — which is the difference between "compliant" and
    "unknown", and the most common way a compliance report flatters itself.

    It is also the labelled example the paper's architecture depends on: an
    image, a rule, and a human verdict. Recording it costs nothing at the moment
    a person is reviewing anyway, and it is the only route to a training set
    that does not require commissioning one.
    """

    __tablename__ = "brand_checks"
    __table_args__ = (
        Unique("observation_id", "standard_id", name="uq_brand_checks_observation_standard"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_observations.id"), nullable=False, index=True
    )
    # The exact version applied, never the rule code. A rule rewritten next
    # season must not change what this check meant.
    standard_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_standards.id"), nullable=False, index=True
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # human for now. The column exists so that when something automated starts
    # producing checks, the two are never averaged together by accident.
    judged_by_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="human")
    judged_by: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))


class Finding(Base, TimestampMixin):
    """A failed check that somebody now has to do something about.

    Separate from the check for the same reason a supplier issue is separate
    from the message that revealed it: the check is evidence and never changes,
    the finding is work and has a life. Dismissing a finding does not erase the
    check that raised it — the observation still says what it said, and a
    dismissal is a second judgement recorded beside the first rather than
    instead of it.
    """

    __tablename__ = "brand_findings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    check_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_checks.id"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_sites.id"), nullable=False, index=True
    )
    standard_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("brand_standards.id"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    detail: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    # Who was told, and when — the gap between raising and telling is the
    # correction latency this whole exercise exists to shorten.
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_by: Mapped[str | None] = mapped_column(String(120))
    closed_note: Mapped[str | None] = mapped_column(String(500))


__all__ = [
    "COMPLIANCE_CLASSES",
    "FINDING_STATUSES",
    "OBSERVATION_SOURCES",
    "SEVERITIES",
    "SITE_KINDS",
    "Check",
    "Finding",
    "Observation",
    "ObservationImage",
    "Site",
    "Standard",
]
