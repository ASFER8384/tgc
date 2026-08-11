"""Turn a supplier email into structured facts.

Deterministic first, on purpose. Rules are testable, auditable and free, and they
already cover the sentences suppliers actually send: a PO number, a date, a
quantity, a price. A language model belongs on top of this, for the messages the
rules cannot classify, and the raw message is stored verbatim so adding it later
means replaying the mailbox rather than asking anyone to resend.

Nothing here writes to the database. It reads text and returns what it found,
with a confidence, so the caller decides whether that is enough to act on.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

PO_PATTERN = re.compile(r"\bPO[-\s]?(\d{4,8})\b", re.IGNORECASE)
QTY_PATTERN = re.compile(r"\b(\d{1,6})\s*(?:pcs|pieces|units|yards|metres|meters|m)\b", re.I)
# Currency either side of the number: a supplier writing "88,800 CNY" means the
# same as "CNY 88,800", and only one of the two was being read. The currency is
# still required, so a quantity or a date fragment is never mistaken for money.
MONEY_PATTERN = re.compile(
    r"(?:\b(?:SAR|USD|CNY|EUR)\s?([\d,]+(?:\.\d{1,2})?)\b"
    r"|\b([\d,]+(?:\.\d{1,2})?)\s?(?:SAR|USD|CNY|EUR)\b)",
    re.I,
)

ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
DMY_DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b")
TEXT_DATE = re.compile(
    r"\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(20\d{2})?\b",
    re.IGNORECASE,
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}

# Word lists, ordered by how strongly they decide the message kind. Delay is
# checked before acknowledgement because "we confirm the order, shipment will be
# delayed" is a delay, not a clean confirmation.
KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delay", ("delay", "delayed", "postpone", "push back", "behind schedule", "cannot meet",
               "unable to ship", "reschedul")),
    ("invoice", ("invoice", "proforma", "payment due", "bank details", "remittance")),
    ("packing_list", ("packing list", "packing slip", "carton list", "shipping marks")),
    # Stems rather than whole words: a supplier writes "we accept", "accepted",
    # "acceptance is confirmed" and means the same thing each time, and a list of
    # exact forms only ever covers the ones somebody thought of. "accept" alone
    # missed "I accept this order", which is about as plain as agreement gets.
    ("acknowledgement", ("confirm", "acknowledg", "accept", "agree", "we will ship",
                         "order received", "booked", "will proceed", "proceeding with")),
    ("quote", ("quotation", "quote", "price list", "offer")),
    ("question", ("please clarify", "could you", "question", "confirm the address", "?" )),
)


@dataclass
class Extraction:
    kind: str = "unknown"
    po_number: str | None = None
    promised_date: date | None = None
    quantities: list[int] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "po_number": self.po_number,
            "promised_date": self.promised_date.isoformat() if self.promised_date else None,
            "quantities": self.quantities,
            "amounts": self.amounts,
            "confidence": round(self.confidence, 2),
        }


# Where a reply stops being the supplier writing and starts being our own email
# quoted back at us. Every mail client marks the boundary differently, and none
# of them agree.
QUOTE_MARKERS = (
    # The attribution line wraps. Gmail routinely breaks it before "wrote:", and
    # a pattern anchored to one line then leaves the date in it behind: "On Tue,
    # 11 Aug 2026 at 13:52, ..." was read as the promised delivery date.
    re.compile(r"^[ \t>]*On\b[\s\S]{0,240}?\bwrote:", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^\s*_{5,}\s*$", re.M),
    re.compile(r"^\s*From:\s.+$", re.M),
)


def strip_quoted(body: str) -> str:
    """Only what the supplier actually wrote this time.

    A reply carries our own order underneath it, and our own order states the
    total, the PO number and the delivery date we asked for. Reading those back
    as if the supplier had said them is how a price increase disappears: the
    quoted original agrees with our figure, so the disagreement above it never
    gets noticed.
    """
    text = body or ""
    cut = len(text)
    for marker in QUOTE_MARKERS:
        found = marker.search(text)
        if found:
            cut = min(cut, found.start())
    text = text[:cut]
    # Whatever survived, minus the conventional quote prefix. Some clients indent
    # rather than announce, so this catches what the markers above miss.
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )


def parse(subject: str | None, body: str, *, received_at: datetime | None = None) -> Extraction:
    text = f"{subject or ''}\n{strip_quoted(body)}"
    lowered = text.lower()

    out = Extraction()

    po = PO_PATTERN.search(text)
    if po:
        out.po_number = f"PO-{po.group(1)}"

    out.kind = _classify(lowered)
    out.promised_date = _first_date(text, received_at)
    out.quantities = [int(q) for q in QTY_PATTERN.findall(text)][:5]
    # Two capture groups, one per currency position; exactly one is filled.
    out.amounts = [
        float((before or after).replace(",", ""))
        for before, after in MONEY_PATTERN.findall(text)
    ][:5]

    # Confidence is deliberately crude and deliberately conservative. It exists to
    # decide "act" against "ask a human", not to be a probability.
    score = 0.0
    if out.po_number:
        score += 0.5
    if out.kind != "unknown":
        score += 0.3
    if out.promised_date:
        score += 0.2
    out.confidence = min(score, 1.0)
    return out


def _classify(lowered: str) -> str:
    for kind, words in KIND_RULES:
        if any(word in lowered for word in words):
            return kind
    return "unknown"


def _first_date(text: str, received_at: datetime | None) -> date | None:
    iso = ISO_DATE.search(text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    dmy = DMY_DATE.search(text)
    if dmy:
        # Day first. Suppliers writing to a Gulf buyer use day first far more often
        # than month first, and guessing the other way turns 5/8 into three months.
        return _safe_date(int(dmy.group(3)), int(dmy.group(2)), int(dmy.group(1)))

    text_date = TEXT_DATE.search(text)
    if text_date:
        day = int(text_date.group(1))
        month = MONTHS[text_date.group(2).lower()[:3]]
        year = int(text_date.group(3)) if text_date.group(3) else _implied_year(
            month, received_at
        )
        return _safe_date(year, month, day)
    return None


def _implied_year(month: int, received_at: datetime | None) -> int:
    """A bare "12 Aug" means the next 12 August, not one in the past."""
    if received_at is None:
        return date.today().year
    year = received_at.year
    return year + 1 if month < received_at.month else year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
