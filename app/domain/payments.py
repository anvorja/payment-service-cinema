# app/domain/payments.py — reglas de pagos, sin framework ni base de datos.
import uuid
from dataclasses import dataclass
from datetime import datetime

STATUSES = ("pending", "approved", "declined", "voided", "error")
# Lo que ve la persona: un enlace que venció sin que empezara ninguna
# transacción es "expired". Se deriva al leer, no se guarda.
VIEW_STATUSES = (*STATUSES, "expired")

KIND_TICKETS = "tickets"
KIND_RECHARGE = "recharge"
KINDS = (KIND_TICKETS, KIND_RECHARGE)

# Qué pasó con el dinero al cancelar una compra aprobada.
REFUND_VOIDED = "voided"                    # Wompi anuló la transacción (tarjeta)
REFUND_MANUAL_REQUIRED = "manual_required"  # hay que devolverlo desde el panel de Wompi
REFUND_STATUSES = (REFUND_VOIDED, REFUND_MANUAL_REQUIRED)


@dataclass(frozen=True)
class Outcome:
    """Lo que dice la pasarela de una transacción."""
    reference: str
    transaction_id: str
    status: str
    amount_in_cents: int
    currency: str
    payment_method_type: str | None = None
    last_four: str | None = None


def new_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def to_cents(amount: float) -> int:
    return round(amount * 100)


def is_final(status: str) -> bool:
    return status != "pending"


def settled_status(
    current: str,
    amount_in_cents: int,
    currency: str,
    outcome: Outcome,
) -> str | None:
    """
    Estado que toma un pago al recibir un resultado, o None si no cambia.
    Un pago solo avanza una vez (idempotente: el webhook, la verificación al
    volver de Wompi y el reconciliador pueden llegar los tres). Un monto o
    moneda que no coincide es "error": nunca se entregan boletas por menos
    de su precio.
    """
    if is_final(current):
        return None
    if outcome.amount_in_cents != amount_in_cents or outcome.currency != currency:
        return "error"
    if outcome.status == current:
        return None
    return outcome.status


def is_abandoned(status: str, transaction_id: str | None, expires_at: datetime, now: datetime) -> bool:
    """El enlace venció y nunca empezó una transacción: ya no se puede pagar."""
    return status == "pending" and transaction_id is None and now >= expires_at


def view_status(status: str, transaction_id: str | None, expires_at: datetime, now: datetime) -> str:
    return "expired" if is_abandoned(status, transaction_id, expires_at, now) else status
