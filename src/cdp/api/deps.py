from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import Settings, get_settings
from cdp.db import session_dep
from sca.config import get_settings as get_platform_settings

SessionDep = Annotated[AsyncSession, Depends(session_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Placeholder for SSO + RBAC (see README roadmap). It is a dependency rather
    than an inline check so replacing it with real roles touches one file, and so
    every protected route is protected the same way.

    One credential for the whole platform: a console with a sidebar is one
    application to the person using it, and asking them to hold two keys for one
    screen would be an implementation detail leaking into their hands.
    """
    if not x_api_key or x_api_key != get_platform_settings().api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")
    return "api-key-user"


ActorDep = Annotated[str, Depends(require_api_key)]
