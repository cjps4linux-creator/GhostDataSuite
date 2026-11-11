"""Alembic environment configuration for Ghost-Data Suite.

This module configures Alembic to run database migrations against the
SQLite database used by the Ghost-Data Suite. It reads the database URL
from the ``DATABASE_URL`` environment variable, falling back to the
default SQLite path.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic Config object, provides access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------
# Prefer DATABASE_URL env var, fall back to the alembic.ini default.
db_url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
config.set_main_option("sqlalchemy.url", db_url)

# ---------------------------------------------------------------------------
# Target metadata
# ---------------------------------------------------------------------------
# Ghost-Data Suite uses raw sqlite3 (no SQLAlchemy declarative base), so
# we do not have an autogenerate target metadata.  Migrations are written
# by hand.  Set this to None so Alembic does not attempt autogeneration.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    # Ensure the parent directory exists for SQLite (SQLAlchemy won't create it).
    db_url = config.get_main_option("sqlalchemy.url")
    if db_url.startswith("sqlite:///"):
        db_path = db_url[len("sqlite:///"):]
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
