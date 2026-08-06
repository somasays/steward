"""The baseline migration, applied and reversed on a database of its own.

Runs against a scratch database so the session's migrated schema -- which every
other test depends on -- is never dropped underneath it.
"""

import pgserver
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from steward_queue.migrate import MIGRATIONS_DIR, downgrade_to_base, upgrade_to_head

SELECT_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
SELECT_INDEXES = "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"

QUEUE_TABLES = {"runs", "tasks", "checkpoints", "audit_log"}


def names(dsn: str, sql: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {row[0] for row in conn.execute(sql).fetchall()}


@pytest.fixture
def scratch_dsn(pg_server: pgserver.PostgresServer) -> str:
    pg_server.psql("DROP DATABASE IF EXISTS migration_probe")
    pg_server.psql("CREATE DATABASE migration_probe")
    uri: str = pg_server.get_uri(database="migration_probe")
    return uri


def test_baseline_creates_the_queue_schema(scratch_dsn: str) -> None:
    upgrade_to_head(scratch_dsn)
    assert QUEUE_TABLES <= names(scratch_dsn, SELECT_TABLES)
    indexes = names(scratch_dsn, SELECT_INDEXES)
    assert {"tasks_run_dedup_key", "tasks_claimable", "tasks_lease"} <= indexes


def test_baseline_is_reversible_and_reappliable(scratch_dsn: str) -> None:
    upgrade_to_head(scratch_dsn)
    downgrade_to_base(scratch_dsn)
    assert names(scratch_dsn, SELECT_TABLES) & QUEUE_TABLES == set()
    upgrade_to_head(scratch_dsn)
    assert QUEUE_TABLES <= names(scratch_dsn, SELECT_TABLES)


def test_a_config_without_a_url_fails_loudly(scratch_dsn: str) -> None:
    # "Checks fail loud, skip honest" (GUARDRAILS.md §3) applies to migrations
    # too: an unconfigured environment must not quietly do nothing.
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    with pytest.raises(RuntimeError, match="no database URL"):
        command.upgrade(config, "head")
