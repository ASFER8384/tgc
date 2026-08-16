"""files could only arrive, never leave

An attachment had to belong to an inbound message, which encoded an assumption
that only suppliers send files. On WhatsApp they send photographs of packing
lists and PDFs of invoices, and we send specifications, artwork and revised
schedules back — and ours had nowhere to be kept, so the drawer would have shown
one side's files against the other side's silence.

`inbound_message_id` becomes optional and `sent_message_id` joins it. Exactly one
of the two is set on any row; the pairwise uniqueness that stopped one file being
stored twice against a message now exists on both sides.

Existing rows are untouched — every attachment stored so far did arrive.

Revision ID: b7e2c1f480a3
Revises: a2f6b8c31d95
Create Date: 2026-08-17 10:10:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'b7e2c1f480a3'
down_revision: str | None = 'a2f6b8c31d95'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('attachments', sa.Column('sent_message_id', sa.String(length=32), nullable=True))
    op.create_index('ix_attachments_sent_message_id', 'attachments', ['sent_message_id'])
    op.create_foreign_key(
        'fk_attachments_sent_message_id', 'attachments', 'sent_messages',
        ['sent_message_id'], ['id'],
    )
    op.create_unique_constraint(
        'uq_attachments_sent_sha', 'attachments', ['sent_message_id', 'sha256'],
    )
    op.alter_column(
        'attachments', 'inbound_message_id',
        existing_type=sa.String(length=32), nullable=True,
    )


def downgrade() -> None:
    # Anything we sent has no inbound message to hang from, so it cannot survive
    # the column becoming required again.
    op.execute("DELETE FROM attachments WHERE inbound_message_id IS NULL")
    op.alter_column(
        'attachments', 'inbound_message_id',
        existing_type=sa.String(length=32), nullable=False,
    )
    op.drop_constraint('uq_attachments_sent_sha', 'attachments', type_='unique')
    op.drop_constraint('fk_attachments_sent_message_id', 'attachments', type_='foreignkey')
    op.drop_index('ix_attachments_sent_message_id', table_name='attachments')
    op.drop_column('attachments', 'sent_message_id')
