"""an item the storefront does not sell still comes in sizes

A shop cannot count a rail by size until something names the sizes. For the four
items on Shopify that is answered — the storefront reports 52 through 60 and the
count boxes are built from it. An item created in this console has nothing to
ask, so it could be split across shops and never broken down further.

This gives such an item its own list. Empty for every existing row and empty by
default, which is the normal state and means "counted as one number" rather than
"one size" — the four that Shopify carries keep taking their sizes from there,
because two lists that could disagree is worse than one and the storefront's is
the one a customer buys from.

Revision ID: b4e2f8c19d37
Revises: 9a3d5e1c7b02
Create Date: 2026-08-16 13:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'b4e2f8c19d37'
down_revision: str | None = '9a3d5e1c7b02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'items',
        sa.Column('variants', base.JSONType, nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('items', 'variants')
