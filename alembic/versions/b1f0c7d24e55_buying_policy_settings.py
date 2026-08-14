"""buying policy a buyer can change, and a floor in units per item

Two halves of one decision. The table holds the policy numbers somebody has
overridden from the console; the column holds the minimum stock an individual
item is held above, which is the setting that could never be global — a floor
that suits abayas is wrong for cartons.

Both are additive and nullable, so an existing deployment upgrades into exactly
the behaviour it had: no rows means every setting still comes from the
environment, and a null minimum means the item falls back to the global default,
which ships as zero, which is off.

Revision ID: b1f0c7d24e55
Revises: 399b3e22da79
Create Date: 2026-08-14 10:20:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base  # noqa: F401

revision: str = 'b1f0c7d24e55'
down_revision: str | None = '399b3e22da79'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=200), nullable=False),
        sa.Column('updated_by', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at',
            sca.models.base.UTCDateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings')),
    )
    op.add_column('items', sa.Column('min_stock', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('items', 'min_stock')
    op.drop_table('app_settings')
