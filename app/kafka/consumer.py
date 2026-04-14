# app/kafka/consumer.py — payment-service
#
# Consume eventos de dominio que activan el procesamiento de pagos:
#   • payment.initiated → procesa el pago (tarjeta o PSE) y publica
#     payment.success o payment.failed según el resultado.
#
import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer
from fastapi import HTTPException

from app.core.config import settings
from app.kafka.producer import publish_event

logger = logging.getLogger(__name__)

_RESTART_DELAY = 10


async def _send_to_dlq(topic: str, payload: dict, error: Exception) -> None:
    """Publica el mensaje fallido en el topic DLQ correspondiente antes de commitear el offset."""
    dlq_topic = f"{topic}.dlq"
    try:
        await publish_event(dlq_topic, {
            "original_topic": topic,
            "original_payload": payload,
            "error": str(error),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.warning("Mensaje enviado al DLQ | dlq_topic=%s", dlq_topic)
    except Exception as dlq_exc:
        logger.error("No se pudo publicar al DLQ | dlq_topic=%s | error=%s", dlq_topic, dlq_exc)


async def _handle_payment_initiated(payload: dict) -> None:
    """
    Procesa un evento payment.initiated emitido por booking-service.

    El payload contiene el método de pago ('flow': 'card' | 'pse'),
    las credenciales correspondientes y el contexto de la orden.
    Publica payment.success o payment.failed según el resultado.
    """
    # Importación local para evitar dependencia circular en el arranque del módulo.
    from app.api.routes import (
        PaymentEventContext,
        PaymentRequest,
        PseRequest,
        _process_payu,
        _process_pse_simulated,
        _process_simulated,
        _publish_payment_failed,
        _publish_payment_success,
    )

    flow = payload.get("flow")
    raw_context = payload.get("order_context")
    order_context = PaymentEventContext(**raw_context) if raw_context else None

    try:
        if flow == "card":
            request = PaymentRequest(
                card_number=payload["card_number"],
                card_holder=payload["card_holder"],
                expiry_month=payload["expiry_month"],
                expiry_year=payload["expiry_year"],
                cvv=payload["cvv"],
                amount=payload["amount"],
                order_context=order_context,
            )
            result = await _process_payu(request) if settings.PAYU_ENABLED else _process_simulated(request)
            await _publish_payment_success(order_context, result.transaction_id, result.last_four)

        elif flow == "pse":
            request = PseRequest(
                bank_code=payload["bank_code"],
                bank_name=payload["bank_name"],
                document_type=payload["document_type"],
                document_number=payload["document_number"],
                payer_email=payload["payer_email"],
                amount=payload["amount"],
                order_context=order_context,
            )
            result = _process_pse_simulated(request)
            await _publish_payment_success(order_context, result.transaction_id, "****")

        else:
            logger.error("payment.initiated con flow desconocido: '%s'", flow)
            await _publish_payment_failed(order_context, f"Flujo de pago no reconocido: {flow}")

    except HTTPException as exc:
        order_id = (raw_context or {}).get("order_id")
        logger.warning(
            "Pago rechazado | order_id=%s | status=%s | detail=%s",
            order_id, exc.status_code, exc.detail,
        )
        await _publish_payment_failed(order_context, str(exc.detail))

    except Exception as exc:
        order_id = (raw_context or {}).get("order_id")
        logger.error(
            "Error inesperado procesando payment.initiated | order_id=%s | error=%s",
            order_id, exc,
        )
        await _publish_payment_failed(order_context, "Error inesperado en el procesamiento del pago.")


_HANDLERS: dict = {
    "payment.initiated": _handle_payment_initiated,
}


async def _run_consumer() -> None:
    ssl_context = ssl.create_default_context()
    consumer = AIOKafkaConsumer(
        *_HANDLERS.keys(),
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        security_protocol="SASL_SSL",
        sasl_mechanism="PLAIN",
        sasl_plain_username=settings.KAFKA_API_KEY,
        sasl_plain_password=settings.KAFKA_API_SECRET,
        ssl_context=ssl_context,
        group_id="payment-service-group",
        auto_offset_reset="latest",
        enable_auto_commit=False,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )

    await consumer.start()
    logger.info("Payment consumer iniciado | topics=%s", list(_HANDLERS.keys()))

    try:
        async for msg in consumer:
            handler = _HANDLERS.get(msg.topic)
            if handler:
                try:
                    await handler(msg.value)
                except Exception as exc:
                    logger.error("Error procesando %s: %s", msg.topic, exc)
                    await _send_to_dlq(msg.topic, msg.value, exc)
            await consumer.commit()
    except asyncio.CancelledError:
        raise
    finally:
        await consumer.stop()
        logger.info("Payment consumer detenido")


async def start_consumer() -> None:
    if not settings.KAFKA_BOOTSTRAP_SERVERS:
        logger.info("Kafka no configurado — payment consumer no iniciado")
        return

    while True:
        try:
            await _run_consumer()
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Payment consumer caído: %s — reiniciando en %ds",
                exc, _RESTART_DELAY,
            )
            await asyncio.sleep(_RESTART_DELAY)
