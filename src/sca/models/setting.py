"""Policy numbers a buyer is allowed to change without a deployment.

Every value here also exists as an environment variable, and the environment
remains the default. This table holds only the ones somebody has deliberately
overridden in the console, which is why it is sparse rather than a full copy of
the configuration: a row means "a person decided this", and its absence means
"whatever the environment says", so a deployment that raises a default still
raises it everywhere nobody had an opinion.

Values are stored as text and parsed against the registry in
``sca.settings.knobs``. A column per setting would need a migration every time a
knob is added, which is exactly the friction this table exists to remove; the
type safety that loses is bought back by validating on the way in and again on
the way out, and a row whose knob no longer exists is ignored rather than
crashing the service that outlived it.
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    # Who moved it. An approval threshold is the number that decides which spend
    # needs a human, so "it has always been that" must never be the only account
    # available of why it is what it is.
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
