# app/models/payment.py
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.domain.payments import KINDS, REFUND_STATUSES, STATUSES


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Payment(Base):
    """
    Un cobro por Wompi: las boletas de una compra (order_id de booking-service)
    o una recarga de la tarjeta Cinema+. La referencia es la que ve Wompi.
    """
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # Solo para kind=tickets: la compra de booking-service que paga.
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Dueño del pago: el "sub" del JWT de auth-service.
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_in_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payment_method_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # Últimos 4 dígitos si se pagó con tarjeta (los da Wompi; nunca el número).
    last_four: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    # Hasta cuándo se puede pagar el enlace (Wompi lo rechaza después).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Contexto de la orden para publicar payment.success/failed (boletas).
    order_context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    refund_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    refund_detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("reference", name="uq_payments_reference"),
        UniqueConstraint("transaction_id", name="uq_payments_transaction_id"),
        CheckConstraint(_in("status", STATUSES), name="ck_payments_status"),
        CheckConstraint(_in("kind", KINDS), name="ck_payments_kind"),
        CheckConstraint(f"refund_status IS NULL OR {_in('refund_status', REFUND_STATUSES)}", name="ck_payments_refund_status"),
        CheckConstraint("amount_in_cents > 0", name="ck_payments_amount"),
        # Una compra tiene a lo sumo un pago: reintentos de payment.initiated
        # reutilizan el mismo en vez de crear otro enlace.
        Index("ux_payments_order_id", "order_id", unique=True, postgresql_where=text("order_id IS NOT NULL")),
        Index("ix_payments_pending", "status", "expires_at", postgresql_where=text("status = 'pending'")),
    )
