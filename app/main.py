# app/main.py
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from app.api.routes import internal_router, router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.core.migrations import migrate_to_head  # noqa: E402
from app.kafka.consumer import start_consumer  # noqa: E402
from app.kafka.producer import start_producer, stop_producer  # noqa: E402
from app.services.payments import reconcile  # noqa: E402

logger = logging.getLogger(__name__)

_reconciler_task: asyncio.Task | None = None


async def _run_reconciler() -> None:
    """Consulta en Wompi los pagos pendientes, por si no llegó el evento."""
    while True:
        try:
            with SessionLocal() as db:
                await reconcile(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Reconciliador de pagos: %s", exc)
        await asyncio.sleep(settings.PAYMENTS_RECONCILE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _reconciler_task
    if settings.MIGRATE_ON_START:
        await asyncio.to_thread(migrate_to_head)
    await start_producer()
    consumer_task = asyncio.create_task(start_consumer())
    _reconciler_task = asyncio.create_task(_run_reconciler())
    yield
    for task in (consumer_task, _reconciler_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await stop_producer()


app = FastAPI(title="Payment Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Métricas de Prometheus (latencia/conteo por endpoint) en /metrics
Instrumentator().instrument(app).expose(app)

app.include_router(router)
app.include_router(internal_router)


@app.get("/health")
async def health():
    kafka_enabled = bool(settings.KAFKA_BOOTSTRAP_SERVERS)
    reconciler_running = _reconciler_task is not None and not _reconciler_task.done()
    return {
        "status": "healthy",
        "service": "payment-service",
        "kafka": "configured" if kafka_enabled else "disabled",
        "gateway": "wompi",
        "reconciler": "running" if reconciler_running else "stopped",
    }
