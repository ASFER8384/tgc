"""Planning, purchase orders, shipments and exceptions."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from sca.api.deps import ActorDep, RuntimeSettingsDep, SessionDep
from sca.carriers.base import get_carrier
from sca.models import (
    Attachment,
    AuditLog,
    Document,
    InboundMessage,
    Issue,
    PurchaseOrder,
    PurchaseOrderLine,
    SentMessage,
    Shipment,
    Supplier,
)
from sca.orders.compose import compose_order_email
from sca.orders.service import OrderError, OrderInputError, OrderService
from sca.planning.service import PlanningService

router = APIRouter(tags=["orders"])


class LineIn(BaseModel):
    sku: str
    description: str | None = None
    quantity: int
    unit_price: Decimal
    # The size split, where the caller has one. Absent stays absent: no split is
    # not an even split, and a mill told nothing about sizes will ask rather than
    # guess. Carried on the line rather than a parallel map so that a revision
    # cannot move a quantity and leave a curve behind that no longer adds to it.
    sizes: dict[str, int] | None = None


class OrderIn(BaseModel):
    supplier_id: str
    lines: list[LineIn]
    origin: str = "manual"


class ApproveIn(BaseModel):
    approver: str = "buyer"


class ReceiveIn(BaseModel):
    # sku to quantity actually counted in. Anything omitted is treated as
    # received in full, which is the common case and keeps the payload short.
    received: dict[str, int] = {}
    # Whether to mail the supplier a receipt note. Off, because booking goods in
    # is a warehouse act and should not write to somebody outside the building
    # unless a person said to. The note is still available on its own endpoint.
    notify: bool = False


class ShipmentIn(BaseModel):
    carrier: str = "mock"
    tracking_number: str


class SendIn(BaseModel):
    # An edited message. Composed text is a starting point, not a rule: the buyer
    # who is mid negotiation knows things the template does not.
    subject: str | None = None
    body: str | None = None


class ReviseIn(BaseModel):
    lines: list[LineIn]
    reason: str


class CancelIn(BaseModel):
    reason: str


# ------------------------------------------------------------------- planning
@router.post("/planning/suggest")
async def suggest(
    session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    suggestions = await PlanningService(session, settings=settings).suggest()
    grouped: dict[str, list[dict]] = {}
    for suggestion in suggestions:
        grouped.setdefault(suggestion.supplier_id, []).append(suggestion.as_dict())
    suppliers = {s.id: s.name for s in await session.scalars(select(Supplier))}
    return {
        "count": len(suggestions),
        # The rule, sent rather than restated in the console. A buyer approving
        # spend is entitled to know what produced the number, and a copy of these
        # thresholds written into the page would drift the first time one is
        # tuned here — leaving the explanation confidently describing a policy
        # the system no longer follows.
        "policy": {
            "demand_window_weeks": settings.demand_window_weeks,
            "reorder_cover_weeks": settings.reorder_cover_weeks,
            "target_cover_weeks": settings.target_cover_weeks,
            # The floor in units, which fires with no forecast at all. Sent
            # for the same reason as the rest: the page explains the rule and
            # an explanation that hardcodes a number goes stale the first time
            # somebody tunes it on the settings page.
            "min_stock_default": settings.min_stock_default,
            # The arrival estimate's two configured terms, sent for the same
            # reason as the three above: the page explains them, and an
            # explanation that repeats a number rather than reading it goes
            # stale the first time somebody tunes it.
            "customs_clearance_days": settings.customs_clearance_days,
            "weather_advisory": settings.weather_advisory,
        },
        "by_supplier": [
            {
                "supplier_id": supplier_id,
                "supplier": suppliers.get(supplier_id, "unknown"),
                "lines": lines,
                "value": str(sum(Decimal(line["line_total"]) for line in lines)),
            }
            for supplier_id, lines in grouped.items()
        ],
    }


class CreateOrdersIn(BaseModel):
    # Which lines to buy. Empty means everything below cover, which is the
    # morning routine; naming one is the buyer who wants that line and not the
    # rest, usually because the rest is still being argued about.
    skus: list[str] = []
    # Where the buyer has overruled the ranking. Cheapest-for-the-quantity is
    # where the cursor starts, not a verdict: a line covering a launch is worth
    # paying more to get sooner, and that judgement is theirs.
    supplier_by_sku: dict[str, str] = {}
    # What the buyer typed over the suggestion. The forecast proposes; a person
    # who knows about the launch, the promotion or the container that is already
    # late decides. Absent means take the suggestion.
    quantity_by_sku: dict[str, int] = {}
    # The curve to cut, per line. A mill needs sizes, and the split the forecast
    # works out per shop is the best first answer — but it is a suggestion like
    # the quantity, so it arrives from the desk where it can be edited rather
    # than being recomputed here and quietly disagreeing with what was on screen.
    sizes_by_sku: dict[str, dict[str, int]] = {}


@router.post("/planning/create-orders", status_code=status.HTTP_201_CREATED)
async def create_from_suggestions(
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
    body: CreateOrdersIn | None = None,
) -> dict:
    """One draft per supplier, not one per line: a buyer sends an order, not a
    stream of individual requests, and consolidating is what earns the freight."""
    wanted = set(body.skus) if body and body.skus else None
    picked = dict(body.supplier_by_sku) if body else {}
    typed = dict(body.quantity_by_sku) if body else {}
    curves = dict(body.sizes_by_sku) if body else {}
    suggestions = await PlanningService(session, settings=settings).suggest()
    if wanted is not None:
        suggestions = [s for s in suggestions if s.sku in wanted]
    service = OrderService(session, actor=actor, settings=settings)
    grouped: dict[str, list[dict]] = {}
    for suggestion in suggestions:
        supplier_id = suggestion.supplier_id
        quantity = suggestion.suggest_quantity
        unit_price = suggestion.unit_cost
        code = picked.get(suggestion.sku)
        if code:
            # Their terms, not the ranked one's: switching supplier changes the
            # minimum order and the pack size, so carrying the old quantity
            # across would draft something they will reject.
            option = next((o for o in suggestion.options if o["code"] == code), None)
            if option is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"{code} does not supply {suggestion.sku}",
                )
            supplier_id = option["supplier_id"]
            quantity = option["quantity"]
            unit_price = Decimal(option["unit_cost"])
        # Last word to the person. Applied after the supplier switch, because
        # switching re-prices the line and a figure typed against the old
        # supplier is still the quantity they meant to buy.
        if suggestion.sku in typed:
            quantity = int(typed[suggestion.sku])
            if quantity < 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"{suggestion.sku}: a quantity cannot be negative",
                )
        if quantity == 0:
            # Typed to nothing is a decision not to buy this line, not an order
            # for zero of it.
            continue
        grouped.setdefault(supplier_id, []).append(
            {
                "sku": suggestion.sku,
                "description": suggestion.description,
                "quantity": quantity,
                "unit_price": unit_price,
                "sizes": curves.get(suggestion.sku),
            }
        )
    created = []
    for supplier_id, lines in grouped.items():
        try:
            order = await service.create(supplier_id, lines, origin="forecast")
        except OrderError as err:
            # A size split that disagrees with its quantity is the buyer's typing,
            # so it comes back as something to fix rather than a server fault.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
        created.append({
            "number": order.number,
            "supplier_id": supplier_id,
            "total_value": str(order.total_value),
            "status": order.status,
            "approval_reason": order.approval_reason,
        })
    return {"created": len(created), "orders": created}


# --------------------------------------------------------------------- orders
@router.post("/purchase-orders", status_code=status.HTTP_201_CREATED)
async def create_order(
    body: OrderIn, session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    try:
        order = await OrderService(session, actor=actor, settings=settings).create(
            body.supplier_id,
            [line.model_dump() for line in body.lines],
            origin=body.origin,
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _order_detail(session, order)


@router.get("/purchase-orders")
async def list_orders(session: SessionDep, actor: ActorDep, status_filter: str | None = None):
    query = select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc())
    if status_filter:
        query = query.where(PurchaseOrder.status == status_filter)
    orders = list(await session.scalars(query))
    suppliers = {s.id: s for s in await session.scalars(select(Supplier))}
    open_issues = {
        row.purchase_order_id
        for row in await session.scalars(select(Issue).where(Issue.status == "open"))
    }
    return [
        {
            "id": o.id,
            "number": o.number,
            "supplier": suppliers[o.supplier_id].name if o.supplier_id in suppliers else "unknown",
            "supplier_timezone": (
                suppliers[o.supplier_id].timezone if o.supplier_id in suppliers else None
            ),
            "status": o.status,
            "total_value": str(o.total_value),
            "currency": o.currency,
            "requires_approval": o.requires_approval,
            "approval_reason": o.approval_reason,
            "scheduled_send_at": o.scheduled_send_at,
            "sent_at": o.sent_at,
            "acknowledged_at": o.acknowledged_at,
            "expected_delivery_date": o.expected_delivery_date,
            "confirmed_delivery_date": o.confirmed_delivery_date,
            "has_open_issue": o.id in open_issues,
            "revision": o.revision,
        }
        for o in orders
    ]


@router.get("/purchase-orders/{number}")
async def get_order(number: str, session: SessionDep, actor: ActorDep) -> dict:
    order = await _by_number(session, number)
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/approve")
async def approve_order(
    number: str,
    body: ApproveIn,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor, settings=settings).approve(
            order, approver=body.approver
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/send")
async def send_order(
    number: str,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
    body: SendIn | None = None,
) -> dict:
    order = await _by_number(session, number)
    if order.status == "pending_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{order.number} needs approval first: {order.approval_reason}",
        )
    try:
        result = await OrderService(session, actor=actor, settings=settings).send(
            order,
            subject=body.subject if body else None,
            message=body.body if body else None,
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    detail = await _order_detail(session, order)
    return detail | {"delivery": result}


@router.get("/purchase-orders/{number}/message")
async def order_message(
    number: str, session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """The exact text that would go to the supplier, before anything is sent.

    A GET, deliberately: reading the message must never change the order. Until a
    mail connector exists this is also how the message actually reaches anyone —
    a buyer copies it — which makes it worth getting right rather than treating
    it as a preview.
    """
    order = await _by_number(session, number)
    supplier = await session.get(Supplier, order.supplier_id)
    if supplier is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "order has no supplier")
    lines = list(
        await session.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
        )
    )
    return compose_order_email(
        order, supplier, lines,
        ack_deadline_hours=settings.ack_reminder_hours,
        now=datetime.now(UTC),
    )


@router.get("/purchase-orders/{number}/thread")
async def order_thread(number: str, session: SessionDep, actor: ActorDep) -> dict:
    """Both halves of the exchange on one order, oldest first.

    Our letters and their replies interleaved, because reading either alone
    invites the wrong conclusion: a supplier who has answered twice looks silent
    if only outbound is shown, and one answering a withdrawn revision looks
    agreeable if only their words are.

    Letters sent before this was recorded are not reconstructed. The audit log
    knows one went out and to whom; it does not know what it said, and rendering
    today's figures under an old date would be inventing evidence.
    """
    order = await _by_number(session, number)
    supplier = await session.get(Supplier, order.supplier_id)

    entries: list[dict] = []
    for msg in await session.scalars(
        select(SentMessage).where(SentMessage.purchase_order_id == order.id)
    ):
        entries.append({
            "id": msg.id,
            "side": "ours",
            "at": msg.sent_at,
            "who": "Procurement",
            "to": msg.to_address,
            "subject": msg.subject,
            "body": msg.body,
            "kind": msg.kind,
            "revision": msg.revision,
            "delivered": msg.delivered,
            "failure": msg.failure,
        })

    for msg in await session.scalars(
        select(InboundMessage).where(InboundMessage.purchase_order_id == order.id)
    ):
        entries.append({
            "id": msg.id,
            "side": "theirs",
            "at": msg.received_at,
            "who": supplier.name if supplier else (msg.from_address or "Supplier"),
            "from": msg.from_address,
            "subject": msg.subject,
            "body": msg.body,
            "kind": msg.kind,
            "confidence": msg.confidence,
            # Read but not acted on: below the threshold the extractor files a
            # message for a person instead of moving the order, and a reader
            # deserves to know which of the two happened.
            "acted_on": msg.processed_at is not None,
        })

    entries.sort(key=lambda e: e["at"])

    # What the record cannot show. Counted as a difference rather than by
    # comparing timestamps: the audit row is written after the letter it
    # describes, so "older than the first letter kept" would miscount the very
    # send that started the keeping. Every send leaves an audit row; only the
    # ones since this table existed left a letter.
    sends = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity == "purchase_order",
            AuditLog.entity_id == order.id,
            AuditLog.action == "order.send",
        )
    )
    kept = sum(1 for e in entries if e["side"] == "ours" and e["kind"] != "receipt")
    unkept = max(0, (sends or 0) - kept)

    return {
        "number": order.number,
        "supplier": supplier.name if supplier else None,
        "supplier_email": supplier.email if supplier else None,
        "status": order.status,
        "revision": order.revision,
        "entries": entries,
        # Named plainly. "0 letters" and "we did not keep them" are different
        # things to read on a screen that claims to be a record.
        "not_kept": unkept,
    }


class MessagePreviewIn(BaseModel):
    # Lines as they stand in the editor, which is not what the database holds:
    # the whole point is to read the message before committing to the change.
    lines: list[LineIn] = []
    reason: str | None = None


@router.post("/purchase-orders/{number}/message")
async def preview_message(
    number: str,
    body: MessagePreviewIn,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    """The message as it would read if these figures were saved.

    Nothing is written. The draft is assembled as plain values rather than by
    editing the order, because touching the loaded object would let autoflush
    persist a revision the buyer has not agreed to yet.
    """
    order = await _by_number(session, number)
    supplier = await session.get(Supplier, order.supplier_id)
    if supplier is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "order has no supplier")

    if body.lines:
        lines = [
            SimpleNamespace(
                sku=line.sku,
                description=line.description or line.sku,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=(line.unit_price * line.quantity).quantize(Decimal("0.01")),
                # Required, not optional. The letter prints a size split when one
                # is there, and a stand-in line without the attribute raised
                # inside the composer — which the dialog caught and swallowed,
                # leaving the previous draft on screen looking like the current
                # one. A preview that silently stops updating is worse than no
                # preview: it reads as confirmation.
                sizes=line.sizes,
            )
            for line in body.lines
        ]
    else:
        lines = list(
            await session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )

    total = sum((Decimal(str(line.line_total)) for line in lines), Decimal("0.00"))
    draft = SimpleNamespace(
        number=order.number,
        # A preview of a revision shows the revision it would become.
        revision=order.revision + 1 if body.lines else order.revision,
        revision_reason=body.reason or order.revision_reason,
        total_value=total,
        currency=order.currency,
        confirmed_delivery_date=order.confirmed_delivery_date,
        expected_delivery_date=order.expected_delivery_date,
    )
    return compose_order_email(
        draft, supplier, lines,
        ack_deadline_hours=settings.ack_reminder_hours,
        now=datetime.now(UTC),
    )


@router.post("/purchase-orders/{number}/revise")
async def revise_order(
    number: str,
    body: ReviseIn,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    """Counter a supplier's price or quantity, and put the order back in play."""
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor, settings=settings).revise(
            order, [line.model_dump() for line in body.lines], reason=body.reason
        )
    # Before the general case, and a different answer: a size split that does not
    # add up to its line is the buyer's typing and can be retyped, where "cannot
    # revise a cancelled order" is a conflict with the order's state that no
    # amount of retyping fixes.
    except OrderInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/cancel")
async def cancel_order(
    number: str,
    body: CancelIn,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor, settings=settings).cancel(
            order, reason=body.reason
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/receive")
async def receive_order(
    number: str,
    body: ReceiveIn,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    order = await _by_number(session, number)
    issues = await OrderService(session, actor=actor, settings=settings).receive(
        order, body.received, notify=body.notify
    )
    detail = await _order_detail(session, order)
    return detail | {"issues_raised": [i.detail for i in issues]}


@router.post("/purchase-orders/{number}/receipt-note")
async def send_receipt_note(
    number: str,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
) -> dict:
    """Mail the supplier the receipt for an order already booked in."""
    order = await _by_number(session, number)
    try:
        return await OrderService(session, actor=actor, settings=settings).send_receipt(order)
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/purchase-orders/{number}/shipment", status_code=status.HTTP_201_CREATED)
async def attach_shipment(
    number: str, body: ShipmentIn, session: SessionDep, actor: ActorDep
) -> dict:
    order = await _by_number(session, number)
    shipment = Shipment(
        purchase_order_id=order.id, carrier=body.carrier, tracking_number=body.tracking_number
    )
    session.add(shipment)
    await session.flush()
    return await refresh_shipment(shipment.id, session, actor)


@router.post("/shipments/{shipment_id}/refresh")
async def refresh_shipment(shipment_id: str, session: SessionDep, actor: ActorDep) -> dict:
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown shipment")
    try:
        carrier = get_carrier(shipment.carrier)
    except KeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    result = await carrier.track(shipment.tracking_number)
    shipment.status = result.status
    shipment.eta = (
        datetime.combine(result.eta, datetime.min.time(), tzinfo=UTC) if result.eta else None
    )
    shipment.last_checked_at = datetime.now(UTC)
    shipment.events = {"events": result.events}

    order = await session.get(PurchaseOrder, shipment.purchase_order_id)
    if order is not None and order.status == "acknowledged" and result.status in (
        "collected", "in_transit", "customs", "out_for_delivery"
    ):
        order.status = "in_transit"
    await session.flush()
    return {
        "shipment_id": shipment.id,
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "status": shipment.status,
        "eta": shipment.eta,
        "order_status": order.status if order else None,
    }


# ----------------------------------------------------------------- exceptions
@router.post("/agent/sweep")
async def sweep(session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep) -> dict:
    """What the scheduled agent run does, exposed as a button.

    Two jobs: release orders whose queued send time has arrived, and chase
    suppliers who have gone quiet. Both are things a person would otherwise do at
    an inconvenient hour.
    """
    service = OrderService(session, actor="agent", settings=settings)
    released = []
    for order in await service.due_to_send():
        result = await service.send(order)
        if result["sent"]:
            released.append(order.number)
    chased = await service.sweep_unacknowledged()
    return {
        "released": released,
        "chased": [{"order": i.purchase_order_id, "detail": i.detail} for i in chased],
    }


@router.get("/issues")
async def list_issues(session: SessionDep, actor: ActorDep, open_only: bool = True):
    query = select(Issue).order_by(Issue.created_at.desc())
    if open_only:
        query = query.where(Issue.status == "open")
    issues = list(await session.scalars(query))
    orders = {o.id: o.number for o in await session.scalars(select(PurchaseOrder))}
    return [
        {
            "id": i.id,
            "order": orders.get(i.purchase_order_id),
            "kind": i.kind,
            "severity": i.severity,
            "detail": i.detail,
            "suggested_action": i.suggested_action,
            "status": i.status,
            "created_at": i.created_at,
        }
        for i in issues
    ]


class ResolveIn(BaseModel):
    resolution: str


@router.post("/issues/{issue_id}/resolve")
async def resolve_issue(
    issue_id: str, body: ResolveIn, session: SessionDep, actor: ActorDep
) -> dict:
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown issue")
    issue.status = "resolved"
    issue.resolution = body.resolution
    issue.resolved_at = datetime.now(UTC)
    await session.flush()
    return {"id": issue.id, "status": issue.status, "resolution": issue.resolution}


# --------------------------------------------------------------------- shared
async def _by_number(session, number: str) -> PurchaseOrder:
    order = await session.scalar(select(PurchaseOrder).where(PurchaseOrder.number == number))
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown purchase order {number}")
    return order


async def _order_detail(session, order: PurchaseOrder) -> dict:
    supplier = await session.get(Supplier, order.supplier_id)
    lines = await session.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
    )
    shipments = await session.scalars(
        select(Shipment).where(Shipment.purchase_order_id == order.id)
    )
    issues = await session.scalars(
        select(Issue).where(Issue.purchase_order_id == order.id).order_by(Issue.created_at.desc())
    )
    # Joined to the attachment so the size is real and a row whose file has gone
    # is visibly one, rather than a link that fails when somebody clicks it.
    documents = (
        await session.execute(
            select(Document, Attachment.byte_size)
            .outerjoin(Attachment, Attachment.id == Document.attachment_id)
            .where(Document.purchase_order_id == order.id)
            .order_by(Document.created_at.desc())
        )
    ).all()
    return {
        "id": order.id,
        "number": order.number,
        "status": order.status,
        "supplier": {
            "id": supplier.id, "name": supplier.name, "timezone": supplier.timezone,
            "email": supplier.email, "lead_time_days": supplier.lead_time_days,
        } if supplier else None,
        "currency": order.currency,
        "total_value": str(order.total_value),
        "origin": order.origin,
        "requires_approval": order.requires_approval,
        "approval_reason": order.approval_reason,
        "approved_by": order.approved_by,
        "scheduled_send_at": order.scheduled_send_at,
        "sent_at": order.sent_at,
        "acknowledged_at": order.acknowledged_at,
        "expected_delivery_date": order.expected_delivery_date,
        "confirmed_delivery_date": order.confirmed_delivery_date,
        "revision": order.revision,
        "revision_reason": order.revision_reason,
        "cancel_reason": order.cancel_reason,
        "lines": [
            {
                "sku": line.sku, "description": line.description, "quantity": line.quantity,
                "unit_price": str(line.unit_price), "line_total": str(line.line_total),
                "received_quantity": line.received_quantity,
                # Null where nobody stated a curve, which is not an even split.
                "sizes": line.sizes or None,
            }
            for line in lines
        ],
        "shipments": [
            {
                "id": s.id, "carrier": s.carrier, "tracking_number": s.tracking_number,
                "status": s.status, "eta": s.eta,
            }
            for s in shipments
        ],
        "documents": [
            {
                "id": d.id, "kind": d.kind, "filename": d.filename,
                "attachment_id": d.attachment_id, "byte_size": size,
                "received_at": d.created_at,
            }
            for d, size in documents
        ],
        "issues": [
            {
                "id": i.id, "kind": i.kind, "severity": i.severity, "detail": i.detail,
                "suggested_action": i.suggested_action, "status": i.status,
            }
            for i in issues
        ],
    }
