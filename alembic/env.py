"""
Async Alembic environment.
Dynamically uses settings.DATABASE_URL in production with asyncpg sanitization.
"""

import asyncio
import os
import sys
import urllib.parse
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Ensure the backend root is importable regardless of cwd
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.movie.models import *  # noqa: E402, F401, F403


def _clean_url_for_alembic(url: str) -> str:
    if not url:
        return url
    clean_url = url
    if clean_url.startswith("postgres://"):
        clean_url = clean_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif clean_url.startswith("postgresql://") and not clean_url.startswith("postgresql+asyncpg://"):
        clean_url = clean_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urllib.parse.urlsplit(clean_url)
    if not parsed.query:
        return clean_url

    query_params = urllib.parse.parse_qs(parsed.query)
    filtered_params: dict[str, list[str]] = {}

    for key, values in query_params.items():
        if key == "sslmode":
            filtered_params["ssl"] = values
        elif key in ("channel_binding", "gssencmode", "target_session_attrs"):
            continue
        else:
            filtered_params[key] = values

    flat_query = []
    for key, val_list in filtered_params.items():
        for val in val_list:
            flat_query.append(f"{key}={val}")

    new_query = "&".join(flat_query)
    return urllib.parse.urlunsplit(parsed._replace(query=new_query))


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)
current_url = config.get_main_option("sqlalchemy.url")
if current_url and current_url != "sqlite+aiosqlite:///./rakki.db":
    config.set_main_option("sqlalchemy.url", _clean_url_for_alembic(current_url))
elif getattr(settings, "DATABASE_URL", None):
    config.set_main_option(
        "sqlalchemy.url", _clean_url_for_alembic(settings.DATABASE_URL)
    )
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()