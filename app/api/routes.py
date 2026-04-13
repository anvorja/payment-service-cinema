# app/api/routes.py
import hashlib
import logging
import random
import time
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.kafka.producer import publish_event

logger = logging.getLogger(__name__)
router = APIRouter()


class TicketEventItem(BaseModel):
    code: str
    seat: str
    status: str = "ACTIVE"


class PaymentEventContext(BaseModel):
    order_id: int
    user_id: int
    user_email: str
    customer_name: str
    movie_id: int
    movie_title: str
    movie_genre: Optional[str] = None
    movie_duration: Optional[int] = None
    movie_rating: Optional[str] = None
    quantity: int = Field(..., ge=1)
    total_amount: float = Field(..., gt=0)
    purchase_created_at: str
    show_date: Optional[str] = None
    show_time: Optional[str] = None
    showtime_id: Optional[int] = None
    tickets: List[TicketEventItem] = Field(default_factory=list)


class PaymentRequest(BaseModel):
    card_number: str = Field(..., pattern=r"^\d{16}$")
    card_holder: str
    expiry_month: int = Field(..., ge=1, le=12)
    expiry_year: int = Field(..., ge=2024)
    cvv: str = Field(..., pattern=r"^\d{3,4}$")
    amount: float = Field(..., gt=0)
    order_context: Optional[PaymentEventContext] = None


class PaymentResult(BaseModel):
    transaction_id: str
    status: str          # approved | declined | pending
    last_four: str
    card_holder: str
    message: str


# ── PayU helpers ──────────────────────────────────────────────────────────────

def _payu_signature(reference_code: str, amount: float) -> str:
    """MD5 signature: apiKey~merchantId~referenceCode~amount~currency"""
    raw = f"{settings.PAYU_API_KEY}~{settings.PAYU_MERCHANT_ID}~{reference_code}~{amount:.2f}~{settings.PAYU_CURRENCY}"
    return hashlib.md5(raw.encode()).hexdigest()


async def _process_payu(request: PaymentRequest) -> PaymentResult:
    """Call PayU sandbox API and map response to PaymentResult."""
    reference_code = f"CINE-{int(time.time())}-{random.randint(1000, 9999)}"
    amount = round(request.amount, 2)
    exp_year = str(request.expiry_year)[-2:]   # PayU expects YY
    exp_date = f"{request.expiry_month:02d}/{exp_year}"

    body = {
        "language": "es",
        "command": "SUBMIT_TRANSACTION",
        "merchant": {
            "apiLogin": settings.PAYU_API_LOGIN,
            "apiKey": settings.PAYU_API_KEY,
        },
        "transaction": {
            "order": {
                "accountId": settings.PAYU_ACCOUNT_ID,
                "referenceCode": reference_code,
                "description": "Cinema ticket purchase",
                "language": "es",
                "signature": _payu_signature(reference_code, amount),
                "additionalValues": {
                    "TX_VALUE": {
                        "value": amount,
                        "currency": settings.PAYU_CURRENCY,
                    }
                },
                "buyer": {
                    "fullName": request.card_holder,
                    "emailAddress": "buyer@cinema.com",
                },
            },
            "creditCard": {
                "number": request.card_number,
                "securityCode": request.cvv,
                "expirationDate": exp_date,
                "name": request.card_holder,
            },
            "extraParameters": {
                "INSTALLMENTS_NUMBER": 1,
            },
            "type": "AUTHORIZATION_AND_CAPTURE",
            "paymentMethod": "VISA" if request.card_number.startswith("4") else "MASTERCARD",
            "paymentCountry": "CO",
            "deviceSessionId": reference_code,
            "ipAddress": "127.0.0.1",
            "userAgent": "Mozilla/5.0",
        },
        "test": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(settings.PAYU_BASE_URL, json=body)
            resp.raise_for_status()
    except Exception as exc:
        logger.error("PayU request failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Error comunicándose con la pasarela de pagos.",
        )

    data = resp.json()
    txn_resp = data.get("transactionResponse", {})
    payu_state = txn_resp.get("state", "ERROR")
    txn_id = txn_resp.get("transactionId", reference_code)
    auth_code = txn_resp.get("authorizationCode", "")

    logger.info(
        "PayU response | ref=%s | state=%s | txn=%s | auth=%s",
        reference_code, payu_state, txn_id, auth_code,
    )

    if payu_state == "APPROVED":
        return PaymentResult(
            transaction_id=txn_id,
            status="approved",
            last_four=request.card_number[-4:],
            card_holder=request.card_holder,
            message="Pago aprobado exitosamente.",
        )

    if payu_state == "DECLINED":
        pending_reason = txn_resp.get("pendingReason", "")
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Tarjeta rechazada por el banco. {pending_reason}".strip(),
        )

    if payu_state == "PENDING":
        return PaymentResult(
            transaction_id=txn_id,
            status="pending",
            last_four=request.card_number[-4:],
            card_holder=request.card_holder,
            message="Pago en proceso. Te notificaremos cuando se confirme.",
        )

    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        f"Respuesta inesperada de la pasarela de pagos (estado: {payu_state}).",
    )


# Números de tarjeta que el simulador trata como rechazados (últimos 4 dígitos).
# Útil para probar el camino negativo (payment.failed) sin activar PayU real.
_SIMULATED_DECLINED_SUFFIXES = {"0002", "0019", "0127"}


def _process_simulated(request: PaymentRequest) -> PaymentResult:
    """
    Simulador de tarjeta.
    - Tarjetas cuyos últimos 4 dígitos coincidan con _SIMULATED_DECLINED_SUFFIXES → rechazadas.
    - Cualquier otro número → aprobada.
    """
    last_four = request.card_number[-4:]
    transaction_id = f"TXN-{random.randint(100000, 999999)}"

    if last_four in _SIMULATED_DECLINED_SUFFIXES:
        logger.info(
            "Simulated payment DECLINED | last_four=%s | amount=%.2f",
            last_four, request.amount,
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Tarjeta rechazada por el banco. (simulación — terminación {last_four})",
        )

    logger.info(
        "Simulated payment APPROVED | txn=%s | amount=%.2f | last_four=%s",
        transaction_id, request.amount, last_four,
    )
    return PaymentResult(
        transaction_id=transaction_id,
        status="approved",
        last_four=last_four,
        card_holder=request.card_holder,
        message="Payment processed successfully (simulated)",
    )


# ── PSE models & simulation ───────────────────────────────────────────────────

class PseRequest(BaseModel):
    bank_code: str
    bank_name: str
    document_type: str = Field(..., pattern=r"^(CC|CE|NIT|PP|TI)$")
    document_number: str = Field(..., min_length=4, max_length=20)
    payer_email: str
    amount: float = Field(..., gt=0)
    order_context: Optional[PaymentEventContext] = None


class PseResult(BaseModel):
    transaction_id: str
    status: str          # approved | failed
    bank_name: str
    payer_email: str
    message: str


def _process_pse_simulated(request: PseRequest) -> PseResult:
    """Simula un pago PSE con aprobación automática."""
    transaction_id = f"PSE-{random.randint(10000000, 99999999)}"
    logger.info(
        "Simulated PSE | txn=%s | bank=%s | doc=%s %s | amount=%.2f",
        transaction_id, request.bank_name,
        request.document_type, request.document_number[-4:],
        request.amount,
    )
    return PseResult(
        transaction_id=transaction_id,
        status="approved",
        bank_name=request.bank_name,
        payer_email=request.payer_email,
        message=f"Transferencia PSE aprobada por {request.bank_name}.",
    )


async def _publish_payment_success(
    context: Optional[PaymentEventContext],
    transaction_id: str,
    payment_last_four: str,
) -> None:
    if context is None:
        return

    payload = context.model_dump()
    payload.update(
        {
            "transaction_id": transaction_id,
            "payment_last_four": payment_last_four,
        }
    )
    await publish_event("payment.success", payload)


async def _publish_payment_failed(
    context: Optional[PaymentEventContext],
    reason: str,
    transaction_id: str | None = None,
) -> None:
    if context is None:
        return

    payload = {
        "order_id": context.order_id,
        "user_id": context.user_id,
        "user_email": context.user_email,
        "movie_id": context.movie_id,
        "quantity": context.quantity,
        "total_amount": context.total_amount,
        "failure_reason": reason,
        "transaction_id": transaction_id,
    }
    await publish_event("payment.failed", payload)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/payments/process", response_model=PaymentResult)
async def process_payment(request: PaymentRequest) -> PaymentResult:
    """
    Procesa un pago con tarjeta.
    - Si PAYU_ENABLED=true → llama PayU sandbox
    - Si PAYU_ENABLED=false → simulador MVP (siempre aprueba)
    """
    try:
        result = await _process_payu(request) if settings.PAYU_ENABLED else _process_simulated(request)
    except HTTPException as exc:
        await _publish_payment_failed(request.order_context, str(exc.detail))
        raise
    except Exception as exc:
        logger.error("Unexpected payment processing error: %s", exc)
        await _publish_payment_failed(
            request.order_context,
            "Error inesperado procesando el pago.",
        )
        raise

    await _publish_payment_success(
        request.order_context,
        transaction_id=result.transaction_id,
        payment_last_four=result.last_four,
    )
    return result


@router.post("/payments/process-pse", response_model=PseResult)
async def process_pse_payment(request: PseRequest) -> PseResult:
    """
    Procesa un pago PSE (simulado).
    En producción: integrar con Mercado Pago API (requiere callback URL pública).
    """
    try:
        result = _process_pse_simulated(request)
    except HTTPException as exc:
        await _publish_payment_failed(request.order_context, str(exc.detail))
        raise
    except Exception as exc:
        logger.error("Unexpected PSE processing error: %s", exc)
        await _publish_payment_failed(
            request.order_context,
            "Error inesperado procesando el pago PSE.",
        )
        raise

    await _publish_payment_success(
        request.order_context,
        transaction_id=result.transaction_id,
        payment_last_four="****",
    )
    return result


# ── Refund ────────────────────────────────────────────────────────────────────

class RefundRequest(BaseModel):
    transaction_id: str
    amount: float = Field(..., gt=0)


class RefundResult(BaseModel):
    transaction_id: str
    refund_id: str
    status: str       # approved | failed
    message: str


def _process_simulated_refund(request: RefundRequest) -> RefundResult:
    """Reembolso simulado — siempre aprobado."""
    refund_id = f"REF-{random.randint(100000, 999999)}"
    logger.info(
        "Simulated refund | refund_id=%s | original_txn=%s | amount=%.2f",
        refund_id, request.transaction_id, request.amount,
    )
    return RefundResult(
        transaction_id=request.transaction_id,
        refund_id=refund_id,
        status="approved",
        message=f"Reembolso de ${request.amount:,.0f} COP procesado exitosamente.",
    )


async def _process_payu_refund(request: RefundRequest) -> RefundResult:
    """Llama PayU sandbox para reembolsar usando parentTransactionId."""
    refund_reference = f"REF-{int(time.time())}-{random.randint(1000, 9999)}"
    body = {
        "language": "es",
        "command": "SUBMIT_TRANSACTION",
        "merchant": {
            "apiLogin": settings.PAYU_API_LOGIN,
            "apiKey": settings.PAYU_API_KEY,
        },
        "transaction": {
            "order": {
                "id": request.transaction_id,
            },
            "type": "REFUND",
            "parentTransactionId": request.transaction_id,
            "reason": "Cancelación solicitada por el cliente",
        },
        "test": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(settings.PAYU_BASE_URL, json=body)
            resp.raise_for_status()
    except Exception as exc:
        logger.error("PayU refund request failed: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Error comunicándose con la pasarela de pagos para el reembolso.",
        )

    data = resp.json()
    txn_resp = data.get("transactionResponse", {})
    payu_state = txn_resp.get("state", "ERROR")
    refund_id = txn_resp.get("transactionId", refund_reference)

    logger.info("PayU refund | state=%s | refund_id=%s | original=%s", payu_state, refund_id, request.transaction_id)

    if payu_state in ("APPROVED", "PENDING"):
        return RefundResult(
            transaction_id=request.transaction_id,
            refund_id=refund_id,
            status="approved",
            message="Reembolso aprobado exitosamente.",
        )

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"El reembolso fue rechazado por la pasarela de pagos (estado: {payu_state}).",
    )


@router.post("/payments/refund", response_model=RefundResult)
async def process_refund(request: RefundRequest) -> RefundResult:
    """
    Procesa el reembolso de una transacción existente.
    - Si PAYU_ENABLED=true → llama PayU sandbox con REFUND command
    - Si PAYU_ENABLED=false → simulador (siempre aprueba)
    """
    if settings.PAYU_ENABLED:
        return await _process_payu_refund(request)
    return _process_simulated_refund(request)
