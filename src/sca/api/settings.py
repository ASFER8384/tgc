"""The buying policy, read and changed from one page.

These numbers already existed; they lived on the environment, which meant that
raising an approval threshold was a deployment and that nobody outside the team
could see what the current one was. The policy is the part of this system a
buyer has actual authority over, and it was the only part they could not touch.

Every response says where each value came from — the environment or somebody's
decision — because "why is this order blocked" is a question that has to be
answerable without reading a container's configuration.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep
from sca.config import get_settings
from sca.models import AppSetting
from sca.settings.knobs import GROUPS, KNOBS, SettingError
from sca.settings.service import reset, save

router = APIRouter(tags=["settings"])


class SettingsIn(BaseModel):
    # A map rather than a model per knob: the registry is the schema, and a
    # pydantic class repeating it would be a second place to add a setting and
    # a second place to forget to.
    values: dict[str, str | float | bool]


async def _view(session) -> dict:
    """Every knob, its value, and the story of how it got that way."""
    env = get_settings()
    rows = {row.key: row for row in await session.scalars(select(AppSetting))}

    fields: dict[str, list[dict]] = {group: [] for group in GROUPS}
    for knob in KNOBS:
        row = rows.get(knob.key)
        default = getattr(env, knob.key)
        value, source, problem = default, "environment", None
        if row is not None:
            try:
                value, source = knob.parse(row.value), "set here"
            except SettingError as exc:
                # Kept visible rather than silently ignored. A stored value that
                # no longer passes its own bounds — because the bounds were
                # tightened in a release — is being disregarded at runtime, and
                # a page showing the environment default with no explanation
                # would make that look like nobody had ever changed it.
                problem = f"{row.value!r} is being ignored: {exc}"
        fields[knob.group].append(
            {
                "key": knob.key,
                "label": knob.label,
                "kind": knob.kind,
                "unit": knob.unit,
                "help": knob.help,
                "value": value,
                "default": default,
                "source": source,
                "env_var": knob.env_var,
                "minimum": knob.minimum,
                "maximum": knob.maximum,
                "updated_by": row.updated_by if row is not None and source == "set here" else None,
                "problem": problem,
            }
        )
    return {
        "env": env.env,
        "groups": [{"name": group, "fields": fields[group]} for group in GROUPS],
    }


@router.get("/settings")
async def read_settings(session: SessionDep, actor: ActorDep) -> dict:
    return await _view(session)


@router.put("/settings")
async def write_settings(body: SettingsIn, session: SessionDep, actor: ActorDep) -> dict:
    """Save a set of changes, or none of them.

    The whole page is sent, not the one field that moved, so that rules spanning
    two settings are checked against what the person is actually looking at.
    """
    try:
        changes = await save(session, dict(body.values), actor=actor)
    except SettingError as exc:
        # 422 with the key attached: the console puts the message beside the
        # field, which is the difference between a correctable mistake and a
        # red banner nobody can act on.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"key": exc.key, "message": str(exc)},
        ) from exc
    return await _view(session) | {"changed": changes}


@router.delete("/settings/{key}")
async def reset_setting(key: str, session: SessionDep, actor: ActorDep) -> dict:
    """Drop an override so the environment decides again."""
    try:
        await reset(session, key, actor=actor)
    except SettingError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, {"key": exc.key, "message": str(exc)}
        ) from exc
    return await _view(session)
