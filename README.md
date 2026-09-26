# payment-service-cinema

Cobra las boletas y las recargas de la tarjeta Cinema+ con el **Web Checkout de
Wompi** y guarda el historial de pagos. La persona paga en Wompi (tarjeta, PSE,
Nequi, Bancolombia…): sus datos de pago nunca pasan por nuestros servicios.

## Stack

FastAPI + SQLAlchemy 2 + Alembic + `aiokafka`, puerto **8003**. Base propia
**`cinema_payments`** (dueño único: este servicio).

## Flujo de una compra de boletas

```
1. booking-service (inventario ya reservado) ── payment.initiated {flow: wompi, amount, expires_at, order_context}
2. payment-service crea el cobro en cinema_payments: referencia cinemaplus-<uuid>, vence en expires_at
3. Frontend: GET /api/v1/payments/orders/{order_id} → checkout_url → redirige a Wompi
4. La persona paga en Wompi → Wompi redirige a <PAYMENTS_REDIRECT_URL>?id=<transacción>
5. El resultado llega por cualquiera de estas vías (la primera gana, las demás no cambian nada):
     • POST /api/v1/payments/verify  (el frontend al volver)
     • POST /api/v1/payments/webhooks/wompi  (evento transaction.updated firmado)
     • reconciliador: consulta en Wompi los pagos pendientes cada PAYMENTS_RECONCILE_INTERVAL_SECONDS
6. payment-service publica payment.success o payment.failed UNA vez → la saga de booking sigue igual
   (confirma, genera los tickets con QR, notification envía el correo)
```

- **`expires_at` lo fija booking-service**, porque es quien retiene los
  asientos mientras la persona paga (`PAYMENT_CHECKOUT_TTL_SECONDS`). Se
  firma como `expiration-time`: pasada esa hora Wompi no cobra el enlace.
- **Idempotencia:** un cobro por `order_id` (índice único). Los reintentos de
  `payment.initiated` reutilizan el mismo cobro y el mismo enlace. Un pago solo
  avanza una vez (bloqueo de fila + regla de dominio).
- **Monto:** si Wompi reporta un monto o moneda distintos, el pago queda en
  `error`: nunca se entregan boletas por menos de su precio.
- **Enlace vencido:** un pago `pending` sin transacción y ya vencido se ve como
  `expired` ("Sin completar": no se cobró nada). Se deriva al leer, no se
  guarda: un PSE que empezó a tiempo todavía puede resolverse.

## Recargas Cinema+

`POST /api/v1/payments/recharges {amount}` crea un cobro `kind=recharge`
(entre `RECHARGE_MIN_AMOUNT` y `RECHARGE_MAX_AMOUNT`, vence en
`RECHARGE_CHECKOUT_TTL_MINUTES`) y devuelve el `checkout_url`. Vuelve a la
misma página de resultado que las boletas.

## Reembolsos

`booking-service` cancela una compra y llama a `POST /internal/refunds
{order_id}` (con `X-Internal-Token`):

| Cómo se pagó | Qué pasa | `refund_status` |
|---|---|---|
| Tarjeta (`CARD`) | Wompi anula la transacción (`POST /transactions/{id}/void`, llave privada) | `voided` |
| Otro medio (PSE, Nequi…) o Wompi rechaza la anulación | Wompi no lo devuelve por API: queda marcado para devolverlo desde el panel de Wompi | `manual_required` |
| Nunca se cobró | Nada que devolver | `null` |

También se usa cuando llega un pago aprobado para una compra que ya se canceló
(pago tardío o doble venta del asiento).

## API HTTP

Públicas vía Traefik (`/api/v1/payments`), con el JWT de auth-service:

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/api/v1/payments/me` | Historial de pagos (boletas y recargas) |
| `GET` | `/api/v1/payments/orders/{order_id}` | Cobro de una compra, con `checkout_url` mientras se pueda pagar. `404` mientras booking no lo haya pedido |
| `POST` | `/api/v1/payments/recharges` | Inicia una recarga Cinema+ |
| `POST` | `/api/v1/payments/verify` | Consulta en Wompi la transacción con que volvió la persona y aplica el resultado |
| `POST` | `/api/v1/payments/webhooks/wompi` | Eventos de Wompi. Público: la confianza viene del checksum (`WOMPI_EVENTS_SECRET`) |

Internas (no pasan por Traefik; `X-Internal-Token`):

| Método | Ruta | Uso |
|---|---|---|
| `POST` | `/internal/refunds` | Devuelve el dinero de una compra cancelada |

`GET /health`, `GET /metrics` (Prometheus) y `/docs` (OpenAPI).

## Eventos Kafka

- **Consume** `payment.initiated` (`flow=wompi`). Un flujo viejo (`card`/`pse`)
  responde `payment.failed`. Un mensaje que falla va a `payment.initiated.dlq`.
- **Publica** `payment.success` / `payment.failed`, con `payment_reference` y
  `payment_method_type`. Ver `kafka-schemas-cinema`.

## Variables de entorno

Todas en `.env.example`. Las esenciales:

| Variable | Para qué |
|---|---|
| `DATABASE_URL` | `cinema_payments` |
| `MIGRATE_ON_START` | `true`: aplica las migraciones al arrancar (cada una corre una sola vez) |
| `JWT_SECRET` | El mismo de auth-service |
| `INTERNAL_SERVICE_TOKEN` | El mismo de booking-service |
| `CORS_ORIGINS` | Orígenes del frontend, separados por coma |
| `WOMPI_PUBLIC_KEY`, `WOMPI_PRIVATE_KEY`, `WOMPI_INTEGRITY_SECRET`, `WOMPI_EVENTS_SECRET` | Llaves del comercio (Wompi → Desarrolladores). Sandbox: `pub_test_`, `prv_test_`, `test_integrity_`, `test_events_` |
| `WOMPI_API_URL` | `https://sandbox.wompi.co/v1` o `https://production.wompi.co/v1` |
| `PAYMENTS_REDIRECT_URL` | Página de resultado del frontend. **Wompi no acepta `localhost`**: en local, `http://lvh.me:5173/pago/resultado` |
| `PAYMENTS_REFERENCE_PREFIX` | `cinemaplus` |

El servicio no arranca si falta una llave (falla con el nombre de la variable).
**La llave privada es obligatoria:** Wompi ya no responde la consulta de una
transacción sin ella (devuelve `404` aunque la transacción exista).

## Migraciones (Alembic)

```bash
alembic upgrade head      # o MIGRATE_ON_START=true
alembic current
```

Base nueva: `CREATE DATABASE cinema_payments;` y arrancar (o `alembic upgrade head`).

## Correr en local

La app debe abrirse en `http://lvh.me:5173` (ver `cinema_ui`). Para que Wompi
también avise por webhook a tu máquina, ver `infra-cinema/DEVELOPMENT.md`,
"Pagos con Wompi en local". Sin webhook funciona igual: el resultado llega al
volver de Wompi o por el reconciliador.

```bash
pip install -r requirements-dev.txt
pytest                                   # dominio y gateway de Wompi
uvicorn app.main:app --reload --port 8003
```

## Pruebas en sandbox

Tarjetas de Wompi: `4242 4242 4242 4242` aprobada, `4111 1111 1111 1111`
rechazada (cualquier CVC y fecha futura). Nequi y PSE tienen sus datos de
prueba en la documentación de Wompi ("Datos de prueba en Sandbox").

## Mismo comercio que api-drinks

Este servicio usa el mismo comercio sandbox de Wompi que api-drinks. Wompi
admite **una sola URL de eventos por comercio**: mientras apunte a cine, los
eventos de api-drinks no llegan allá (y viceversa). Cada servicio ignora
referencias que no son suyas (`cinemaplus-…` vs `drinks-…`). Para desplegar los
dos a la vez (Render), crear un segundo comercio sandbox en Wompi, o poner un
reenvío de eventos por prefijo de referencia.

## Flujo de trabajo: Gitflow

| Rama        | Sale de   | Entra a (vía PR)         | Método en GitHub | Para |
| ----------- | --------- | ------------------------ | ---------------- | ---- |
| `main`      | —         | —                        | —                | Lo que está en producción. Cada merge es una versión. |
| `develop`   | `main`    | —                        | —                | Integración de lo próximo a publicar. Rama por defecto. |
| `feature/*` | `develop` | `develop`                | **Squash**       | Una funcionalidad o cambio: `feature/mi-cambio`. |
| `release/*` | `develop` | `main` y luego `develop` | **Merge** a `main`; **Squash** a `develop` | Preparar una versión: `release/1.0.0`. Solo ajustes finales. |
| `hotfix/*`  | `main`    | `main` y luego `develop` | **Merge** a `main`; **Squash** a `develop` | Corrección urgente en producción. |

- **Nadie hace push directo** a `main` ni a `develop`: todo entra por pull request, con los checks de CI en verde.
- **En `develop` se usa squash:** cada feature queda como un solo commit con el título del PR.
- **En `main` se usa merge commit:** cada release o hotfix queda visible como una unidad.
- **Todavía no hay releases:** la app no está completa, así que `main` se queda como está hasta el
  primer `release/*`. Desde entonces, cada versión se etiqueta en `main` (`git tag -a v1.0.0`) con
  [versionado semántico](https://semver.org/lang/es/).

```bash
git switch develop && git pull
git switch -c feature/mi-cambio
# ...commits...
git push -u origin feature/mi-cambio   # abrir PR hacia develop → Squash and merge
```

## CI/CD

GitHub Actions (`.github/workflows/`) corre en cada PR hacia `main` o `develop`. Los rulesets exigen
estos checks; si se renombra un job, hay que actualizar `.github/rulesets/*.json`.

| Check | Qué revisa |
| ----- | ---------- |
| `Lint` | Ruff con las reglas de `ruff.toml`. |
| `Calidad y build` | Instala las dependencias, compila todo el código y carga la app con configuración falsa (sin base de datos ni Kafka); corre las pruebas con pytest. |
| `Imagen Docker` | Construye la imagen y comprueba que la app carga dentro de ella, sin red. |

Con cada push a `develop` o `main` (es decir, al fusionar un PR), y solo si pasaron los checks, se
publica en Docker Hub **la misma imagen que se probó** (no se reconstruye):

- `develop` → `<usuario>/payment-service-cinema:develop` y `:<sha>`
- `main` → `<usuario>/payment-service-cinema:latest` y `:<sha>`

El flujo no despliega en ningún servicio (tampoco en Render): solo publica la imagen.

### Configuración en GitHub (una vez)

- **Rulesets:** `main` y `develop` se protegen importando `.github/rulesets/main.json` y
  `.github/rulesets/develop.json` en *Settings → Rules → Rulesets → Import a ruleset*. Exigen PR, los
  checks de la tabla de arriba, y no permiten borrar la rama ni forzar pushes. `main` solo acepta
  merge commit y `develop` solo squash.
- **Settings → General:** rama por defecto `develop`; permitir merge commits y squash (no rebase);
  activar *Automatically delete head branches*.
- **Secrets** (*Settings → Secrets and variables → Actions*): `DOCKER_USERNAME` y `DOCKER_TOKEN`
  (token de acceso de Docker Hub con permiso de escritura).
