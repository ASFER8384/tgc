"""store supplier attachments

Revision ID: 9d1e4b7c0a52
Revises: 3862b4ddc933
Create Date: 2026-08-12 18:05:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

import sca.models.base  # noqa: F401

revision: str = '9d1e4b7c0a52'
down_revision: str | None = '3862b4ddc933'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'attachments',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('inbound_message_id', sa.String(length=32), nullable=False),
        sa.Column('filename', sa.String(length=300), nullable=False),
        sa.Column('content_type', sa.String(length=120), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column(
            'created_at',
            sca.models.base.UTCDateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['inbound_message_id'], ['inbound_messages.id'],
            name=op.f('fk_attachments_inbound_message_id_inbound_messages'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_attachments')),
        sa.UniqueConstraint('inbound_message_id', 'sha256', name='uq_attachments_message_sha'),
    )
    op.create_index(
        op.f('ix_attachments_inbound_message_id'), 'attachments',
        ['inbound_message_id'], unique=False,
    )
    op.create_index(op.f('ix_attachments_sha256'), 'attachments', ['sha256'], unique=False)

    # Batch mode so the same migration runs on SQLite, which cannot alter a
    # column or add a foreign key in place.
    with op.batch_alter_table('documents') as batch:
        batch.alter_column(
            'filename', existing_type=sa.String(length=200), type_=sa.String(length=300),
            existing_nullable=False,
        )
        batch.add_column(sa.Column('attachment_id', sa.String(length=32), nullable=True))
        batch.create_index(
            op.f('ix_documents_attachment_id'), ['attachment_id'], unique=False
        )
        batch.create_foreign_key(
            op.f('fk_documents_attachment_id_attachments'), 'attachments',
            ['attachment_id'], ['id'],
        )

    # Rows filed before this migration name a file that was never stored: the
    # ingest path invented "PO-5003-invoice.pdf" from the word "invoice" in a
    # message body. Left in place, they are a link that cannot resolve. They are
    # marked rather than deleted, because the row still records that a message
    # was read as an invoice on that date, and the message itself is intact.
    op.execute(
        "UPDATE documents SET filename = filename || ' (no file stored)' "
        "WHERE attachment_id IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.drop_constraint(op.f('fk_documents_attachment_id_attachments'), type_='foreignkey')
        batch.drop_index(op.f('ix_documents_attachment_id'))
        batch.drop_column('attachment_id')
        batch.alter_column(
            'filename', existing_type=sa.String(length=300), type_=sa.String(length=200),
            existing_nullable=False,
        )
    op.drop_index(op.f('ix_attachments_sha256'), table_name='attachments')
    op.drop_index(op.f('ix_attachments_inbound_message_id'), table_name='attachments')
    op.drop_table('attachments')
