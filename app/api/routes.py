# app/api/routes.py
import random
import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


class PaymentRequest(BaseModel):
    card_number: str = Field(..., pattern=r"^\d{16}$")
    card_holder: str
    expiry_month: int = Field(..., ge=1, le=12)
    expiry_year: int = Field(..., ge=2024)
    cvv: str = Field(..., pattern=r"^\d{3,4}$")
    amount: float = Field(..., gt=0)


class PaymentResult(BaseModel):
    transaction_id: str
    status: str
    last_four: str
    card_holder: str
    message: str


@router.post("/payments/process", response_model=PaymentResult)
async def process_payment(request: PaymentRequest) -> PaymentResult:
    """
    Procesa un pago simulado. Siempre aprueba en MVP.
    En producción: integrar Stripe, MercadoPago, etc.
    """
    transaction_id = f"TXN-{random.randint(100000, 999999)}"
    last_four = request.card_number[-4:]

    logger.info(
        "Payment processed | txn=%s | amount=%.2f | last_four=%s",
        transaction_id, request.amount, last_four,
    )

    return PaymentResult(
        transaction_id=transaction_id,
        status="approved",
        last_four=last_four,
        card_holder=request.card_holder,
        message="Payment processed successfully",
    )
