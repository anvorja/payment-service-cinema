# app/kafka/consumer.py — payment-service
#
# Consume eventos de dominio que activan el cobro:
#   • payment.initiated (flow=wompi) → crea el cobro de las boletas en
#     cinema_payments. La persona paga en el Web Checkout de Wompi; el
#     resultado llega por el webhook, al volver de Wompi o por el
#     reconciliador, y ahí se publica payment.success o payment.failed.
#
import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer

from app.core.config import settings
from app.core.database import SessionLocal
from app.kafka.producer import publish_event
from app.services.payments import start_ticket_checkout

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


def _parse_expires_at(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _handle_payment_initiated(payload: dict) -> None:
    """
    booking-service ya reservó el inventario y pide cobrar la compra. El
    enlace de Wompi vence en expires_at, que fija booking (retiene los
    asientos hasta entonces). Idempotente por order_id.
    """
    flow = payload.get("flow")
    context = payload.get("order_context") or {}
    if flow != "wompi":
        # Flujos retirados (card/pse con datos de tarjeta en el evento).
        logger.error("payment.initiated con flujo no soportado: '%s' | order_id=%s", flow, context.get("order_id"))
        await publish_event("payment.failed", {
            "order_id": context.get("order_id"),
            "user_id": context.get("user_id"),
            "user_email": context.get("user_email"),
            "movie_id": context.get("movie_id"),
            "quantity": context.get("quantity"),
            "total_amount": context.get("total_amount"),
            "failure_reason": "Medio de pago no soportado. Intenta de nuevo.",
            "transaction_id": None,
        })
        return

    with SessionLocal() as db:
        start_ticket_checkout(
            db,
            order_context=context,
            amount=float(payload["amount"]),
            expires_at=_parse_expires_at(payload["expires_at"]),
        )


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
        group_id=settings.KAFKA_GROUP_ID,
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
