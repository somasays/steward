"""Alembic runtime environment.

There is no `target_metadata`: the schema is authored as SQL, not as SQLAlchemy
Core tables, so autogenerate is deliberately unavailable and revisions are
written by hand. SQLAlchemy appears here only as the engine Alembic drives --
it is not how this package talks to Postgres (see `steward_queue.db`).
"""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from steward_queue.migrate import sqlalchemy_url


def _url() -> str:
    dsn = context.config.attributes.get("dsn")
    if dsn is None:
        dsn = context.config.get_main_option("sqlalchemy.url")
    if not dsn:
        raise RuntimeError("no database URL: pass dsn via Config.attributes or sqlalchemy.url")
    return sqlalchemy_url(str(dsn))


def run_migrations_offline() -> None:  # pragma: no cover -- SQL-emitting mode, not used by the app
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():  # pragma: no cover -- see run_migrations_offline
    run_migrations_offline()
else:
    run_migrations_online()
