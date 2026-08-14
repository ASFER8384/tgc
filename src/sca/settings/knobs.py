"""Which settings a person may change from the console, and what a legal value is.

Deliberately a whitelist rather than "everything on the Settings object". Two of
those fields are a database password and an API key, and a page that can edit
them is a page that can lock the service out of its own data from a browser.
Credentials, the database URL and the switches that disable authentication are
changed where they have always been changed: on the environment, by whoever
deploys it.

What is here is the buying policy — the numbers a buyer has an opinion about and
currently has to ask an engineer to move. Each one carries its own bounds,
because the failure mode of a free text box on a threshold is not a validation
error, it is an order for four hundred thousand pieces that nobody notices until
the mill confirms it.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SettingError(ValueError):
    """A value that would not be safe or would not parse. Carries the key so the
    console can put the message beside the field the person actually typed in."""

    def __init__(self, key: str, message: str):
        self.key = key
        super().__init__(message)


@dataclass(frozen=True)
class Knob:
    """One editable setting.

    ``key`` is both the attribute on ``Settings`` and the row key in
    ``app_settings``, so the environment variable that backs it is always
    ``SCA_`` plus this name. Keeping the three the same is what makes "where
    does this number come from" answerable in one step.
    """

    key: str
    group: str
    label: str
    kind: str  # int | float | bool | timezone
    help: str
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None

    @property
    def env_var(self) -> str:
        return f"SCA_{self.key.upper()}"

    def parse(self, raw: object) -> object:
        """Text or JSON from a browser into the type the setting actually is.

        Raises rather than falling back to a default. A threshold that silently
        becomes 25000 because the field was typed wrong is worse than one that
        refuses to save: the person walks away believing they changed it.
        """
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
            raise SettingError(self.key, f"{self.label} must be true or false")

        if self.kind == "timezone":
            name = str(raw).strip()
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
                raise SettingError(
                    self.key, f"{name!r} is not an IANA time zone, for example Asia/Riyadh"
                ) from exc
            return name

        text = str(raw).strip()
        if not text:
            raise SettingError(self.key, f"{self.label} cannot be blank")
        try:
            value = float(text)
        except ValueError as exc:
            raise SettingError(self.key, f"{self.label} must be a number") from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise SettingError(self.key, f"{self.label} must be a number")
        if self.kind == "int":
            if value != int(value):
                raise SettingError(self.key, f"{self.label} must be a whole number")
            value = int(value)
        if self.minimum is not None and value < self.minimum:
            raise SettingError(self.key, f"{self.label} cannot be below {_plain(self.minimum)}")
        if self.maximum is not None and value > self.maximum:
            raise SettingError(self.key, f"{self.label} cannot be above {_plain(self.maximum)}")
        return value

    def store(self, value: object) -> str:
        """The parsed value as the text that goes in the row."""
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


KNOBS: tuple[Knob, ...] = (
    # ------------------------------------------------------------ when to buy
    Knob(
        key="reorder_cover_weeks",
        group="When to buy",
        label="Reorder below",
        kind="float",
        unit="weeks of cover",
        minimum=0.5,
        maximum=52,
        help=(
            "A line is suggested once stock plus what is already on order falls "
            "below this many weeks of demand. Whichever is longer, this or the "
            "supplier's own lead time, is what actually triggers: a mill that "
            "takes six weeks has to be ordered from before cover runs to four, "
            "or the order arrives after the shelf is empty."
        ),
    ),
    Knob(
        key="target_cover_weeks",
        group="When to buy",
        label="Buy up to",
        kind="float",
        unit="weeks of cover",
        minimum=1,
        maximum=104,
        help=(
            "How much is bought once a line triggers. The quantity is this many "
            "weeks of demand less what is already available, then rounded up to "
            "the supplier's minimum order and pack size — so the amount ordered "
            "is usually more than this asks for, and always is on a line whose "
            "mill will not cut below a thousand."
        ),
    ),
    Knob(
        key="min_stock_default",
        group="When to buy",
        label="Never hold fewer than",
        kind="int",
        unit="units, any item",
        minimum=0,
        maximum=1_000_000,
        help=(
            "A floor in units applied to every item that has not set its own, "
            "regardless of what the forecast says. This is what makes \"never "
            "fewer than fifty abayas\" expressible at all: cover in weeks needs "
            "a demand figure to divide by, and an item nobody has bought yet "
            "produces no suggestion however empty the shelf gets. Zero is off, "
            "which is the sensible global default — a floor that suits abayas "
            "will be wrong for cartons. Set it per item on the item instead."
        ),
    ),
    Knob(
        key="demand_window_weeks",
        group="When to buy",
        label="Measure demand over",
        kind="float",
        unit="weeks of sales",
        minimum=1,
        maximum=104,
        help=(
            "How far back sales are read when no forecast has been typed for an "
            "item. Long enough to survive a quiet fortnight, short enough that "
            "last season stops voting on this one. Weeks the item was out of "
            "stock are taken out of the divisor, so a line that sold out does "
            "not read as a slow mover."
        ),
    ),
    # ---------------------------------------------------------- who may spend
    Knob(
        key="approval_threshold_sar",
        group="Who may spend",
        label="Needs an approver at or above",
        kind="float",
        unit="SAR per order",
        minimum=0,
        maximum=100_000_000,
        help=(
            "An order worth this or more cannot be sent until a named person "
            "approves it. The value is the whole order, not the line, because "
            "orders are consolidated one per supplier and the money leaves the "
            "business as one commitment. A second gate is not configurable: a "
            "supplier who has never completed an order always needs approval, "
            "whatever the value, because their first shipment is the one most "
            "likely to go wrong."
        ),
    ),
    # ------------------------------------------------------ chasing suppliers
    Knob(
        key="ack_reminder_hours",
        group="Chasing suppliers",
        label="Chase for an acknowledgement after",
        kind="int",
        unit="hours",
        minimum=1,
        maximum=720,
        help=(
            "How long a supplier may sit on an order before silence becomes an "
            "exception. Counted in real hours, but the chase itself is queued "
            "for their next working hour, so a deadline that expires at 2am in "
            "Guangzhou is not sent into the night."
        ),
    ),
    Knob(
        key="eta_slip_days",
        group="Chasing suppliers",
        label="A promised date may move by",
        kind="int",
        unit="days",
        minimum=0,
        maximum=365,
        help=(
            "A confirmed date moving less than this is recorded as an update. "
            "Moving more raises a date slip exception with a suggested action, "
            "because at some point a later delivery stops being news and starts "
            "being a decision somebody has to make."
        ),
    ),
    # -------------------------------------------------------- arrival estimates
    Knob(
        key="customs_clearance_days",
        group="Arrival estimates",
        label="Allow for customs",
        kind="int",
        unit="days",
        minimum=0,
        maximum=90,
        help=(
            "A flat allowance added to arrival estimates for imported goods, "
            "and stated as an assumption wherever the estimate is shown rather "
            "than folded into one confident date. It stays flat until enough "
            "receipts exist to measure a real figure per lane."
        ),
    ),
    Knob(
        key="weather_advisory",
        group="Arrival estimates",
        label="Warn about weather at the origin",
        kind="bool",
        help=(
            "Fetches a real forecast for the supplier's country and shows a "
            "warning beside the arrival estimate. It never adds days to the "
            "estimate — a date quietly moved by the weather is a date nobody "
            "can reconcile with the supplier's own promise."
        ),
    ),
    # -------------------------------------------------------------- where we sit
    Knob(
        key="home_timezone",
        group="Where we sit",
        label="The buying team works in",
        kind="timezone",
        help=(
            "Where the buyers are, as an IANA name such as Asia/Riyadh. Every "
            "supplier keeps their own hours on their own record; this is the "
            "clock the overlap between the two is measured against, and the one "
            "\"they open in 40 minutes\" is counted from."
        ),
    ),
)

KNOBS_BY_KEY: dict[str, Knob] = {knob.key: knob for knob in KNOBS}

# Order matters: it is the order of the page. Buying policy first because it is
# what gets tuned; the timezone last because it is set once at install.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(knob.group for knob in KNOBS))


def cross_check(values: dict[str, object]) -> None:
    """Rules that involve two settings at once.

    Only one so far, and it is the one that matters: buying up to less cover
    than triggers the buy produces an order that arrives already below the
    reorder point, so the next sweep suggests the same line again. That is not a
    misconfiguration anybody spots in the numbers — it shows up weeks later as a
    supplier being ordered from every single day.
    """
    reorder = values.get("reorder_cover_weeks")
    target = values.get("target_cover_weeks")
    if reorder is None or target is None:
        return
    if float(target) <= float(reorder):
        raise SettingError(
            "target_cover_weeks",
            f"Buy up to ({_plain(float(target))} weeks) must be more than reorder below "
            f"({_plain(float(reorder))} weeks), or every order would arrive already "
            "below the reorder point and the same line would be suggested again.",
        )
