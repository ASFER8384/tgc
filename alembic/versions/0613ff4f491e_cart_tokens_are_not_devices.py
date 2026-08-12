"""cart tokens are not devices

The Shopify connector fell back to the cart token when no storefront pixel was
installed, and stored it under ``device_id``. A cart token names one checkout,
so a customer with forty orders acquired forty "devices" — noise on every
profile, and a claim about her hardware that was never true.

The connector now stores the two separately. This relabels what is already
there. Every existing ``device_id`` came from a cart token: ``browser_ip_hash``
requires a storefront pixel that has never been configured on this deployment,
so there is no row this could mislabel. That is a fact about this database at
this point in its life, not a general rule — a later deployment with a pixel
would need to distinguish them by value, which is why the connector was fixed
rather than this being left to a periodic cleanup.

Nothing is merged or unmerged by this: both kinds are weak, and weak identifiers
never join two people on their own.

Revision ID: 0613ff4f491e
Revises: 6752ea553786
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0613ff4f491e"
down_revision: str | Sequence[str] | None = "6752ea553786"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE identifiers SET kind = 'cart_token' WHERE kind = 'device_id'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE identifiers SET kind = 'device_id' WHERE kind = 'cart_token'"))
