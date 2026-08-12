"""identifier capture context

Records how each identifier was obtained, so that a number which may belong to
someone other than the person it is attached to — written on a form at a mall
stand, or given as the delivery contact on a gift — can be refused for addressed
messaging however good the consent looks.

Existing rows are backfilled from the source of the event that produced them
rather than left unknown, because the source is the evidence: identifiers first
seen on an activation event were captured at an activation, and nothing else
could have produced them. Rows whose origin cannot be established stay NULL and
are treated as ordinary. Marking the whole existing base as risky would empty
every campaign and teach everyone to ignore the flag.

Revision ID: 648dc613a950
Revises: 0613ff4f491e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "648dc613a950"
down_revision: str | Sequence[str] | None = "0613ff4f491e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Event source -> the circumstance that produced the identifier.
BY_SOURCE = {
    "activation": "activation",
    "whatsapp": "messaging",
    "shopify": "checkout",
    "shopify_pos": "checkout",
}


def upgrade() -> None:
    with op.batch_alter_table("identifiers") as batch:
        batch.add_column(sa.Column("capture_context", sa.String(length=32), nullable=True))

    with op.batch_alter_table("activation_runs") as batch:
        batch.add_column(
            sa.Column(
                "skipped_identifier_risk",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    connection = op.get_bind()
    # The earliest event for a person is the one that first produced her
    # identifiers, which is why first_seen_at is the join rather than any event.
    for source, context in BY_SOURCE.items():
        connection.execute(
            sa.text(
                "UPDATE identifiers SET capture_context = :context "
                "WHERE capture_context IS NULL AND EXISTS ("
                "  SELECT 1 FROM events e"
                "  WHERE e.person_id = identifiers.person_id"
                "    AND e.source = :source"
                "    AND e.occurred_at <= identifiers.first_seen_at"
                ")"
            ),
            {"context": context, "source": source},
        )


def downgrade() -> None:
    with op.batch_alter_table("activation_runs") as batch:
        batch.drop_column("skipped_identifier_risk")
    with op.batch_alter_table("identifiers") as batch:
        batch.drop_column("capture_context")
