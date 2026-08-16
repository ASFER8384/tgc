"""a mill cannot cut eighteen abayas

An order line carried a quantity and nothing about its shape, so the size curve
the forecast works out per shop and per size was thrown away at the moment it
became an order — and somebody read it off a screen and typed it into an email.

This gives the line the curve. Null rather than an empty object, because no
split is not the same fact as an even one: the four items Shopify carries have
sizes to state and an item nobody has broken down does not, and rendering the
second as the first would put a number in front of a mill that no one chose.

Existing lines stay null. They were ordered without a stated curve and inventing
one now would be writing history that did not happen.

Revision ID: c8d1a4f27e60
Revises: b4e2f8c19d37
Create Date: 2026-08-16 11:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'c8d1a4f27e60'
down_revision: str | None = 'b4e2f8c19d37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'purchase_order_lines',
        sa.Column('sizes', base.JSONType, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('purchase_order_lines', 'sizes')
