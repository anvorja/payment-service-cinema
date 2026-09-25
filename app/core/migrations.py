# app/core/migrations.py — aplica las migraciones de Alembic al arrancar.
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.database import engine

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
# Clave arbitraria pero fija: dos réplicas que arrancan a la vez no migran a la vez.
_LOCK_KEY = 72_003_001


def migrate_to_head() -> None:
    """
    Alembic registra cada migración aplicada en alembic_version: cada una
    corre una sola vez, y una base al día no cambia. El advisory lock
    serializa réplicas concurrentes.
    """
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
        try:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
            connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
            connection.commit()
    logger.info("Migraciones de cinema_payments al día")
