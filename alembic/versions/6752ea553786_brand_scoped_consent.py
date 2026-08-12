"""brand scoped consent

Consent was recorded per person and purpose, which made a grant given at an
Aleena checkout into a company-wide permission that Rawash could rely on. This
adds the brand a grant was made with, and the brand an audience is built for.

The backfill is the interesting part. Existing rows carry no brand and one
cannot be invented, so each legacy grant is expanded into one grant per brand
the person has actually bought from — which is what the data says happened, and
is a claim that can be defended row by row. Where a person has bought from
nothing (an offline capture with no order behind it) the row is left unattributed,
and an unattributed grant satisfies no brand-scoped gate. Losing an audience
member is the right outcome there; asserting a permission nobody gave is not.

Revision ID: 6752ea553786
Revises: 800e9227e7ed
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6752ea553786"
down_revision: str | Sequence[str] | None = "800e9227e7ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KNOWN_BRANDS = ("aleena", "rawash", "aynola")


def upgrade() -> None:
    with op.batch_alter_table("consent_events") as batch:
        batch.add_column(sa.Column("brand", sa.String(length=64), nullable=True))
        batch.create_index(
            "ix_consent_person_brand", ["person_id", "brand", "purpose", "occurred_at"]
        )
    with op.batch_alter_table("segments") as batch:
        batch.add_column(sa.Column("brand", sa.String(length=64), nullable=True))

    _backfill()


def _backfill() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, person_id, purpose, granted, source, evidence, occurred_at "
            "FROM consent_events WHERE brand IS NULL"
        )
    ).fetchall()
    if not rows:
        return

    purchases: dict[str, list[str]] = {}
    for person_id, brand in connection.execute(
        sa.text("SELECT person_id, brand FROM person_brand_stats")
    ).fetchall():
        if brand in KNOWN_BRANDS:
            purchases.setdefault(person_id, []).append(brand)

    # new_id() in the application is a hex uuid; matched here so the ids these
    # rows get are indistinguishable from any other, and nothing downstream has
    # to know which grants arrived by migration.
    import uuid

    for row in rows:
        brands = purchases.get(row.person_id, [])
        if not brands:
            continue
        connection.execute(
            sa.text("UPDATE consent_events SET brand = :brand WHERE id = :id"),
            {"brand": brands[0], "id": row.id},
        )
        for extra in brands[1:]:
            connection.execute(
                sa.text(
                    "INSERT INTO consent_events "
                    "(id, person_id, brand, purpose, granted, source, evidence, occurred_at, "
                    " created_at) "
                    "VALUES (:id, :person_id, :brand, :purpose, :granted, :source, :evidence, "
                    " :occurred_at, :occurred_at)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "person_id": row.person_id,
                    "brand": extra,
                    "purpose": row.purpose,
                    "granted": row.granted,
                    "source": row.source,
                    # Marked, so nobody later mistakes a migration's inference
                    # for something the customer was asked.
                    "evidence": (row.evidence or "") + " [backfilled from purchase history]",
                    "occurred_at": row.occurred_at,
                },
            )


def downgrade() -> None:
    # The rows the backfill created cannot be told apart from real ones by
    # anything but their evidence note, so they are removed on the way down
    # rather than left behind as unexplained duplicates.
    op.execute(
        sa.text(
            "DELETE FROM consent_events "
            "WHERE evidence LIKE '%[backfilled from purchase history]'"
        )
    )
    with op.batch_alter_table("segments") as batch:
        batch.drop_column("brand")
    with op.batch_alter_table("consent_events") as batch:
        batch.drop_index("ix_consent_person_brand")
        batch.drop_column("brand")
