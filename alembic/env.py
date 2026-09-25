"""Alembic environment — payment-service-cinema (base cinema_payments).

La URL sale de DATABASE_URL (settings), nunca de alembic.ini. Si la app
aplica las migraciones al arrancar (MIGRATE_ON_START), pasa su propia
conexión en config.attributes["connection"] — ya con el advisory lock
tomado — y aquí se reutiliza.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings  # noqa: E402
from app.core.database import Base    # noqa: E402
import app.models.payment              # noqa: E402,F401 — registra la tabla payments

config = context.config
# configparser interpreta "%": se escapa por si la contraseña lo trae.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_with(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
