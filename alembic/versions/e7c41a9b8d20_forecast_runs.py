"""what the model predicted, and whether it was allowed out

Predictions are stored rather than recomputed because a forecast overwritten by
the next one can never be checked against what actually happened. Rejected runs
are kept beside accepted ones: keeping only the winners hides the trend that says
the data has changed.

Revision ID: e7c41a9b8d20
Revises: d4b28f1a6c07
Create Date: 2026-08-15 15:10:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import sca.models.base as base  # noqa: F401

revision: str = 'e7c41a9b8d20'
down_revision: str | None = 'd4b28f1a6c07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'forecast_runs',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('ran_at', base.UTCDateTime(), nullable=False),
        sa.Column('actor', sa.String(64), nullable=False),
        sa.Column('horizon_weeks', sa.Integer(), nullable=False),
        sa.Column('weeks_history', sa.Integer(), nullable=False),
        sa.Column('train_rows', sa.Integer(), nullable=False),
        sa.Column('passed', sa.Boolean(), nullable=False),
        sa.Column('published', sa.Boolean(), nullable=False),
        sa.Column('refusal', sa.Text(), nullable=True),
        sa.Column('metrics', JSON, nullable=False),
        sa.Column('params', JSON, nullable=False),
        sa.Column('created_at', base.UTCDateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )
    op.create_table(
        'forecast_items',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('run_id', sa.String(32), sa.ForeignKey('forecast_runs.id'), nullable=False),
        sa.Column('sku', sa.String(64), nullable=False),
        sa.Column('week', sa.Date(), nullable=False),
        sa.Column('units', sa.Float(), nullable=False),
        sa.Column('buyers', sa.Float(), nullable=False),
    )
    op.create_index('ix_forecast_items_run_id', 'forecast_items', ['run_id'])
    op.create_index('ix_forecast_items_run_sku', 'forecast_items', ['run_id', 'sku'])

    op.create_table(
        'forecast_buyers',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('run_id', sa.String(32), sa.ForeignKey('forecast_runs.id'), nullable=False),
        sa.Column('sku', sa.String(64), nullable=False),
        sa.Column('person_id', sa.String(32), nullable=False),
        sa.Column('probability', sa.Float(), nullable=False),
        sa.Column('expected_units', sa.Float(), nullable=False),
    )
    op.create_index('ix_forecast_buyers_run_id', 'forecast_buyers', ['run_id'])
    op.create_index('ix_forecast_buyers_person_id', 'forecast_buyers', ['person_id'])
    op.create_index('ix_forecast_buyers_run_sku', 'forecast_buyers', ['run_id', 'sku'])


def downgrade() -> None:
    op.drop_table('forecast_buyers')
    op.drop_table('forecast_items')
    op.drop_table('forecast_runs')
