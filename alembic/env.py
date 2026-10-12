"""Alembic environment.

The database URL comes from, in order: the sqlalchemy.url option set in code
by database.run_migrations(), then the DATABASE_URL environment variable.
No URL is stored in alembic.ini.
"""
import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

config = context.config

# Logging is only configured when running from the alembic.ini command line.
# When the app calls Alembic in code there is no ini file and the app keeps
# its own logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No autogenerate: the schema is written by hand in versions/.
target_metadata = None


def get_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Pin the driver: SQLAlchemy 2.1 maps a bare postgresql:// to psycopg 3,
    # and the app uses psycopg2. Also accept the legacy postgres:// scheme.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
            break
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (alembic upgrade --sql)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        # A caller passed an open connection through config.attributes.
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
