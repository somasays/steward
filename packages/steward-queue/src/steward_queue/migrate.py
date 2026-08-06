"""Programmatic access to the queue's Alembic migrations.

Callers (the API service on boot, the integration fixture, an operator) run
`upgrade_to_head(dsn)` rather than shelling out to the CLI, so the migration
path exercised in tests is the one that runs in production.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_PSYCOPG_DRIVER = "postgresql+psycopg://"
_SCHEME_PREFIXES = ("postgresql://", "postgres://")


def sqlalchemy_url(dsn: str) -> str:
    """Bind a libpq DSN to the psycopg 3 driver SQLAlchemy should use.

    Alembic drives a SQLAlchemy engine; the rest of this package does not.
    Pinning the driver here stops Alembic from silently picking psycopg2 --
    which is not a dependency -- when a plain `postgresql://` URL is passed.
    """
    if dsn.startswith(_PSYCOPG_DRIVER):
        return dsn
    for prefix in _SCHEME_PREFIXES:
        if dsn.startswith(prefix):
            return _PSYCOPG_DRIVER + dsn[len(prefix) :]
    raise ValueError(f"not a PostgreSQL DSN: {dsn!r}")


def alembic_config(dsn: str) -> Config:
    """An Alembic config bound to this package's revisions and `dsn`.

    The DSN travels in `attributes`, not `sqlalchemy.url`: the ini file is
    read by configparser, where a `%` in a password would be interpolation
    syntax rather than a character.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["dsn"] = dsn
    return config


def upgrade_to_head(dsn: str) -> None:
    """Bring `dsn`'s database to the latest revision."""
    command.upgrade(alembic_config(dsn), "head")


def downgrade_to_base(dsn: str) -> None:
    """Drop every queue table `dsn`'s database has. Used by tests and teardown."""
    command.downgrade(alembic_config(dsn), "base")
