# app/services/payments.py — casos de uso de pagos con Wompi.
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.payments import (
    KIND_RECHARGE,
    KIND_TICKETS,
    REFUND_MANUAL_REQUIRED,
    REFUND_VOIDED,
    Outcome,
    is_final,
    new_reference,
    settled_status,
    to_cents,
)
from app.gateways.wompi import GatewayUnavailable, WompiGateway, WompiSettings
from app.kafka.producer import publish_event
from app.models.payment import Payment

logger = logging.getLogger(__name__)

_gateway: WompiGateway | None = None


def gateway() -> WompiGateway:
    global _gateway
    if _gateway is None:
        _gateway = WompiGateway(
            WompiSettings(
                public_key=settings.WOMPI_PUBLIC_KEY,
                private_key=settings.WOMPI_PRIVATE_KEY,
                integrity_secret=settings.WOMPI_INTEGRITY_SECRET,
                events_secret=settings.WOMPI_EVENTS_SECRET,
                api_url=settings.WOMPI_API_URL.rstrip("/"),
                checkout_url=settings.WOMPI_CHECKOUT_URL,
                redirect_url=settings.PAYMENTS_REDIRECT_URL,
                timeout_seconds=settings.WOMPI_TIMEOUT_SECONDS,
            )
        )
    return _gateway


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PaymentNotFound(Exception):
    pass


class InvalidRecharge(Exception):
    pass


def checkout_url_for(payment: Payment) -> str:
    return gateway().checkout_url(
        reference=payment.reference,
        amount_in_cents=payment.amount_in_cents,
        currency=payment.currency,
        expires_at=payment.expires_at,
        customer_email=payment.user_email,
    )


# ── Crear cobros ──────────────────────────────────────────────────────────────

def start_ticket_checkout(db: Session, order_context: dict[str, Any], amount: float, expires_at: datetime) -> Payment:
    """
    Crea el cobro de las boletas de una compra. Idempotente por order_id: un
    payment.initiated repetido (reintento del reconciliador de booking)
    devuelve el mismo pago y el mismo enlace.
    """
    order_id = int(order_context["order_id"])
    existing = db.scalar(select(Payment).where(Payment.order_id == order_id))
    if existing is not None:
        return existing

    quantity = int(order_context.get("quantity") or 1)
    title = order_context.get("movie_title") or "Cine"
    payment = Payment(
        reference=new_reference(settings.PAYMENTS_REFERENCE_PREFIX),
        kind=KIND_TICKETS,
        order_id=order_id,
        user_id=order_context.get("user_id"),
        user_email=order_context["user_email"],
        description=f"{quantity} {'boleta' if quantity == 1 else 'boletas'} · {title}"[:255],
        amount_in_cents=to_cents(amount),
        currency=settings.PAYMENTS_CURRENCY,
        status="pending",
        expires_at=expires_at,
        order_context=order_context,
    )
    db.add(payment)
    db.commit()
    logger.info("Cobro de boletas creado | order_id=%s | ref=%s", order_id, payment.reference)
    return payment


def create_recharge(db: Session, user_email: str, amount: int) -> Payment:
    if not settings.RECHARGE_MIN_AMOUNT <= amount <= settings.RECHARGE_MAX_AMOUNT:
        raise InvalidRecharge(
            f"La recarga debe estar entre {settings.RECHARGE_MIN_AMOUNT} y {settings.RECHARGE_MAX_AMOUNT} pesos."
        )
    payment = Payment(
        reference=new_reference(settings.PAYMENTS_REFERENCE_PREFIX),
        kind=KIND_RECHARGE,
        user_email=user_email,
        description="Recarga tarjeta Cinema+",
        amount_in_cents=to_cents(amount),
        currency=settings.PAYMENTS_CURRENCY,
        status="pending",
        expires_at=_now() + timedelta(minutes=settings.RECHARGE_CHECKOUT_TTL_MINUTES),
    )
    db.add(payment)
    db.commit()
    logger.info("Recarga Cinema+ creada | ref=%s", payment.reference)
    return payment


# ── Resultados de Wompi ───────────────────────────────────────────────────────

_FAILURE_REASONS = {
    "declined": "Wompi rechazó el pago.",
    "voided": "El pago fue anulado.",
    "error": "El pago no se pudo completar en Wompi.",
}


async def _publish_ticket_result(payment: Payment) -> None:
    context = dict(payment.order_context or {})
    if payment.status == "approved":
        await publish_event(
            "payment.success",
            {
                **context,
                "transaction_id": payment.transaction_id,
                "payment_reference": payment.reference,
                "payment_method_type": payment.payment_method_type,
                "payment_last_four": payment.last_four or "****",
            },
        )
        return
    await publish_event(
        "payment.failed",
        {
            "order_id": context.get("order_id"),
            "user_id": context.get("user_id"),
            "user_email": context.get("user_email"),
            "movie_id": context.get("movie_id"),
            "quantity": context.get("quantity"),
            "total_amount": context.get("total_amount"),
            "failure_reason": _FAILURE_REASONS.get(payment.status, "El pago no se completó."),
            "transaction_id": payment.transaction_id,
            "payment_reference": payment.reference,
        },
    )


async def apply_outcome(db: Session, outcome: Outcome) -> Payment | None:
    """
    Aplica lo que dice Wompi a un pago. Llega por el webhook, al volver de
    Wompi y por el reconciliador: el bloqueo de fila y la regla "un pago solo
    avanza una vez" hacen que solo uno de ellos publique el resultado.
    """
    payment = db.scalar(select(Payment).where(Payment.reference == outcome.reference).with_for_update())
    if payment is None:
        # Otra app del mismo comercio de Wompi (p. ej. api-drinks): no es nuestro.
        db.rollback()
        return None

    new_status = settled_status(payment.status, payment.amount_in_cents, payment.currency, outcome)
    if payment.transaction_id is None:
        payment.transaction_id = outcome.transaction_id
        payment.payment_method_type = outcome.payment_method_type
        payment.last_four = outcome.last_four
    if new_status is None:
        db.commit()
        return payment

    if new_status == "error":
        logger.error(
            "Monto o moneda no coinciden | ref=%s | esperado=%s %s | recibido=%s %s",
            payment.reference, payment.amount_in_cents, payment.currency,
            outcome.amount_in_cents, outcome.currency,
        )
    payment.status = new_status
    db.commit()
    logger.info("Pago %s | ref=%s | txn=%s", new_status, payment.reference, payment.transaction_id)

    if payment.kind == KIND_TICKETS and is_final(new_status):
        await _publish_ticket_result(payment)
    return payment


async def verify(db: Session, user_email: str, transaction_id: str) -> Payment:
    """Al volver de Wompi (?id=<transacción>): consulta y aplica el resultado."""
    outcome = await gateway().fetch_transaction(transaction_id)
    payment = None
    if outcome is not None:
        payment = db.scalar(select(Payment).where(Payment.reference == outcome.reference))
    if payment is None or payment.user_email != user_email:
        raise PaymentNotFound()
    return await apply_outcome(db, outcome) or payment


async def reconcile(db: Session) -> None:
    """
    Consulta en Wompi los pagos que siguen pendientes: los que ya tienen
    transacción y los que aún no (por si no llegó el evento ni la persona
    volvió). Deja de preguntar pasado el margen tras el vencimiento.
    """
    horizon = _now() - timedelta(minutes=settings.PAYMENTS_RECONCILE_GRACE_MINUTES)
    pending = db.scalars(
        select(Payment).where(
            Payment.status == "pending",
            or_(Payment.transaction_id.is_not(None), Payment.expires_at > horizon),
        )
    ).all()
    for payment in pending:
        try:
            if payment.transaction_id:
                outcome = await gateway().fetch_transaction(payment.transaction_id)
            else:
                outcome = await gateway().find_by_reference(payment.reference)
        except GatewayUnavailable as exc:
            logger.warning("Reconciliador: Wompi no disponible (%s)", exc)
            return
        if outcome is not None:
            await apply_outcome(db, outcome)


# ── Reembolsos ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RefundResult:
    reference: str | None
    refund_status: str | None  # voided | manual_required | None (no hubo cobro)
    detail: str


async def refund_order(db: Session, order_id: int) -> RefundResult:
    """
    Devuelve el dinero de una compra cancelada. Wompi solo anula por API las
    transacciones con tarjeta; para otros medios (PSE, Nequi…) queda marcado
    para devolverlo desde el panel de Wompi. Idempotente.
    """
    payment = db.scalar(select(Payment).where(Payment.order_id == order_id).with_for_update())
    if payment is None or payment.status != "approved":
        db.rollback()
        return RefundResult(
            reference=payment.reference if payment else None,
            refund_status=payment.refund_status if payment else None,
            detail="No hubo cobro aprobado: no hay nada que devolver.",
        )
    if payment.refund_status is not None:
        result = RefundResult(payment.reference, payment.refund_status, payment.refund_detail or "")
        db.rollback()
        return result

    if payment.payment_method_type == "CARD" and payment.transaction_id:
        try:
            voided = await gateway().void(payment.transaction_id)
        except GatewayUnavailable as exc:
            db.rollback()
            raise exc
        if voided.ok:
            payment.status = "voided"
            payment.refund_status = REFUND_VOIDED
            payment.refund_detail = "Wompi anuló la transacción con tarjeta."
        else:
            payment.refund_status = REFUND_MANUAL_REQUIRED
            payment.refund_detail = f"No se pudo anular por API ({voided.reason}). Devolver desde el panel de Wompi."[:255]
    else:
        payment.refund_status = REFUND_MANUAL_REQUIRED
        payment.refund_detail = (
            f"Wompi no anula pagos {payment.payment_method_type or 'sin tarjeta'} por API. "
            "Devolver desde el panel de Wompi."
        )[:255]
    db.commit()
    logger.info("Reembolso | order_id=%s | ref=%s | %s", order_id, payment.reference, payment.refund_status)
    return RefundResult(payment.reference, payment.refund_status, payment.refund_detail or "")


def list_for(db: Session, user_email: str) -> list[Payment]:
    return list(
        db.scalars(select(Payment).where(Payment.user_email == user_email).order_by(Payment.created_at.desc())).all()
    )
