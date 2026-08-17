from fastapi import APIRouter
from pydantic import BaseModel

from cdp.api.deps import ActorDep, SessionDep
from cdp.proof.service import ProofService

router = APIRouter(tags=["proof"])


class StitchingOut(BaseModel):
    identifiers: int
    # The ratio is taken over these, not over everything: a cart token is a
    # browser session and there are fourteen of them per phone number.
    strong: int
    weak: int
    people: int
    identifiers_per_person: float
    merges: int
    open_reviews: int


class CrossBrandOut(BaseModel):
    buyers: int
    two_or_more: int
    all_three: int
    counted_separately: int
    share: float


class AttributionOut(BaseModel):
    currency: str
    known_amount: str
    total_amount: str
    known_orders: int
    total_orders: int
    share: float


class RefusalsOut(BaseModel):
    delivered: int
    skipped_no_consent: int
    skipped_identifier_risk: int
    risky_identifiers: int


class PointOut(BaseModel):
    week: str
    amount: str
    orders: int


class SliceOut(BaseModel):
    label: str
    amount: str
    count: int


class BandOut(BaseModel):
    label: str
    people: int
    # A string for the same reason the attribution totals are: this figure is
    # meant to be checkable against the store's own reports, and a float would
    # round a riyal total in a way nobody can reproduce.
    amount: str


class ProofOut(BaseModel):
    stitching: StitchingOut
    cross_brand: CrossBrandOut
    attribution: AttributionOut
    refusals: RefusalsOut
    trend: list[PointOut]
    brands: list[SliceOut]
    channels: list[SliceOut]
    loyalty: list[BandOut]


@router.get("/proof", response_model=ProofOut)
async def proof(session: SessionDep, actor: ActorDep) -> ProofOut:
    """What the identity, consent and provenance rules are doing to the data
    that is actually in the database."""
    result = await ProofService(session).collect()
    return ProofOut(
        stitching=StitchingOut(
            identifiers=result.stitching.identifiers,
            strong=result.stitching.strong,
            weak=result.stitching.weak,
            people=result.stitching.people,
            identifiers_per_person=result.stitching.identifiers_per_person,
            merges=result.stitching.merges,
            open_reviews=result.stitching.open_reviews,
        ),
        cross_brand=CrossBrandOut(
            buyers=result.cross_brand.buyers,
            two_or_more=result.cross_brand.two_or_more,
            all_three=result.cross_brand.all_three,
            counted_separately=result.cross_brand.counted_separately,
            share=result.cross_brand.share,
        ),
        attribution=AttributionOut(
            currency=result.attribution.currency,
            # Money crosses the wire as a string. A float here would round a
            # riyal total in a way nobody can reproduce, and this figure is meant
            # to be checkable against the store's own reports.
            known_amount=str(result.attribution.known_amount),
            total_amount=str(result.attribution.total_amount),
            known_orders=result.attribution.known_orders,
            total_orders=result.attribution.total_orders,
            share=result.attribution.share,
        ),
        refusals=RefusalsOut(
            delivered=result.refusals.delivered,
            skipped_no_consent=result.refusals.skipped_no_consent,
            skipped_identifier_risk=result.refusals.skipped_identifier_risk,
            risky_identifiers=result.refusals.risky_identifiers,
        ),
        trend=[
            PointOut(week=p.week, amount=str(p.amount), orders=p.orders) for p in result.trend
        ],
        brands=[
            SliceOut(label=s.label, amount=str(s.amount), count=s.count) for s in result.brands
        ],
        channels=[
            SliceOut(label=s.label, amount=str(s.amount), count=s.count) for s in result.channels
        ],
        loyalty=[
            BandOut(label=b.label, people=b.people, amount=str(b.amount))
            for b in result.loyalty
        ],
    )
