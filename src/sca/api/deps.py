from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.db import session_dep

SessionDep = Annotated[AsyncSession, Depends(session_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def runtime_settings(session: SessionDep) -> Settings:
    """The environment, with whatever the console has overridden applied.

    Distinct from ``SettingsDep`` on purpose, and both are needed. Authentication
    and the database URL are read from the environment only — a policy table
    that can rewrite the API key is one a browser can lock the service out of
    its own data with. Everything a buyer is allowed to tune comes through here.
    """
    from sca.settings.service import effective

    return await effective(session)


RuntimeSettingsDep = Annotated[Settings, Depends(runtime_settings)]


async def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Placeholder for SSO and roles. A dependency rather than an inline check so
    replacing it touches one file, and so every protected route is protected the
    same way. Buying approval in particular needs a real named user behind it."""
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")
    return "api-key-user"


ActorDep = Annotated[str, Depends(require_api_key)]
