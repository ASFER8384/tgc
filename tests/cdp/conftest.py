"""Fixtures for the customer half, on the merged application.

The originals built their own app and their own database. Both now come from the
platform: one `create_app`, one metadata, one API key. Keeping a second set would
have let the two halves drift apart in tests while sharing a database in
production, which is the failure this merge exists to prevent.
"""

import os
from collections.abc import AsyncIterator

import pytest_asyncio

# Set before anything imports the settings, which are cached on first read.
os.environ.setdefault("CDP_SHOPIFY_WEBHOOK_SECRET", "test-shopify-secret")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import cdp.models  # noqa: E402, F401  registers the customer tables
from sca.db import session_dep  # noqa: E402
from sca.main import create_app  # noqa: E402
from sca.models import Base  # noqa: E402
from tests.conftest import API_KEY  # noqa: E402

SHOPIFY_SECRET = "test-shopify-secret"


@pytest_asyncio.fixture
async def engine():
    """One in-memory database per test, built from the shared metadata.

    Shared, so a customer test now creates the supplier tables too. That is the
    point: the two halves are one schema, and a test that cannot see the other
    side would not notice if a merge broke it.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(sessionmaker_) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as session:
        yield session


@pytest_asyncio.fixture
async def client(sessionmaker_) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[session_dep] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["X-API-Key"] = API_KEY
        yield client
