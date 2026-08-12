from fastapi import APIRouter
from pydantic import BaseModel

from cdp.api.deps import ActorDep, SessionDep
from cdp.proof.service import ProofService

router = APIRouter(tags=["proof"])


class StitchingOut(BaseModel):
    identifiers: int
    people: int
    identifiers_per_person: float
    merges: int
    open_reviews: int


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


class ProofOut(BaseModel):
    stitching: StitchingOut
    attribution: AttributionOut
    refusals: RefusalsOut


@router.get("/proof", response_model=ProofOut)
async def proof(session: SessionDep, actor: ActorDep) -> ProofOut:
    """What the identity, consent and provenance rules are doing to the data
    that is actually in the database."""
    result = await ProofService(session).collect()
    return ProofOut(
        stitching=StitchingOut(
            identifiers=result.stitching.identifiers,
            people=result.stitching.people,
            identifiers_per_person=result.stitching.identifiers_per_person,
            merges=result.stitching.merges,
            open_reviews=result.stitching.open_reviews,
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
    )
