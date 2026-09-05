from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from database.settings import DatabaseApplicationName, DatabaseSettings
from database.tables import DatabaseBase
from sqlalchemy import Connection, engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = DatabaseBase.metadata


def _database_url() -> str:
    override = config.attributes.get("database_url")
    if isinstance(override, str) and override:
        return override
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return DatabaseSettings(application_name=DatabaseApplicationName.Bootstrap).direct().url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if isinstance(provided_connection, Connection):
        context.configure(connection=provided_connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    database_url = _database_url()
    section["sqlalchemy.url"] = database_url
    application_name = config.attributes.get("database_application_name")
    connect_args = (
        {"application_name": application_name}
        if database_url.startswith("postgresql") and isinstance(application_name, str)
        else {}
    )
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
