"""our half of the conversation was never written down

Supplier replies were stored verbatim from the beginning. What this system said
to them was composed, sent and forgotten, so the record was one-sided: a reply
could be answering figures nobody here could still produce, because the order
had been revised since and the composer only renders an order as it stands now.

Backfilling is not possible and should not be faked. The audit log knows a
letter went out, when, and to which address; it does not know what it said, and
recomposing one from today's rows would read as evidence while showing figures
that were never sent. History stays empty and says so.

Failures get rows too. "We tried and the mail bounced" and "nobody told them"
are different facts, and only one of them is somebody else's fault.

Revision ID: f1a3c7d92b04
Revises: e5f9c02a71b8
Create Date: 2026-08-16 11:05:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'f1a3c7d92b04'
down_revision: str | None = 'e5f9c02a71b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'sent_messages',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('purchase_order_id', sa.String(length=32), nullable=True),
        sa.Column('supplier_id', sa.String(length=32), nullable=True),
        sa.Column('to_address', sa.String(length=320), nullable=True),
        sa.Column('subject', sa.String(length=500), nullable=True),
        sa.Column('body', sa.String(length=20000), nullable=False, server_default=''),
        sa.Column('kind', sa.String(length=32), nullable=False, server_default='order'),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('delivered', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('provider', sa.String(length=16), nullable=False, server_default='none'),
        sa.Column('failure', sa.String(length=300), nullable=True),
        sa.Column('sent_at', base.UTCDateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at', base.UTCDateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_sent_messages_purchase_order_id', 'sent_messages', ['purchase_order_id'])
    op.create_index('ix_sent_messages_supplier_id', 'sent_messages', ['supplier_id'])


def downgrade() -> None:
    op.drop_index('ix_sent_messages_supplier_id', table_name='sent_messages')
    op.drop_index('ix_sent_messages_purchase_order_id', table_name='sent_messages')
    op.drop_table('sent_messages')
