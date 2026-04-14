# app/main.py
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.kafka.consumer import start_consumer
from app.kafka.producer import start_producer, stop_producer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from app.core.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await start_producer()
    consumer_task = asyncio.create_task(start_consumer())
    yield
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
    await stop_producer()


app = FastAPI(title="Payment Service", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    kafka_enabled = bool(settings.KAFKA_BOOTSTRAP_SERVERS)
    return {
        "status": "healthy",
        "service": "payment-service",
        "kafka": "configured" if kafka_enabled else "disabled",
    }
