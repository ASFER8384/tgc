"""The Saudi trading year, as a fashion business actually experiences it.

A seasonal index built on calendar months is measuring the wrong thing here. The
two dates that move this business — Ramadan and the Eids — follow the Hijri
calendar and fall about eleven days earlier each Gregorian year:

    Eid al-Fitr   2024-04-09    2025-03-30    2026-03-19

So "March" is a Ramadan month in one year and an ordinary one in another, and
averaging them produces an index that is wrong in both. The peak the data shows
at 2.5x is not a March effect; it is the fortnight before Eid, and next year it
will be in February.

This classifies every day into the regime it belongs to, from the real dates
rather than from a rule of thumb. ``holidays`` supplies them offline with no key
and no network call, which is what makes this usable inside a forecast that has
to run on a schedule.

The buckets are chosen for what a clothing business feels, not for what a
calendar prints:

- The last ten days of Ramadan are when Eid clothes are actually bought.
- The fortnight before that is when they are chosen and ordered.
- Eid itself is holiday trade — different, and not always smaller.
- Hajj and Eid al-Adha are a second, shorter peak.
- National Day and Founding Day are promotional weeks the whole market runs.
- July and August are the summer trough: heat, and everybody travelling.

Nothing here is fitted. These are date ranges; the *multiplier* for each one is
measured from the trading history in ``plan.py``, which is the part that should
be learned rather than assumed.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import holidays

# The regimes, in the order they are tested. First match wins, so the narrow and
# more strongly felt windows are listed before the broad ones — a day that is
# both "in summer" and "Eid al-Adha" is an Eid day, because that is what it
# behaves like.
RAMADAN_PEAK = "ramadan_peak"
RAMADAN_EARLY = "ramadan_early"
EID_FITR = "eid_fitr"
POST_EID = "post_eid"
EID_ADHA = "eid_adha"
NATIONAL_DAY = "national_day"
FOUNDING_DAY = "founding_day"
SUMMER = "summer"
NORMAL = "normal"

LABELS = {
    RAMADAN_PEAK: "Last 10 days of Ramadan",
    RAMADAN_EARLY: "Early Ramadan",
    EID_FITR: "Eid al-Fitr",
    POST_EID: "The fortnight after Eid",
    EID_ADHA: "Hajj and Eid al-Adha",
    NATIONAL_DAY: "National Day",
    FOUNDING_DAY: "Founding Day",
    SUMMER: "Summer lull",
    NORMAL: "Ordinary trading",
}

# Display order, roughly how the year runs rather than alphabetical.
ORDER = [
    RAMADAN_EARLY, RAMADAN_PEAK, EID_FITR, POST_EID, EID_ADHA,
    NATIONAL_DAY, FOUNDING_DAY, SUMMER, NORMAL,
]


@lru_cache(maxsize=8)
def _saudi(first_year: int, last_year: int) -> dict[date, str]:
    """The published Saudi holiday dates for a span of years, in English.

    Cached because the underlying library computes Hijri conversions on
    construction and this is called per day of a two year history.
    """
    return dict(
        holidays.SaudiArabia(
            years=range(first_year, last_year + 1), language="en_US"
        ).items()
    )


@lru_cache(maxsize=8)
def _anchors(first_year: int, last_year: int) -> tuple[tuple[date, ...], tuple[date, ...]]:
    """The first day of each Eid, which is what every window is measured from.

    The library publishes several consecutive days per Eid — the holiday itself
    plus the observed days that follow a weekend. Only the first is an anchor;
    the rest are the holiday, and treating each as its own Eid would stamp four
    overlapping windows onto one event.
    """
    fitr: list[date] = []
    adha: list[date] = []
    for day, name in sorted(_saudi(first_year, last_year).items()):
        lowered = name.lower()
        if "fitr" in lowered:
            target = fitr
        elif "adha" in lowered or "arafah" in lowered:
            target = adha
        else:
            continue
        # A date within a week of the previous one belongs to the same Eid.
        if target and (day - target[-1]).days <= 7:
            continue
        target.append(day)
    return tuple(fitr), tuple(adha)


def regime_of(day: date, *, first_year: int, last_year: int) -> str:
    """Which trading regime a single day belongs to."""
    fitr, adha = _anchors(first_year, last_year)

    for eid in fitr:
        delta = (day - eid).days
        # Ramadan is the lunar month before Eid al-Fitr, so it is derived from
        # the Eid date rather than looked up separately — one published date
        # anchors the whole window, and there is no second source to disagree.
        if -10 <= delta <= -1:
            return RAMADAN_PEAK
        if -29 <= delta <= -11:
            return RAMADAN_EARLY
        if 0 <= delta <= 4:
            return EID_FITR
        if 5 <= delta <= 18:
            return POST_EID

    for eid in adha:
        delta = (day - eid).days
        if -7 <= delta <= 4:
            return EID_ADHA

    if day.month == 9 and 20 <= day.day <= 25:
        return NATIONAL_DAY
    if day.month == 2 and 20 <= day.day <= 25:
        return FOUNDING_DAY
    if day.month in (7, 8):
        return SUMMER
    return NORMAL


def days_in(year: int, month: int) -> list[date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return [start + timedelta(days=n) for n in range((end - start).days)]


def regimes_for_month(year: int, month: int, *, first_year: int, last_year: int) -> dict[str, int]:
    """How many days of the month fall in each regime.

    A month is rarely one thing. March 2026 holds the end of Ramadan, Eid itself
    and the days after it, and its expected trade is those three added up rather
    than any one of them — which is exactly what a month-level index cannot say.
    """
    counts: dict[str, int] = {}
    for day in days_in(year, month):
        key = regime_of(day, first_year=first_year, last_year=last_year)
        counts[key] = counts.get(key, 0) + 1
    return counts


def calendar_notes(year: int, month: int, *, first_year: int, last_year: int) -> list[str]:
    """What is in this month, in words, for a person reading the plan."""
    fitr, adha = _anchors(first_year, last_year)
    out: list[str] = []
    for eid in fitr:
        if (eid.year, eid.month) == (year, month):
            out.append(f"Eid al-Fitr falls on {eid.isoformat()}.")
        elif eid > date(year, month, 1) and (eid - date(year, month, 1)).days <= 45:
            out.append(f"Ramadan buying runs into this month — Eid al-Fitr is {eid.isoformat()}.")
    for eid in adha:
        if (eid.year, eid.month) == (year, month):
            out.append(f"Eid al-Adha falls on {eid.isoformat()}.")
    if month == 9:
        out.append("National Day on the 23rd — the whole market discounts that week.")
    if month == 2:
        out.append("Founding Day on the 22nd.")
    if month in (7, 8):
        out.append("Summer: the heat and the travel season, historically the year's trough.")
    return out
