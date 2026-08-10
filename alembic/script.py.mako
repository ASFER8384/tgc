"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

# Autogenerate renders custom types by qualified name, so the module has to be
# importable here. It also means the migration carries the JSONB variant and the
# UTC timestamp decorator rather than degrading to plain JSON and naive
# timestamps on Postgres, which is how a hand tidied first migration drifts away
# from the models without anyone noticing.
import sca.models.base  # noqa: F401

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
