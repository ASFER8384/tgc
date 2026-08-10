from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.db import session_dep

SessionDep = Annotated[AsyncSession, Depends(session_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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
