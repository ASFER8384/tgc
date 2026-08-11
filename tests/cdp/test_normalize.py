import pytest

from cdp.identity.normalize import normalize, normalize_email, normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "+966 50 123 4567",
        "0501234567",
        "501234567",
        "00966501234567",
        "966501234567",
        "+966-50-123-4567",
        "(966) 50 123 4567",
    ],
)
def test_every_saudi_format_folds_to_one_number(raw: str) -> None:
    # Phone is the primary key in a WhatsApp-first, COD market. If these do not
    # collapse, the majority of the customer base fragments.
    assert normalize_phone(raw) == "+966501234567"


def test_different_numbers_do_not_collapse() -> None:
    assert normalize_phone("0501234567") != normalize_phone("0501234568")


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12", None])
def test_unusable_phones_are_dropped_not_guessed(raw) -> None:
    assert normalize_phone(raw) is None


def test_gmail_dots_and_tags_are_folded() -> None:
    assert normalize_email("Noura.Al.Qahtani+shop@Gmail.com") == "nouraalqahtani@gmail.com"


def test_dots_are_significant_outside_gmail() -> None:
    # Folding dots everywhere would merge two genuinely different mailboxes.
    assert normalize_email("first.last@tgc-ksa.com") == "first.last@tgc-ksa.com"
    assert normalize_email("firstlast@tgc-ksa.com") != normalize_email("first.last@tgc-ksa.com")


@pytest.mark.parametrize("raw", ["not-an-email", "@gmail.com", "a@b", "", None])
def test_malformed_emails_are_dropped(raw) -> None:
    assert normalize_email(raw) is None


def test_unknown_kinds_pass_through_trimmed() -> None:
    assert normalize("shopify_customer_id", " 9001 ") == "9001"
    assert normalize("device_id", "") is None
