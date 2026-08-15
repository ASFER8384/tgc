"""a shelf holds sizes, not just abayas

The storefront knows its stock by size and shade; the shops know theirs only by
item. So the group can say it holds three abayas in Jeddah and cannot say whether
any of them is the Small a customer just asked for — and since the counter is
where most of the trade happens, the size curve is being inferred from the
minority of it that sells online.

This adds the third level under the same rule as the two above: the group's
total is the sum of its shelves, and a shelf's total is the sum of the variants
counted on it.

**Nothing is backfilled, deliberately.** Splitting the three abayas in Jeddah
across five sizes would be inventing the exact fact this table exists to
establish, and a wrong size count is discovered by a customer standing in front
of a rail. An empty table means no shelf has been broken down, every item-level
count stands untouched, and the first real number arrives when somebody walks a
rail and types it.

Revision ID: 9a3d5e1c7b02
Revises: 7c1f0a9d3b45
Create Date: 2026-08-16 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = '9a3d5e1c7b02'
down_revision: str | None = '7c1f0a9d3b45'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'stock_at_variant',
        sa.Column('sku', sa.String(64), primary_key=True),
        sa.Column('location_code', sa.String(32), sa.ForeignKey('stock_locations.code'),
                  primary_key=True),
        # Shopify's own label for the row — "Small", "Rose". The string rather
        # than an id, because it is what the storefront reports, what the till
        # offers and what a shop assistant is reading off a ticket.
        sa.Column('variant', sa.String(120), primary_key=True),
        sa.Column('on_hand', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', base.UTCDateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('stock_at_variant')
