"""Working hours arithmetic.

The single most useful thing this service does is refuse to send at the wrong
hour. An order that lands at 3am in Guangzhou is read the next afternoon and the
reply arrives after Riyadh has gone home, which is how a two day round trip
becomes four. Everything here answers one of two questions: is anyone there right
now, and if not, when.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_LOOKAHEAD_DAYS = 14


@dataclass(frozen=True)
class WorkingHours:
    timezone: str
    # ISO weekday numbers, Monday is 1 through Sunday is 7. A Gulf supplier
    # resting Friday and Saturday and a Chinese mill resting Saturday and Sunday
    # cannot share a hardcoded weekend, so this is data, not a constant.
    working_days: tuple[int, ...] = (1, 2, 3, 4, 5)
    start_hour: int = 9
    end_hour: int = 17

    @classmethod
    def from_supplier(cls, supplier) -> "WorkingHours":
        days = tuple(
            int(part) for part in str(supplier.working_days).split(",") if part.strip().isdigit()
        )
        return cls(
            timezone=supplier.timezone,
            working_days=days or (1, 2, 3, 4, 5),
            start_hour=supplier.work_start_hour,
            end_hour=supplier.work_end_hour,
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def local_now(hours: WorkingHours, now: datetime) -> datetime:
    return _aware(now).astimezone(hours.zone)


def is_open(hours: WorkingHours, now: datetime) -> bool:
    here = local_now(hours, now)
    if here.isoweekday() not in hours.working_days:
        return False
    return hours.start_hour <= here.hour < hours.end_hour


def next_open(hours: WorkingHours, now: datetime) -> datetime:
    """The next instant the supplier is at their desk, in UTC.

    Returns `now` when they are already open, so callers can treat "send now" and
    "send later" as the same code path rather than branching at every call site.
    """
    if is_open(hours, now):
        return _aware(now)

    here = local_now(hours, now)
    # Today, if the working day has not started yet.
    if here.isoweekday() in hours.working_days and here.hour < hours.start_hour:
        candidate = here.replace(
            hour=hours.start_hour, minute=0, second=0, microsecond=0
        )
        return candidate.astimezone(UTC)

    for offset in range(1, MAX_LOOKAHEAD_DAYS + 1):
        day = (here + timedelta(days=offset)).date()
        probe = datetime.combine(day, time(hour=hours.start_hour), tzinfo=hours.zone)
        if probe.isoweekday() in hours.working_days:
            return probe.astimezone(UTC)

    # A supplier with no working days configured would loop forever otherwise.
    # Falling back to "now" is wrong quietly; raising is wrong loudly, which is
    # the better failure for a scheduling bug.
    raise ValueError(f"no working day found within {MAX_LOOKAHEAD_DAYS} days for {hours.timezone}")


def hours_until_open(hours: WorkingHours, now: datetime) -> float:
    return max(0.0, (next_open(hours, now) - _aware(now)).total_seconds() / 3600)


def working_hours_between(hours: WorkingHours, start: datetime, end: datetime) -> float:
    """How many of the supplier's own working hours passed between two instants.

    A deadline measured in wall clock hours punishes a supplier for their own
    night and their own weekend: an order sent as Istanbul closes on Friday is
    twenty four hours old on Saturday, when nobody was ever there to read it.
    Chasing then is wrong, and it is the kind of wrong that teaches a buyer to
    ignore the exception list.
    """
    start, end = _aware(start), _aware(end)
    if end <= start:
        return 0.0

    step = timedelta(minutes=30)
    # Beyond the lookahead the answer stops being useful — an order this stale is
    # already past every threshold, and counting further only costs time.
    stop = min(end, start + timedelta(days=MAX_LOOKAHEAD_DAYS * 2))
    probe, counted = start, 0.0
    while probe < stop:
        # Only the part of this half hour that falls inside the interval. Adding
        # the whole step made a reply that arrived in one minute count as thirty,
        # which reported more working time than elapsed time — impossible, and
        # obviously so on any dashboard showing both.
        following = min(probe + step, stop)
        if is_open(hours, probe):
            counted += (following - probe).total_seconds() / 3600
        probe = following
    return counted


def overlap_hours(a: WorkingHours, b: WorkingHours, now: datetime) -> float:
    """How many hours today both parties are at their desks at the same time.

    Zero is the number that explains the project: with no overlap, every question
    costs a full day, and the only fix is to stop needing a live conversation.
    """
    day = local_now(a, now).date()
    shared = 0.0
    probe = datetime.combine(day, time(0, 30), tzinfo=a.zone)
    for _ in range(48):
        if is_open(a, probe) and is_open(b, probe):
            shared += 0.5
        probe += timedelta(minutes=30)
    return shared


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
