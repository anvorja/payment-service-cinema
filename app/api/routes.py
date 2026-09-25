# app/api/routes.py
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import current_user_email, verify_internal_token
from app.core.database import get_db
from app.domain.payments import view_status
from app.gateways.wompi import GatewayUnavailable
from app.models.payment import Payment
from app.services import payments as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])
internal_router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_token)])


class PaymentView(BaseModel):
    id: str
    reference: str
    kind: Literal["tickets", "recharge"]
    order_id: Optional[int]
    description: str
    amount: float
    currency: str
    # expired: el enlace venció sin que empezara ninguna transacción (no se cobró nada).
    status: Literal["pending", "approved", "declined", "voided", "error", "expired"]
    transaction_id: Optional[str]
    payment_method_type: Optional[str]
    last_four: Optional[str]
    refund_status: Optional[Literal["voided", "manual_required"]]
    expires_at: datetime
    created_at: datetime


class CheckoutView(PaymentView):
    # Solo mientras se pueda pagar (pendiente y sin vencer).
    checkout_url: Optional[str]


def _view(p: Payment) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "id": str(p.id),
        "reference": p.reference,
        "kind": p.kind,
        "order_id": p.order_id,
        "description": p.description,
        "amount": p.amount_in_cents / 100,
        "currency": p.currency,
        "status": view_status(p.status, p.transaction_id, p.expires_at, now),
        "transaction_id": p.transaction_id,
        "payment_method_type": p.payment_method_type,
        "last_four": p.last_four,
        "refund_status": p.refund_status,
        "expires_at": p.expires_at,
        "created_at": p.created_at,
    }


def _checkout_view(p: Payment) -> CheckoutView:
    view = _view(p)
    payable = view["status"] == "pending" and p.transaction_id is None
    return CheckoutView(**view, checkout_url=svc.checkout_url_for(p) if payable else None)


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "La pasarela de pagos no responde. Intenta de nuevo.")


@router.get("/me", response_model=list[PaymentView], summary="Historial de pagos de quien consulta")
def my_payments(db: Session = Depends(get_db), email: str = Depends(current_user_email)):
    return [_view(p) for p in svc.list_for(db, email)]


@router.get(
    "/orders/{order_id}",
    response_model=CheckoutView,
    summary="Cobro de una compra de boletas (con el enlace de Wompi mientras se pueda pagar)",
    responses={404: {"description": "La compra aún no tiene cobro (el inventario no ha respondido) o no es tuya"}},
)
def order_checkout(order_id: int, db: Session = Depends(get_db), email: str = Depends(current_user_email)):
    payment = db.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None or payment.user_email != email:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cobro no encontrado")
    return _checkout_view(payment)


class RechargeRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Pesos colombianos")


@router.post(
    "/recharges",
    response_model=CheckoutView,
    status_code=status.HTTP_201_CREATED,
    summary="Inicia la recarga de la tarjeta Cinema+ (devuelve el enlace de Wompi)",
)
def create_recharge(body: RechargeRequest, db: Session = Depends(get_db), email: str = Depends(current_user_email)):
    try:
        payment = svc.create_recharge(db, email, body.amount)
    except svc.InvalidRecharge as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return _checkout_view(payment)


class VerifyRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, max_length=64)


@router.post(
    "/verify",
    response_model=PaymentView,
    summary="Consulta en Wompi la transacción con la que volvió la persona y aplica el resultado",
)
async def verify(body: VerifyRequest, db: Session = Depends(get_db), email: str = Depends(current_user_email)):
    try:
        payment = await svc.verify(db, email, body.transaction_id)
    except svc.PaymentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pago no encontrado")
    except GatewayUnavailable:
        raise _unavailable()
    return _view(payment)


@router.post(
    "/webhooks/wompi",
    summary="Eventos de Wompi (URL de eventos del comercio)",
    description="Pública: la confianza viene del checksum con WOMPI_EVENTS_SECRET. transaction.updated liquida el pago.",
    responses={401: {"description": "Checksum inválido"}},
)
async def wompi_events(event: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    valid, outcome = svc.gateway().verify_event(event)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid event checksum")
    if outcome is not None:
        await svc.apply_outcome(db, outcome)
    return {"received": True}


class RefundRequest(BaseModel):
    order_id: int = Field(..., gt=0)


class RefundView(BaseModel):
    reference: Optional[str]
    refund_status: Optional[Literal["voided", "manual_required"]]
    detail: str


@internal_router.post("/refunds", response_model=RefundView, summary="Devuelve el dinero de una compra cancelada")
async def refund(body: RefundRequest, db: Session = Depends(get_db)):
    try:
        result = await svc.refund_order(db, body.order_id)
    except GatewayUnavailable:
        raise _unavailable()
    return RefundView(reference=result.reference, refund_status=result.refund_status, detail=result.detail)
