"""The migration and the models must describe the same schema.

Drift between them is the classic silent failure: the suite passes against
create_all, production runs on alembic upgrade head, and a column that exists in
tests is missing in Riyadh. Here the migration is actually executed and compared
against the metadata, so drift fails the suite rather than a deployment.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from sca.models import Base

IGNORED_TABLES = {"alembic_version"}


def _alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_matches_the_models(tmp_path, monkeypatch) -> None:
    db = tmp_path / "migrated.sqlite3"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("SCA_DATABASE_URL", f"sqlite+aiosqlite:///{db.as_posix()}")

    command.upgrade(_alembic_config(f"sqlite+aiosqlite:///{db.as_posix()}"), "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        migrated = {t for t in inspector.get_table_names() if t not in IGNORED_TABLES}
        assert migrated == set(Base.metadata.tables)

        for table in sorted(Base.metadata.tables):
            migrated_columns = {c["name"] for c in inspector.get_columns(table)}
            model_columns = set(Base.metadata.tables[table].columns.keys())
            assert migrated_columns == model_columns, f"{table} columns drifted"
    finally:
        engine.dispose()


def test_every_model_table_has_a_primary_key() -> None:
    missing = [
        name for name, table in Base.metadata.tables.items() if not table.primary_key.columns
    ]
    assert missing == []
