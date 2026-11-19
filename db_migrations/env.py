from __future__ import annotations

import importlib
import os
from pathlib import Path
from alembic import context
import sqlalchemy as sa


config = context.config


def migration_url() -> str:
    url = (
        config.attributes.get("database_url")
        or
        os.getenv("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if not url:
        raise RuntimeError("Set DATABASE_URL or sqlalchemy.url before running migrations.")
    if url == "sqlite:///data/prepsage.db":
        url = f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'prepsage.db'}"
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    return url


def desired_metadata():
    module_name = (
        config.get_main_option("target_metadata_module")
        or "app"
    )
    models = importlib.import_module(f"{module_name}.models")
    desired = sa.MetaData()
    seen = set()
    for value in vars(models).values():
        metadata = getattr(value, "metadata", None)
        if not isinstance(metadata, sa.MetaData) or not metadata.tables:
            continue
        if id(metadata) in seen:
            continue
        seen.add(id(metadata))
        for table in metadata.tables.values():
            if table.key not in desired.tables:
                table.to_metadata(desired)
    return desired


target_metadata = desired_metadata()


def run_migrations_offline():
    context.configure(
        url=migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = sa.create_engine(migration_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
