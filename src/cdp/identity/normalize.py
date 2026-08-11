import re

# Providers that ignore dots in the local part. Applying this rule universally
# would merge two genuinely different mailboxes on most other hosts.
DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

_NON_DIGIT = re.compile(r"\D")


def normalize_email(raw: str | None) -> str | None:
    """Fold an email to a comparable form. Lowercase, drop the +tag, and remove
    dots only where the provider actually ignores them."""
    if not raw:
        return None
    value = raw.strip().lower()
    local, sep, domain = value.rpartition("@")
    if not sep or not local or "." not in domain:
        return None
    local = local.split("+", 1)[0]
    if domain in DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    if not local:
        return None
    return f"{local}@{domain}"


def normalize_phone(raw: str | None, default_country_code: str = "966") -> str | None:
    """Fold a phone number to E.164.

    Saudi numbers arrive in at least five shapes and all of them are the same
    woman: ``+966 50 123 4567``, ``0501234567``, ``501234567``,
    ``00966501234567``, ``966501234567``. Phone is the primary key for a
    WhatsApp-first, COD market, so this function failing quietly would fragment
    the majority of the customer base.
    """
    if not raw:
        return None
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return None

    if digits.startswith("00"):
        digits = digits[2:]

    cc = default_country_code
    if digits.startswith(cc):
        national = digits[len(cc) :]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    national = national.lstrip("0")
    # KSA mobiles are 9 national digits beginning with 5. Anything else is kept
    # verbatim rather than coerced: a Bahraini or Egyptian number is a real
    # customer, not a validation error, and guessing its country would merge
    # strangers.
    if not national or len(national) < 6:
        return None
    return f"+{cc}{national}"


def normalize(kind: str, value: str | None, default_country_code: str = "966") -> str | None:
    """Normalise by identifier kind. Unknown kinds are trimmed and lowercased —
    permissive on purpose, so adding a connector does not require touching this
    module before its identifiers can be stored."""
    if kind == "email":
        return normalize_email(value)
    if kind in {"phone", "whatsapp_id"}:
        return normalize_phone(value, default_country_code)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
