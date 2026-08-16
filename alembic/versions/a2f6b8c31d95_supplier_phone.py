"""a supplier we could only write to

Every supplier carried an email address and no phone number, so the only way to
reach one was the one channel this system had wired. A mill that answers
WhatsApp within the hour and email the following week was, as far as the record
went, reachable only the slow way.

Stored in E.164 — country code, digits, nothing else — because that is the one
form a message can be addressed to. Keeping it as somebody typed it would push
the guess about which digits are the country code to send time, once per
message, on a number nobody could check afterwards.

Existing rows stay null. No phone number was ever collected and inventing one
from a country code would be worse than the gap.

Revision ID: a2f6b8c31d95
Revises: f1a3c7d92b04
Create Date: 2026-08-16 18:55:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'a2f6b8c31d95'
down_revision: str | None = 'f1a3c7d92b04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column('phone', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('suppliers', 'phone')
