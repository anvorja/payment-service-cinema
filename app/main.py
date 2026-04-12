# app/main.py
import logging
from fastapi import FastAPI
from app.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(title="Payment Service")
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "payment-service"}
