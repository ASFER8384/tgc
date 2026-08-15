"""somewhere to put a sale nobody left a name on

A shop sale paid in cash has no identifier at all. Resolving it the ordinary way
would mint a fresh person for every basket, so the counter gets one standing
record per till and the sales land on that. It is not a customer, and this column
is how everything that addresses people knows to leave it out.

Existing rows are people, so the default is false and the backfill is a constant.

Revision ID: d4b28f1a6c07
Revises: c3a91d60f47b
Create Date: 2026-08-15 10:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd4b28f1a6c07'
down_revision: str | None = 'c3a91d60f47b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added with a server default so the column is populated for existing rows in
    # one statement, then dropped: the application always states the value, and
    # leaving the default in place would let a future insert forget to.
    op.add_column(
        'persons',
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # In a batch, because SQLite has no ALTER COLUMN and rebuilds the table
    # instead. The suite runs on SQLite and Postgres is the deployment target, so
    # a migration that only works on one of them is a migration nothing tests.
    with op.batch_alter_table('persons') as batch:
        batch.alter_column('synthetic', server_default=None)


def downgrade() -> None:
    op.drop_column('persons', 'synthetic')
