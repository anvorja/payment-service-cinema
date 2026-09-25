# app/core/config.py
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SERVICE_NAME: str = "payment-service"

    # Base propia (cinema_payments): historial de pagos, dueño único este servicio.
    DATABASE_URL: str = Field(min_length=1)
    # Aplica las migraciones de Alembic al arrancar (con un advisory lock, así
    # varias réplicas no migran a la vez). Ver README, "Migraciones".
    MIGRATE_ON_START: bool = False

    # JWT emitido por auth-service (mismo secreto) — identifica a quien consulta
    # su historial, verifica un pago o paga una recarga.
    JWT_SECRET: str = Field(min_length=1)
    JWT_ALGORITHM: str = "HS256"
    # Redis compartido con auth-service: blacklist de tokens tras logout.
    REDIS_URL: str = ""

    # Orígenes del frontend que pueden llamar a /api/v1/payments, separados por
    # coma (en producción el gateway también aplica su propio CORS).
    CORS_ORIGINS: str = ""

    # Secreto compartido servicio-a-servicio (rutas /internal/*), igual que en
    # booking-service. Sin él, las rutas internas rechazan todo.
    INTERNAL_SERVICE_TOKEN: str = ""

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = ""
    KAFKA_API_KEY: str = ""
    KAFKA_API_SECRET: str = ""
    # Default = group_id histórico de producción, sin variable nueva en Render.
    # Local lo sobreescribe con sufijo "-local" — dev y prod comparten el
    # mismo cluster de Confluent Cloud, y sin distinguir el group_id ambos
    # entornos terminan en el MISMO grupo de consumidores.
    KAFKA_GROUP_ID: str = "payment-service-group"

    # Wompi (https://docs.wompi.co). Llaves del comercio en comercios.wompi.co →
    # Desarrolladores. Sandbox: pub_test_ / prv_test_ / test_integrity_ / test_events_.
    WOMPI_PUBLIC_KEY: str = Field(min_length=1)
    # Consultar y anular transacciones exige la llave privada (desde el backend).
    WOMPI_PRIVATE_KEY: str = Field(min_length=1)
    WOMPI_INTEGRITY_SECRET: str = Field(min_length=1)
    WOMPI_EVENTS_SECRET: str = Field(min_length=1)
    WOMPI_API_URL: str = Field(min_length=1)
    WOMPI_CHECKOUT_URL: str = Field(min_length=1)
    WOMPI_TIMEOUT_SECONDS: float = 8.0

    # Página del frontend a la que Wompi devuelve (agrega ?id=<transacción>).
    # Wompi no acepta localhost: en local se usa lvh.me.
    PAYMENTS_REDIRECT_URL: str = Field(min_length=1)
    # Prefijo de las referencias: <prefijo>-<uuid>.
    PAYMENTS_REFERENCE_PREFIX: str = "cinemaplus"
    PAYMENTS_CURRENCY: str = "COP"
    # Minutos que se puede pagar el enlace de una recarga Cinema+. El de las
    # boletas lo fija booking-service (es quien retiene los asientos).
    RECHARGE_CHECKOUT_TTL_MINUTES: int = Field(default=30, ge=5, le=1440)
    # Montos permitidos para una recarga Cinema+ (pesos).
    RECHARGE_MIN_AMOUNT: int = Field(default=10_000, gt=0)
    RECHARGE_MAX_AMOUNT: int = Field(default=1_000_000, gt=0)
    # Cada cuánto se consultan en Wompi los pagos pendientes (por si no llegó
    # el evento ni la persona volvió de Wompi).
    PAYMENTS_RECONCILE_INTERVAL_SECONDS: int = Field(default=60, ge=10)
    # Tras vencer el enlace se sigue consultando este tiempo: una transacción
    # que empezó a tiempo (un PSE lento) puede resolverse después.
    PAYMENTS_RECONCILE_GRACE_MINUTES: int = Field(default=30, ge=0)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
