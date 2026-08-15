"""twenty abayas is not one number

Stock was one figure per item, which was true while everything sold from one
place. It stopped being true once the storefront and the shops were both selling
from it: twenty is ten the website can ship, five in one shop and five in the
other, and only the first of those can be promised to somebody online.

The rolled-up total stays on ``stock_snapshots`` and stays what buying reads. An
order goes to a mill for the group rather than for a shelf, and splitting the
reorder decision per location would order the same thing three times over. What
this adds is where that total actually sits.

Existing stock is placed at the storefront rather than spread evenly. Guessing a
split would put units in shops that may not hold them, and a wrong number in a
shop is discovered by a customer standing in front of an empty rail. The
storefront is the one location whose count can be checked against Shopify in a
second, so a wrong guess there is visible immediately. Move it afterwards from
the console once somebody has counted.

Revision ID: 504846520807
Revises: e7c41a9b8d20
Create Date: 2026-08-15 21:05:00.000000
"""
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = '504846520807'
down_revision: str | None = 'e7c41a9b8d20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'stock_locations',
        sa.Column('code', sa.String(32), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False, server_default='retail'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', base.UTCDateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_table(
        'stock_at_location',
        sa.Column('sku', sa.String(64), primary_key=True),
        sa.Column('location_code', sa.String(32), sa.ForeignKey('stock_locations.code'),
                  primary_key=True),
        sa.Column('on_hand', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('on_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', base.UTCDateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    # Null goes on keeping its present meaning — the group's position — because
    # every row already in this table is one, and the demand correction reads
    # them. A backfill that stamped them all with a location would be inventing
    # a fact nobody recorded.
    # In a batch, because SQLite cannot ALTER in a foreign key and rebuilds the
    # table instead. The suite runs on SQLite and Postgres is the deployment
    # target, so a migration that only works on one of them is one nothing tests.
    with op.batch_alter_table('stock_levels') as batch:
        batch.add_column(
            sa.Column('location_code', sa.String(32), nullable=True),
        )
        batch.create_foreign_key(
            'fk_stock_levels_location', 'stock_locations', ['location_code'], ['code'],
        )

    # Stamped here rather than left to the column default. now() is Postgres's
    # spelling and the suite runs these migrations on SQLite, where the default
    # is only evaluated when a row is actually written — which is exactly what
    # the next two statements do.
    stamp = datetime.now(UTC)
    locations = sa.table(
        'stock_locations',
        sa.column('code', sa.String), sa.column('name', sa.String),
        sa.column('kind', sa.String), sa.column('active', sa.Boolean),
        sa.column('created_at', base.UTCDateTime(timezone=True)),
    )
    op.bulk_insert(locations, [
        {'code': 'online', 'name': 'Shopify storefront', 'kind': 'online',
         'active': True, 'created_at': stamp},
        {'code': 'riyadh', 'name': 'Riyadh shop', 'kind': 'retail',
         'active': True, 'created_at': stamp},
        {'code': 'jeddah', 'name': 'Jeddah shop', 'kind': 'retail',
         'active': True, 'created_at': stamp},
    ])

    # Everything on the storefront's shelf to begin with, so the total is
    # unchanged and nothing downstream moves. See the note above on why this is
    # not spread across the shops.
    op.execute(
        sa.text(
            "INSERT INTO stock_at_location "
            "(sku, location_code, on_hand, on_order, created_at) "
            "SELECT sku, 'online', on_hand, on_order, :stamp FROM stock_snapshots"
        ).bindparams(stamp=stamp)
    )


def downgrade() -> None:
    with op.batch_alter_table('stock_levels') as batch:
        batch.drop_column('location_code')
    op.drop_table('stock_at_location')
    op.drop_table('stock_locations')
