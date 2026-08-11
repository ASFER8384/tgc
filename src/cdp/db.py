"""The customer platform uses the platform's database.

One engine, one connection pool, one session per request across both halves.
Keeping a second engine here would mean two pools against the same Postgres and,
worse, two transactions in a request that touched both sides — an ingest that
resolved a person and an order that recorded it could half commit.
"""

from sca.db import get_engine, get_sessionmaker, session_dep

__all__ = ["get_engine", "get_sessionmaker", "session_dep"]
