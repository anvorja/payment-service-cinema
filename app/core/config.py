# app/core/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SERVICE_NAME: str = "payment-service"

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
    }


settings = Settings()
