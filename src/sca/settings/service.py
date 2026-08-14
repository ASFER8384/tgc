"""Reading and writing the settings a person may change.

The environment is the default and the database is the override, in that order,
resolved per request. Per request rather than cached because the alternative is
a cache that has to be invalidated across however many workers are running, and
the cost being avoided is one indexed read of a table with fewer than a dozen
rows. A buyer who raises the approval threshold and immediately sees an order
still blocked by the old one would not report it as a caching bug; they would
report that the page does not work.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.models import AppSetting, AuditLog
from sca.settings.knobs import KNOBS, KNOBS_BY_KEY, SettingError, cross_check


async def overrides(session: AsyncSession) -> dict[str, object]:
    """Only what somebody has actually changed, parsed.

    A row whose knob no longer exists is skipped rather than raised on: a
    setting can be removed from the registry in a release that runs against a
    database still holding the row, and refusing to start would make deleting a
    knob a migration.

    A row that no longer parses — bounds tightened under a value already saved —
    is skipped for the same reason, and the environment default takes over. The
    console shows the stored value beside the reason it is being ignored, so the
    silence is only in the resolver.
    """
    out: dict[str, object] = {}
    for row in await session.scalars(select(AppSetting)):
        knob = KNOBS_BY_KEY.get(row.key)
        if knob is None:
            continue
        try:
            out[knob.key] = knob.parse(row.value)
        except SettingError:
            continue
    return out


async def effective(session: AsyncSession) -> Settings:
    """The settings this request should actually run under."""
    env = get_settings()
    applied = await overrides(session)
    return env.model_copy(update=applied) if applied else env


async def save(
    session: AsyncSession, values: dict[str, object], *, actor: str
) -> list[dict[str, str]]:
    """Apply a set of changes, or none of them.

    Validated whole before anything is written, including the rules that span
    two settings — half-applying a pair that only makes sense together is how a
    validation error turns into a policy nobody chose.
    """
    unknown = sorted(set(values) - set(KNOBS_BY_KEY))
    if unknown:
        raise SettingError(unknown[0], f"not a setting anyone may change: {', '.join(unknown)}")

    parsed = {key: KNOBS_BY_KEY[key].parse(raw) for key, raw in values.items()}
    # Against the resolved picture, not just the fields in this request: raising
    # the reorder point alone has to be checked against the target already
    # stored, or the pair can be walked into an invalid state one field at a time.
    current = await effective(session)
    cross_check({**{knob.key: getattr(current, knob.key) for knob in KNOBS}, **parsed})

    existing = {row.key: row for row in await session.scalars(select(AppSetting))}
    changes: list[dict[str, str]] = []
    for key, value in parsed.items():
        knob = KNOBS_BY_KEY[key]
        before = getattr(current, key)
        text = knob.store(value)
        row = existing.get(key)
        if row is None:
            session.add(AppSetting(key=key, value=text, updated_by=actor))
        else:
            if row.value == text:
                continue
            row.value = text
            row.updated_by = actor
        if str(before) == str(value):
            # Written, because an explicit override that happens to match the
            # environment is still a decision — it pins the value against a
            # deployment later changing the default underneath it.
            continue
        changes.append({"key": key, "from": str(before), "to": str(value)})

    if changes:
        session.add(
            AuditLog(
                actor=actor,
                action="settings.update",
                entity="settings",
                entity_id="policy",
                meta={"changes": changes},
            )
        )
    await session.flush()
    return changes


async def reset(session: AsyncSession, key: str, *, actor: str) -> dict[str, str]:
    """Drop an override and fall back to the environment.

    A separate verb rather than saving the default value, because the two mean
    different things: this says "no opinion here", which lets a later deployment
    move the default. Saving the same number says "this one, whatever you
    change", and a page with only a save button can express one of those.
    """
    knob = KNOBS_BY_KEY.get(key)
    if knob is None:
        raise SettingError(key, f"not a setting anyone may change: {key}")
    row = await session.get(AppSetting, key)
    if row is not None:
        await session.delete(row)
        session.add(
            AuditLog(
                actor=actor,
                action="settings.reset",
                entity="settings",
                entity_id=key,
                meta={"was": row.value},
            )
        )
    await session.flush()
    return {"key": key, "value": str(getattr(get_settings(), key))}
