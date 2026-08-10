"""Reading supplier email, written the way suppliers write it."""

from datetime import UTC, date, datetime

from sca.inbound.parser import parse

RECEIVED = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def test_clean_confirmation():
    found = parse(
        "Re: PO-5001 - Order Confirmation",
        "We confirm the order and will ship on 2026-09-28.",
        received_at=RECEIVED,
    )
    assert found.kind == "acknowledgement"
    assert found.po_number == "PO-5001"
    assert found.promised_date == date(2026, 9, 28)
    assert found.confidence == 1.0


def test_delay_wins_over_a_polite_confirmation():
    # The sentence contains "confirm", but the message is a delay. Reading this as
    # a clean acknowledgement is the expensive mistake: it hides a late order.
    found = parse(
        "RE: PO-5002",
        "We have received and confirm PO-5002, however our line is booked and this "
        "will be delayed. New ship date 12 Nov 2026.",
        received_at=RECEIVED,
    )
    assert found.kind == "delay"
    assert found.promised_date == date(2026, 11, 12)


def test_invoice_amount_is_extracted():
    found = parse("Invoice for PO-5003", "Please find our invoice for USD 9,850.00.",
                  received_at=RECEIVED)
    assert found.kind == "invoice"
    assert found.amounts == [9850.0]


def test_day_first_dates_are_not_read_as_month_first():
    # A supplier writing to a Gulf buyer means 5 August, not 8 May.
    found = parse("PO-5004", "Ready on 5/8/2026.", received_at=RECEIVED)
    assert found.promised_date == date(2026, 8, 5)


def test_bare_month_implies_the_next_occurrence():
    found = parse("PO-5005", "Shipping 3 Feb.", received_at=RECEIVED)
    assert found.promised_date == date(2027, 2, 3)


def test_quantities_are_picked_up():
    found = parse("PO-5006", "We will send 500 pcs first and 300 units later.",
                  received_at=RECEIVED)
    assert found.quantities == [500, 300]


def test_unreadable_message_is_low_confidence_and_unknown():
    found = parse("شكرا", "Thanks for the file, we will revert.", received_at=RECEIVED)
    assert found.kind == "unknown"
    assert found.po_number is None
    assert found.confidence < 0.7


def test_impossible_date_does_not_crash():
    found = parse("PO-5007", "Ready 31/02/2026.", received_at=RECEIVED)
    assert found.promised_date is None
