"""what the storefront says it holds

The platform could not answer "how many abayas are there" because it only knew
about the ones in the shops. Shopify held the rest and was never asked. Two
counts existed, neither was the group's, and the one on the buying desk was the
smaller of them — which is the direction that hurts, because it buys stock that
is already sitting in a warehouse.

This mirrors the storefront's own variants so the online shelf can be read
rather than guessed. Read-only by design: Shopify moves its count on a checkout,
a refund, a fulfilment and a manual correction, none of which reach this
platform, so any figure kept here that tried to lead theirs would oversell the
website within a day.

Nothing is backfilled. The table is empty until somebody connects a store and
pulls, and an empty table reads as "never synced" everywhere it is shown —
which is true, and better than a zero that looks like an answer.

Revision ID: 7c1f0a9d3b45
Revises: 504846520807
Create Date: 2026-08-16 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = '7c1f0a9d3b45'
down_revision: str | None = '504846520807'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'shopify_variants',
        # Shopify's own ids, so a second pull updates rather than duplicates and
        # so a row here can be found again in their admin.
        sa.Column('variant_id', sa.String(64), primary_key=True),
        sa.Column('product_id', sa.String(64), nullable=False),
        # Nullable on purpose. A variant with no SKU belongs to no item here and
        # is in no total; that is a real fault worth seeing, not a row to drop.
        sa.Column('sku', sa.String(64), nullable=True),
        sa.Column('product_title', sa.String(300), nullable=False, server_default=''),
        sa.Column('handle', sa.String(300), nullable=True),
        sa.Column('vendor', sa.String(120), nullable=True),
        sa.Column('status', sa.String(16), nullable=True),
        sa.Column('variant_title', sa.String(300), nullable=True),
        sa.Column('options', base.JSONType, nullable=False, server_default='[]'),
        sa.Column('price', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('on_hand', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tracked', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('synced_at', base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('created_at', base.UTCDateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('ix_shopify_variants_sku', 'shopify_variants', ['sku'])


def downgrade() -> None:
    op.drop_index('ix_shopify_variants_sku', table_name='shopify_variants')
    op.drop_table('shopify_variants')
