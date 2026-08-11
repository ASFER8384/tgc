"""SQLite backed test app.

The suite runs with nothing installed and nothing running, which is what keeps it
useful in CI and on a laptop. Postgres remains the production target, and
test_schema.py is what stops the two drifting apart.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sca.config import get_settings
from sca.db import session_dep
from sca.main import create_app
from sca.models import Base

# Read from settings rather than hardcoded: the app authenticates against
# whatever SCA_API_KEY is configured, so a literal here turns every rotation of
# the key into a suite-wide 401 that surfaces as a confusing KeyError.
API_KEY = get_settings().api_key


@pytest_asyncio.fixture
async def sessionmaker_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def session(sessionmaker_fixture):
    async with sessionmaker_fixture() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture
async def client(sessionmaker_fixture):
    app = create_app()

    async def override():
        async with sessionmaker_fixture() as session:
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


@pytest.fixture
def supplier_payload():
    return {
        "code": "GZ-TEX",
        "name": "Guangzhou Silk Mill",
        "country": "CN",
        "email": "orders@gzsilkmill.cn",
        "timezone": "Asia/Shanghai",
        "working_days": "1,2,3,4,5,6",
        "work_start_hour": 8,
        "work_end_hour": 18,
        "lead_time_days": 42,
        "currency": "CNY",
    }
