from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, exists, or_, select, true

from cdp.consent.service import latest_consent_subquery
from cdp.models import BRANDS, PURPOSES, Person, PersonBrandStat, ProfileTraits

# The grant that lets one brand read another brand's behaviour. Named here
# because the compiler, not the campaign, decides when it is required.
CROSS_BRAND_PURPOSE = "cross_brand_profiling"


class SegmentDefinitionError(ValueError):
    pass


# Only these traits are addressable. A whitelist, not getattr on a string from the
# request body — otherwise a segment definition is an arbitrary-column read.
TRAIT_COLUMNS = {
    "ltv": ProfileTraits.ltv,
    "aov": ProfileTraits.aov,
    "order_count": ProfileTraits.order_count,
    "recency_days": ProfileTraits.recency_days,
    "brands_purchased": ProfileTraits.brands_purchased,
    "event_count": ProfileTraits.event_count,
    "rfm": ProfileTraits.rfm,
    "preferred_language": Person.preferred_language,
    "preferred_channel": Person.preferred_channel,
}

NUMERIC_TRAITS = {"ltv", "aov", "order_count", "recency_days", "brands_purchased", "event_count"}

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "not_null")


def _condition(node: dict[str, Any]):
    if "brand_purchased" in node:
        brand = str(node["brand_purchased"]).lower()
        return exists(
            select(PersonBrandStat.person_id).where(
                PersonBrandStat.person_id == Person.id, PersonBrandStat.brand == brand
            )
        )
    if "brand_not_purchased" in node:
        brand = str(node["brand_not_purchased"]).lower()
        return ~exists(
            select(PersonBrandStat.person_id).where(
                PersonBrandStat.person_id == Person.id, PersonBrandStat.brand == brand
            )
        )

    name = node.get("trait")
    if name not in TRAIT_COLUMNS:
        raise SegmentDefinitionError(f"unknown trait: {name!r}")
    column = TRAIT_COLUMNS[name]
    op = node.get("op", "eq")
    if op not in OPS:
        raise SegmentDefinitionError(f"unknown operator: {op!r}")

    if op == "is_null":
        return column.is_(None)
    if op == "not_null":
        return column.is_not(None)

    value = node.get("value")
    if op == "in":
        if not isinstance(value, list) or not value:
            raise SegmentDefinitionError("'in' requires a non-empty list")
        return column.in_(value)

    if name in NUMERIC_TRAITS:
        try:
            value = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - surfaced as a definition error
            raise SegmentDefinitionError(f"{name} needs a number, got {value!r}") from exc

    return {
        "eq": column == value,
        "ne": column != value,
        "gt": column > value,
        "gte": column >= value,
        "lt": column < value,
        "lte": column <= value,
    }[op]


def _clause(definition: dict[str, Any]):
    if not isinstance(definition, dict):
        raise SegmentDefinitionError("definition must be an object")

    if "all" in definition:
        parts = [_clause(n) if _is_group(n) else _condition(n) for n in definition["all"]]
        return and_(true(), *parts)
    if "any" in definition:
        parts = [_clause(n) if _is_group(n) else _condition(n) for n in definition["any"]]
        if not parts:
            raise SegmentDefinitionError("'any' requires at least one condition")
        return or_(*parts)
    if "none" in definition:
        parts = [_clause(n) if _is_group(n) else _condition(n) for n in definition["none"]]
        return and_(true(), *[~p for p in parts])
    return _condition(definition)


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and bool({"all", "any", "none"} & set(node))


def referenced_brands(definition: Any) -> set[str]:
    """Every brand whose behaviour a definition reads.

    ``brand_not_purchased`` counts as much as ``brand_purchased``. Knowing that
    she has *never* bought Rawash is still a fact derived from Rawash's data,
    and the whole cross-brand upsell rests on it — treating the negative as
    harmless would leave the most common cross-brand audience ungated.
    """
    found: set[str] = set()
    if isinstance(definition, list):
        for node in definition:
            found |= referenced_brands(node)
        return found
    if not isinstance(definition, dict):
        return found
    for key in ("brand_purchased", "brand_not_purchased"):
        if key in definition:
            found.add(str(definition[key]).lower())
    for key in ("all", "any", "none"):
        if key in definition:
            found |= referenced_brands(definition[key])
    return found


def compile_segment(
    definition: dict[str, Any], required_consent: str | None, *, brand: str | None = None
) -> Select:
    """Compile a stored definition into a person-id query, for one asking brand.

    Three invariants are enforced here rather than left to callers:

    1. merged-away persons are excluded, so a merge can never double-count a
       customer in an audience;
    2. when the segment declares a consent purpose, the gate is part of the WHERE
       clause, scoped to the asking brand. There is no code path that produces
       the member list without it, so "we forgot to filter opt-outs" is not an
       available mistake;
    3. when the definition reads another brand's behaviour, a second gate
       requires that brand-crossing to have been agreed to as well.

    The gates are conditions in the query, not a filter applied to its results.
    Someone who did not agree is never selected, so the audience containing her
    is never built — which is a stronger property than removing her afterwards,
    and the one the whole design exists to provide.
    """
    if required_consent is not None and required_consent not in PURPOSES:
        raise SegmentDefinitionError(f"unknown consent purpose: {required_consent!r}")
    if brand is not None and brand not in BRANDS:
        raise SegmentDefinitionError(f"unknown brand: {brand!r}")

    crossed = referenced_brands(definition) - ({brand} if brand else set())

    # Refusing here rather than returning nobody. An unscoped request that reads
    # brand data has no answer, right or empty: without knowing who is asking,
    # there is no consent to evaluate. An empty list would read as "no matching
    # customers" when the truth is "this question cannot be asked".
    if crossed and brand is None:
        raise SegmentDefinitionError(
            "a definition that reads brand behaviour must name the brand it is for: "
            f"referenced {sorted(crossed)}"
        )
    if required_consent is not None and brand is None:
        raise SegmentDefinitionError(
            f"consent purpose {required_consent!r} is granted per brand, so the "
            "asking brand must be named"
        )

    query = (
        select(Person.id)
        .join(ProfileTraits, ProfileTraits.person_id == Person.id)
        .where(Person.merged_into_id.is_(None))
        # A segment is an audience — something gets sent to it. The shop counter
        # has the purchase history of a whole till and would qualify for every
        # high-value segment there is, so it is excluded here rather than left to
        # be noticed when a broadcast has nowhere to go.
        .where(Person.synthetic.is_(False))
        .where(_clause(definition))
    )

    if required_consent is not None:
        query = query.where(latest_consent_subquery(required_consent, Person.id, brand).is_(True))

    if crossed:
        query = query.where(
            latest_consent_subquery(CROSS_BRAND_PURPOSE, Person.id, brand).is_(True)
        )

    return query
