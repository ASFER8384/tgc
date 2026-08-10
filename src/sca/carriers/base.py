"""Carrier tracking behind one interface.

Aramex, DHL, SMSA and a freight forwarder's spreadsheet all answer the same
question in different shapes. The rest of the system asks this protocol, so
adding a carrier is a class, not a change to the ordering logic.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol


@dataclass
class TrackingResult:
    status: str
    eta: date | None = None
    events: list[dict] = field(default_factory=list)


class Carrier(Protocol):
    name: str

    async def track(self, tracking_number: str) -> TrackingResult: ...


class MockCarrier:
    """A working carrier that invents nothing at random.

    The status is derived from the tracking number, so a demo is repeatable and a
    test can assert on it. Randomness here would make every failure unreproducible.
    """

    name = "mock"

    STATUSES = ("booked", "collected", "in_transit", "customs", "out_for_delivery", "delivered")

    async def track(self, tracking_number: str) -> TrackingResult:
        seed = sum(ord(c) for c in tracking_number)
        status = self.STATUSES[seed % len(self.STATUSES)]
        days_out = (seed % 12) + 1
        eta = (datetime.now(UTC) + timedelta(days=days_out)).date()
        if status == "delivered":
            eta = (datetime.now(UTC) - timedelta(days=1)).date()
        return TrackingResult(
            status=status,
            eta=eta,
            events=[
                {"status": s, "note": f"{s.replace('_', ' ')} recorded by {self.name}"}
                for s in self.STATUSES[: self.STATUSES.index(status) + 1]
            ],
        )


CARRIERS: dict[str, Carrier] = {"mock": MockCarrier()}


def get_carrier(name: str) -> Carrier:
    carrier = CARRIERS.get(name)
    if carrier is None:
        raise KeyError(f"unknown carrier {name!r}, have {sorted(CARRIERS)}")
    return carrier
