# app/core/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SERVICE_NAME: str = "payment-service"

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = ""
    KAFKA_API_KEY: str = ""
    KAFKA_API_SECRET: str = ""
    # Default = group_id histórico de producción, sin variable nueva en Render.
    # Local lo sobreescribe con sufijo "-local" — dev y prod comparten el
    # mismo cluster de Confluent Cloud, y sin distinguir el group_id ambos
    # entornos terminan en el MISMO grupo de consumidores.
    KAFKA_GROUP_ID: str = "payment-service-group"

    # PayU Latam sandbox credentials
    PAYU_MERCHANT_ID: str = "508029"
    PAYU_API_KEY: str = "4Vj8eK4rloUd272L48hsrarnUA"
    PAYU_API_LOGIN: str = "pRRXKOl8ikMmt9u"
    PAYU_ACCOUNT_ID: str = "512321"
    PAYU_BASE_URL: str = "https://sandbox.api.payulatam.com/payments-api/4.0/service.cgi"
    PAYU_CURRENCY: str = "COP"

    # Set to False to use the simulator (fallback when PayU creds not configured)
    PAYU_ENABLED: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
