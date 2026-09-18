# payment-service-cinema

Procesa pagos con tarjeta y PSE para el flujo de compra de boletos, y sus reembolsos.

## Responsabilidad

Servicio **interno** — sin ruta pública en Traefik (ver `ARCHITECTURE.md` en la
raíz del proyecto). No tiene base de datos propia: es transaccional y sin
estado, solo produce/consume eventos y llama a la pasarela de pago.

## Stack

FastAPI + `aiokafka`, puerto **8003**. Sin ORM ni base de datos.

## Simulador de pago

`PAYU_ENABLED=false` (valor por defecto): usa un simulador en vez de llamar a
PayU real.

- **Tarjeta:** cualquier número se aprueba, salvo que termine en `0002`,
  `0019` o `0127` (rechazo forzado, para probar el camino `payment.failed` —
  ver `_SIMULATED_DECLINED_SUFFIXES` en `app/api/routes.py`).
- **PSE:** siempre se aprueba.
- **Reembolso:** siempre se aprueba.

`PAYU_ENABLED=true` activa el procesador real (PayU Latam, sandbox por
defecto vía `PAYU_BASE_URL`) — firma MD5 de la orden, llamada HTTP a PayU,
mapeo de `APPROVED`/`DECLINED`/`PENDING`. El mismo flag aplica también al
reembolso (`REFUND` contra PayU en vez del simulador).

## API HTTP

| Método | Ruta | Uso |
|---|---|---|
| `POST` | `/payments/process` | Procesa un pago con tarjeta (invocación directa, fuera del flujo Kafka) |
| `POST` | `/payments/process-pse` | Procesa un pago PSE |
| `POST` | `/payments/refund` | Reembolsa una transacción por `transaction_id` — es la vía real que usa `booking-service` al cancelar una compra |
| `GET` | `/health` | Estado del servicio + si Kafka está configurado |

## Eventos Kafka

**Consume** `payment.initiated` — publicado por `booking-service` cuando
inicia el cobro. **Esta es la vía real del flujo de compra**, no el endpoint
HTTP de arriba: `app/kafka/consumer.py` reutiliza las mismas funciones de
`app/api/routes.py` (`_process_payu` / `_process_simulated` /
`_process_pse_simulated`) para procesar el pago y publicar el resultado.
Verificado con una compra real de punta a punta el 2026-09-18.

**Publica** `payment.success` o `payment.failed` según el resultado. Un
mensaje que falla al procesarse va a `payment.initiated.dlq` antes de
comitear el offset.

⚠️ **`payment.initiated` viaja con `card_number` y `cvv` en texto plano.**
Advertencia de seguridad activa y aceptada mientras `PAYU_ENABLED=false` —
ver el detalle completo (payload, razón, qué falta para resolverlo) en
`kafka-schemas-cinema/event_contracts_operativos.md`.

## Variables de entorno clave

| Variable | Para qué sirve |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` / `KAFKA_API_KEY` / `KAFKA_API_SECRET` | Confluent Cloud — si están vacías, el consumer de Kafka simplemente no arranca |
| `PAYU_ENABLED` | `false` = simulador, `true` = PayU real |
| `PAYU_MERCHANT_ID` / `PAYU_API_KEY` / `PAYU_API_LOGIN` / `PAYU_ACCOUNT_ID` | Credenciales de PayU (sandbox por defecto) |
| `PAYU_BASE_URL` | Endpoint de la API de PayU |
| `PAYU_CURRENCY` | Moneda de las transacciones (`COP`) |

Sin variables de base de datos — este servicio no tiene una.

## Dependencias

- **`booking-service`** — dispara el pago publicando `payment.initiated`
  (Kafka) y llama a `/payments/refund` (HTTP) al cancelar una compra
  confirmada.
- No consume eventos de ningún otro servicio ni expone datos a
  `catalog-service`/`notification-service` directamente — esos se enteran
  del resultado vía `payment.success`/`payment.failed`, no hablando con este
  servicio.

## Correr en local

```bash
# Standalone (sin Kafka, usa el simulador si no hay credenciales)
uvicorn app.main:app --reload --port 8003

# Como parte del stack completo — ver ../IMPLEMENTATION-GUIDE.md
```
