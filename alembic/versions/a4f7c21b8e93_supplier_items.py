"""supplier items

Revision ID: a4f7c21b8e93
Revises: 9d1e4b7c0a52
Create Date: 2026-08-12 21:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

import sca.models.base  # noqa: F401

revision: str = 'a4f7c21b8e93'
down_revision: str | None = '9d1e4b7c0a52'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'supplier_items',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('supplier_id', sa.String(length=32), nullable=False),
        sa.Column('sku', sa.String(length=64), nullable=False),
        sa.Column('unit_cost', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('moq', sa.Integer(), nullable=False),
        sa.Column('pack_size', sa.Integer(), nullable=False),
        sa.Column('lead_time_days', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at',
            sca.models.base.UTCDateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['supplier_id'], ['suppliers.id'],
            name=op.f('fk_supplier_items_supplier_id_suppliers'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_items')),
        sa.UniqueConstraint('supplier_id', 'sku', name='uq_supplier_items_supplier_sku'),
    )
    op.create_index(
        op.f('ix_supplier_items_supplier_id'), 'supplier_items', ['supplier_id'], unique=False
    )
    op.create_index(op.f('ix_supplier_items_sku'), 'supplier_items', ['sku'], unique=False)

    # Every item already names one supplier and carries the terms for buying it
    # from them. That pairing is real data, so it becomes the first row rather
    # than being discarded and retyped — the table starts populated and nothing
    # downstream sees a gap on the day this ships.
    #
    # ids are generated in SQL because the model default is a Python function
    # and does not run for an INSERT ... SELECT.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT i.sku, i.supplier_id, i.unit_cost, i.moq, i.pack_size, "
            "       COALESCE(s.currency, 'SAR') AS currency "
            "FROM items i JOIN suppliers s ON s.id = i.supplier_id"
        )
    ).mappings().all()
    if rows:
        import uuid

        bind.execute(
            sa.text(
                "INSERT INTO supplier_items "
                "(id, supplier_id, sku, unit_cost, currency, moq, pack_size, active) "
                "VALUES (:id, :supplier_id, :sku, :unit_cost, :currency, :moq, :pack_size, TRUE)"
            ),
            [
                {
                    "id": uuid.uuid4().hex[:32],
                    "supplier_id": row["supplier_id"],
                    "sku": row["sku"],
                    "unit_cost": row["unit_cost"],
                    "currency": row["currency"],
                    "moq": row["moq"],
                    "pack_size": row["pack_size"],
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    op.drop_index(op.f('ix_supplier_items_sku'), table_name='supplier_items')
    op.drop_index(op.f('ix_supplier_items_supplier_id'), table_name='supplier_items')
    op.drop_table('supplier_items')
