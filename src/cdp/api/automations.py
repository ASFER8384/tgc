from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from cdp.api.deps import ActorDep, SessionDep
from cdp.models import STEP_KINDS, TRIGGERS, AuditLog, Automation, Segment

router = APIRouter(prefix="/automations", tags=["automations"])

# The channels a step may name. Both are addressed sends, so both are subject to
# the consent and provenance gates when the step actually runs.
CHANNELS = ("whatsapp", "email")


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trigger: dict = Field(examples=[{"type": "winback", "days": 42}])
    steps: list[dict] = Field(default_factory=list)
    stop_on_purchase: bool = True
    reentry_days: int = 0
    active: bool = False


class AutomationOut(BaseModel):
    id: str
    name: str
    trigger: dict
    steps: list[dict]
    stop_on_purchase: bool
    reentry_days: int
    active: bool
    updated_at: datetime | None = None


def _validate(body: AutomationIn, segment_keys: set[str]) -> list[str]:
    """Everything wrong with the flow, not the first thing wrong with it.

    Returned as a list because a builder that reports one problem per save makes
    the merchant play twenty questions with the form.
    """
    problems: list[str] = []
    kind = body.trigger.get("type")
    if kind not in TRIGGERS:
        problems.append(f"unknown trigger: {kind}")

    if kind == "segment_entered" or kind == "scheduled":
        key = body.trigger.get("segment_key")
        if not key:
            problems.append("choose which audience this watches")
        elif key not in segment_keys:
            problems.append(f"unknown audience: {key}")

    if kind == "winback" and int(body.trigger.get("days") or 0) <= 0:
        problems.append("winback needs a number of days above zero")

    if kind == "scheduled":
        hour, minute = body.trigger.get("hour"), body.trigger.get("minute")
        if not (isinstance(hour, int) and 0 <= hour <= 23):
            problems.append("schedule needs an hour between 0 and 23")
        if not (isinstance(minute, int) and 0 <= minute <= 59):
            problems.append("schedule needs a minute between 0 and 59")

    if not body.steps:
        problems.append("add at least one step")

    for i, step in enumerate(body.steps, start=1):
        skind = step.get("kind")
        if skind not in STEP_KINDS:
            problems.append(f"step {i}: unknown kind {skind}")
            continue
        if skind == "message":
            if step.get("channel") not in CHANNELS:
                problems.append(f"step {i}: choose a channel")
            if not (step.get("body") or "").strip():
                problems.append(f"step {i}: the message is empty")
        if skind == "wait" and int(step.get("minutes") or 0) <= 0:
            problems.append(f"step {i}: a wait needs to be longer than zero")

    return problems


def _out(row: Automation) -> AutomationOut:
    return AutomationOut(
        id=row.id,
        name=row.name,
        trigger=row.trigger,
        steps=row.steps,
        stop_on_purchase=row.stop_on_purchase,
        reentry_days=row.reentry_days,
        active=row.active,
        updated_at=getattr(row, "created_at", None),
    )


@router.get("", response_model=list[AutomationOut])
async def list_automations(session: SessionDep, actor: ActorDep) -> list[AutomationOut]:
    rows = await session.scalars(select(Automation).order_by(Automation.name))
    return [_out(r) for r in rows]


@router.post("", response_model=AutomationOut, status_code=status.HTTP_201_CREATED)
async def save_automation(
    body: AutomationIn, session: SessionDep, actor: ActorDep, id: str | None = None
) -> AutomationOut:
    keys = set(await session.scalars(select(Segment.key)))
    problems = _validate(body, keys)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problems)

    row = await session.get(Automation, id) if id else None
    if row is None:
        row = Automation(name=body.name)
        session.add(row)
    row.name = body.name
    row.trigger = body.trigger
    row.steps = body.steps
    row.stop_on_purchase = body.stop_on_purchase
    row.reentry_days = body.reentry_days
    row.active = body.active
    await session.flush()

    session.add(
        AuditLog(
            actor=actor,
            action="automation.saved",
            entity="automation",
            entity_id=row.id,
            meta={"name": row.name, "trigger": row.trigger.get("type"), "active": row.active},
        )
    )
    await session.flush()
    return _out(row)


@router.post("/{automation_id}/active", response_model=AutomationOut)
async def set_active(
    automation_id: str, active: bool, session: SessionDep, actor: ActorDep
) -> AutomationOut:
    row = await session.get(Automation, automation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown automation")
    row.active = active
    session.add(
        AuditLog(
            actor=actor,
            # Turning a flow on is the moment it can reach a customer, so it is
            # audited as its own action rather than folded into an edit.
            action="automation.activated" if active else "automation.paused",
            entity="automation",
            entity_id=row.id,
            meta={"name": row.name},
        )
    )
    await session.flush()
    return _out(row)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(automation_id: str, session: SessionDep, actor: ActorDep) -> None:
    row = await session.get(Automation, automation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown automation")
    name = row.name
    await session.delete(row)
    session.add(
        AuditLog(
            actor=actor,
            action="automation.deleted",
            entity="automation",
            entity_id=automation_id,
            meta={"name": name},
        )
    )
    await session.flush()
