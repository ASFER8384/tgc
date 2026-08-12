from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, JSONType, TimestampMixin, new_id

# What starts an automation. Only the ones this platform can actually observe:
# a customer entering an audience, an order landing, a customer going quiet, or
# a clock. Offering triggers nothing listens for would be a menu of dead ends.
TRIGGERS = ("segment_entered", "order_placed", "winback", "scheduled")

# What it does. `wait` and `exit` carry no message; `message` names a channel and
# the copy, and the channel is re-checked for consent when the step runs rather
# than when it is written.
STEP_KINDS = ("message", "wait", "exit")


class Automation(Base, TimestampMixin):
    """A stored flow: one trigger, an ordered list of steps.

    The definition is held as JSON rather than as a table per step type. Steps
    nest and their shapes differ per kind, so a relational layout would be five
    joins to read one flow, and every new step kind would be a migration. What
    matters relationally is which flow ran for whom, and that lives on the
    activation tables that already exist.
    """

    __tablename__ = "automations"
    __table_args__ = (UniqueConstraint("name", name="uq_automations_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[dict] = mapped_column(JSONType, nullable=False)
    steps: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # Off until someone turns it on. A flow that starts sending the moment it is
    # saved gives no room to read it back before it reaches a customer.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stop_on_purchase: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reentry_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
