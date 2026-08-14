"""buying policy per item, not only per deployment

The three thresholds that decide when and how much to buy, held on the item as
well as globally. Silk is bought deep and cartons thin, and one number for both
was a compromise nobody chose.

All nullable, all falling back to the global setting of the same name, so an
existing deployment upgrades into exactly the behaviour it had.

Revision ID: c3a91d60f47b
Revises: b1f0c7d24e55
Create Date: 2026-08-14 12:05:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base  # noqa: F401

revision: str = 'c3a91d60f47b'
down_revision: str | None = 'b1f0c7d24e55'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('items', sa.Column('reorder_cover_weeks', sa.Numeric(6, 2), nullable=True))
    op.add_column('items', sa.Column('target_cover_weeks', sa.Numeric(6, 2), nullable=True))
    op.add_column('items', sa.Column('demand_window_weeks', sa.Numeric(6, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('items', 'demand_window_weeks')
    op.drop_column('items', 'target_cover_weeks')
    op.drop_column('items', 'reorder_cover_weeks')
