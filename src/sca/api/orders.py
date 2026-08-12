"""Planning, purchase orders, shipments and exceptions."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep
from sca.carriers.base import get_carrier
from sca.config import get_settings
from sca.models import (
    Attachment,
    Document,
    Issue,
    PurchaseOrder,
    PurchaseOrderLine,
    Shipment,
    Supplier,
)
from sca.orders.compose import compose_order_email
from sca.orders.service import OrderError, OrderService
from sca.planning.service import PlanningService

router = APIRouter(tags=["orders"])


class LineIn(BaseModel):
    sku: str
    description: str | None = None
    quantity: int
    unit_price: Decimal


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
async def suggest(session: SessionDep, actor: ActorDep) -> dict:
    suggestions = await PlanningService(session).suggest()
    grouped: dict[str, list[dict]] = {}
    for suggestion in suggestions:
        grouped.setdefault(suggestion.supplier_id, []).append(suggestion.as_dict())
    suppliers = {s.id: s.name for s in await session.scalars(select(Supplier))}
    return {
        "count": len(suggestions),
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


@router.post("/planning/create-orders", status_code=status.HTTP_201_CREATED)
async def create_from_suggestions(
    session: SessionDep, actor: ActorDep, body: CreateOrdersIn | None = None
) -> dict:
    """One draft per supplier, not one per line: a buyer sends an order, not a
    stream of individual requests, and consolidating is what earns the freight."""
    wanted = set(body.skus) if body and body.skus else None
    suggestions = await PlanningService(session).suggest()
    if wanted is not None:
        suggestions = [s for s in suggestions if s.sku in wanted]
    service = OrderService(session, actor=actor)
    grouped: dict[str, list[dict]] = {}
    for suggestion in suggestions:
        grouped.setdefault(suggestion.supplier_id, []).append(
            {
                "sku": suggestion.sku,
                "description": suggestion.description,
                "quantity": suggestion.suggest_quantity,
                "unit_price": suggestion.unit_cost,
            }
        )
    created = []
    for supplier_id, lines in grouped.items():
        order = await service.create(supplier_id, lines, origin="forecast")
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
async def create_order(body: OrderIn, session: SessionDep, actor: ActorDep) -> dict:
    try:
        order = await OrderService(session, actor=actor).create(
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
    number: str, body: ApproveIn, session: SessionDep, actor: ActorDep
) -> dict:
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor).approve(order, approver=body.approver)
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/send")
async def send_order(
    number: str, session: SessionDep, actor: ActorDep, body: SendIn | None = None
) -> dict:
    order = await _by_number(session, number)
    if order.status == "pending_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{order.number} needs approval first: {order.approval_reason}",
        )
    try:
        result = await OrderService(session, actor=actor).send(
            order,
            subject=body.subject if body else None,
            message=body.body if body else None,
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    detail = await _order_detail(session, order)
    return detail | {"delivery": result}


@router.get("/purchase-orders/{number}/message")
async def order_message(number: str, session: SessionDep, actor: ActorDep) -> dict:
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
        ack_deadline_hours=get_settings().ack_reminder_hours,
        now=datetime.now(UTC),
    )


class MessagePreviewIn(BaseModel):
    # Lines as they stand in the editor, which is not what the database holds:
    # the whole point is to read the message before committing to the change.
    lines: list[LineIn] = []
    reason: str | None = None


@router.post("/purchase-orders/{number}/message")
async def preview_message(
    number: str, body: MessagePreviewIn, session: SessionDep, actor: ActorDep
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
        ack_deadline_hours=get_settings().ack_reminder_hours,
        now=datetime.now(UTC),
    )


@router.post("/purchase-orders/{number}/revise")
async def revise_order(
    number: str, body: ReviseIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Counter a supplier's price or quantity, and put the order back in play."""
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor).revise(
            order, [line.model_dump() for line in body.lines], reason=body.reason
        )
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/cancel")
async def cancel_order(
    number: str, body: CancelIn, session: SessionDep, actor: ActorDep
) -> dict:
    order = await _by_number(session, number)
    try:
        await OrderService(session, actor=actor).cancel(order, reason=body.reason)
    except OrderError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _order_detail(session, order)


@router.post("/purchase-orders/{number}/receive")
async def receive_order(
    number: str, body: ReceiveIn, session: SessionDep, actor: ActorDep
) -> dict:
    order = await _by_number(session, number)
    issues = await OrderService(session, actor=actor).receive(order, body.received)
    detail = await _order_detail(session, order)
    return detail | {"issues_raised": [i.detail for i in issues]}


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
async def sweep(session: SessionDep, actor: ActorDep) -> dict:
    """What the scheduled agent run does, exposed as a button.

    Two jobs: release orders whose queued send time has arrived, and chase
    suppliers who have gone quiet. Both are things a person would otherwise do at
    an inconvenient hour.
    """
    service = OrderService(session, actor="agent")
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
