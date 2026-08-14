# TGC Supplier Coordination

Purchase orders raised from forecast, sent when the supplier is actually awake,
replies read automatically, and everything the rules cannot settle raised as an
exception with a suggested action. For **Aleena**, **Rawash** and **Aynola**.

Today a buyer in Riyadh emails a mill in Guangzhou at 4pm, which is midnight
there. The reply arrives at 6am Riyadh and is read at 9. A single question costs
two days, and the only defence anyone has is checking a mailbox at odd hours.
This service holds the schedule, the chasing and the reading, and asks a person
only when the answer matters or the machine is unsure.

It is **not** an ERP, a replacement for email, or a portal suppliers must log in
to. Suppliers keep replying to a normal email address.

## Status: base build

What works end to end today:

- **Reorder planning** from stock and forecast, in weeks of cover, respecting
  minimum order quantity, pack size and the supplier's own lead time.
- **A floor in units** per item, for the lines cover cannot speak to: an item
  with no forecast and no sales history produces no suggestion however empty the
  shelf gets, and this is what makes "never fewer than fifty" expressible.
- **Buying policy on a page**, at `/procure?view=settings`: the reorder point,
  the approval threshold, the chase window and the rest, changed by a buyer
  without a deployment and with every value saying where it came from.
- **Purchase orders** with a checked lifecycle, consolidated one per supplier.
- **Approval gates**: value over a threshold, or a supplier who has never
  completed an order, cannot leave the building without a named approver.
- **Timezone aware sending**: an order approved at 6pm Riyadh is queued for the
  supplier's next working hour rather than landing overnight.
- **Reply parsing**: purchase order number, promised date, quantities and
  amounts, with a confidence score and a hard rule that anything unclear is
  filed for a human rather than guessed.
- **Exceptions with actions**: date slips, price mismatches, short shipments,
  silence from a supplier, and messages the parser could not read.
- **Shipment tracking** behind a carrier interface, with a working mock.
- **Console** at `/`: supplier clocks, what is below cover, orders, exceptions,
  and a box to paste a supplier email into and watch it be read.

Deliberately not here yet: see [Roadmap](#roadmap).

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # POSIX: .venv/bin/pip

cp .env.example .env
docker compose up -d                              # postgres :5434, redis :6381
.venv/Scripts/python -m alembic upgrade head

.venv/Scripts/python -m uvicorn sca.main:app --reload --port 8001
.venv/Scripts/python -m scripts.seed_demo         # four suppliers, four time zones
```

Console at `http://localhost:8001/`, API reference at `/docs`.

```bash
.venv/Scripts/python -m pytest -q                 # 285 tests, no container needed
.venv/Scripts/python -m ruff check src tests scripts
```

The suite runs on SQLite so it needs nothing running. `tests/test_schema.py`
executes the migration and compares it with the models, so the two cannot drift
apart unnoticed.

Ports are 8001, 5434 and 6381 rather than the CDP's 8000, 5433 and 6380, because
the two services are expected to run side by side.

## The three demo moments

1. **The clock wall** — four suppliers, four local times, two of them closed.
   Create draft orders and watch them queue for each supplier's next working
   hour instead of being sent into the night.
2. **Read the email** — paste a supplier reply into the console. A clean
   confirmation updates the order. A polite paragraph that buries the word
   delayed is read as a delay, not a confirmation, and the date slip is raised.
3. **Refuse to guess** — paste a vague reply. Confidence drops below the
   threshold, nothing is changed, and it appears under "Needs a human".

## Architecture

```
Inventory + forecast ──► planning ──► purchase orders ──► scheduled send
                          (cover)      (approval gates)   (supplier hours)
                                              │
Supplier email ──► parser ──► inbound ────────┤
Carrier APIs ────► tracking ──────────────────┤
                                              ▼
                                  exceptions with suggested actions
```

| Layer | Module | Notes |
|---|---|---|
| Planning | `sca/planning/` | Weeks of cover against lead time. Consumes the forecast, never invents demand. |
| Orders | `sca/orders/` | Lifecycle, approval policy, chasing, receiving. |
| Scheduling | `sca/scheduling/` | Working hours arithmetic, including overlap between two parties. |
| Reading | `sca/inbound/` | Deterministic extraction, then a confidence gate. Raw message stored verbatim. |
| Carriers | `sca/carriers/` | One protocol, a working mock, real carriers are a class each. |
| Policy | `sca/settings/` | Which numbers a buyer may change, and what a legal value is. Environment defaults, database overrides. |
| API | `sca/api/` | HTTP only. Routers call services. |

### Decisions worth knowing

**Working hours are data, not a constant.** A Gulf supplier rests Friday and
Saturday, a Chinese mill rests Sunday. One hardcoded weekend would have the
Riyadh printer closed on their busiest day. Every send and chase is scheduled
against per supplier columns.

**The parser is rules first, deliberately.** Rules are testable, auditable and
free, and they already cover the sentences suppliers actually send. The raw
message is stored verbatim, so putting a language model on top later means
replaying the mailbox rather than asking anyone to resend a year of
confirmations. Delay is checked before acknowledgement, because "we confirm the
order, shipment will be delayed" is a delay, and reading it as a confirmation
hides a late order until it is too late to react.

**Below a confidence threshold, nothing happens.** An automation that acts on a
guess is worse than one that asks. Unreadable messages become a low severity
exception with the original text attached.

**Approval gates exist for money that cannot be recovered.** Value is the obvious
gate. The second is a supplier who has never completed an order: their first
shipment is the most likely to go wrong and should never be triggered
automatically.

**Cover is the trigger; a units floor is the safety net.** Weeks of cover is the
better rule and stays primary: it knows how fast a thing sells and how long the
mill takes, where "below 50" knows neither and goes stale every season. But it
needs a demand figure to divide by, so a new line nobody has bought yet produces
nothing at all. `Item.min_stock` fills exactly that gap — it fires with no
forecast, it cannot be overruled by healthy cover, and the quantity ordered is
the larger of the two rules so a line tripping both does not trigger again the
following week. Blank inherits the global default, and a zero is a decision that
this line has no floor.

**Policy lives in the database, defaults live in the environment.** Everything a
buyer has authority over — the reorder point, the approval threshold, the chase
window — was previously only an environment variable, which made raising a
threshold a deployment and made the current one invisible to the people it
governs. Overrides are now rows, resolved per request over the environment, so
the page says of every value whether it is still the deployed default or
somebody's decision and who made it. Credentials, the database URL and the
switch that serves the API key into the console are deliberately not on that
page: a policy table that can rewrite the API key is one a browser can lock the
service out of its own data with.

**Every exception carries a suggested action.** "No acknowledgement in 36 hours"
is a report. "Chase them, they open in 40 minutes" is the product.

**Every automated action is audited.** An agent acting on your behalf is only
acceptable if everything it did can be listed afterwards.

## Known gaps in this build

- A reply can acknowledge an order that is still queued to be sent. Defensible
  when a buyer has emailed the supplier by hand, but it should be flagged rather
  than silently accepted.
- Sending is recorded, not performed: no SMTP or supplier API is wired yet.
- The sweep runs when called, from the console button or a cron. No scheduler.
- One shared API key. Buying approval in particular needs real named users, and
  it is the same gap on the settings page: every policy change is audited
  against "api-key-user", which records that somebody moved the approval
  threshold without recording who.

## Roadmap

**Next** — real mailbox polling (Microsoft 365, Google Workspace, IMAP) ·
outbound email with the order attached · a language model pass for the messages
the rules score low · real carrier connectors (Aramex, DHL, SMSA) · scheduled
sweeps · ERP or accounting sync for goods receipt and invoices.

**Later** — supplier scorecards on lead time reliability · landed cost including
freight and duty · multi currency exposure · EDI for suppliers who have it ·
approval routing by value band and role.

**Explicitly out of scope for now** — supplier portals, demand forecasting
itself, automated payment.

## Open questions for TGC

1. Which ERP or accounting system holds purchase orders today, and is it the
   book of record or is email?
2. Microsoft 365 or Google Workspace, and can we have a shared buying mailbox?
3. Which carriers and freight forwarders, and do they have tracking APIs?
4. Who approves, and at what value? The threshold in this build is a placeholder.
5. Do any suppliers use EDI or their own portal, and are they big enough to
   justify a bespoke connector?
