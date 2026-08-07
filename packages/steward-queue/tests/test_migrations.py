"""The migrations, applied and reversed on a database of its own.

Runs against a scratch database so the session's migrated schema -- which every
other test depends on -- is never dropped underneath it.
"""

import re

import pgserver
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from steward_queue import RunStatus, TaskState
from steward_queue.migrate import MIGRATIONS_DIR, downgrade_to_base, upgrade_to_head

SELECT_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
SELECT_INDEXES = "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"

SELECT_CHECK_CONSTRAINT = """
SELECT pg_get_constraintdef(c.oid)
FROM pg_constraint AS c
JOIN pg_class AS t ON t.oid = c.conrelid
WHERE c.contype = 'c' AND t.relname = %(table)s AND c.conname = %(constraint)s
"""

SELECT_NOT_NULL = """
SELECT attnotnull
FROM pg_attribute
WHERE attrelid = %(table)s::regclass AND attname = %(column)s
"""

QUEUE_TABLES = {"runs", "tasks", "checkpoints", "audit_log"}

QUOTED_LITERAL = re.compile(r"'([^']*)'")


def names(dsn: str, sql: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {row[0] for row in conn.execute(sql).fetchall()}


def allowed_values(dsn: str, table: str, column: str) -> set[str]:
    """The literals a column's membership `CHECK` constraint permits.

    Read out of the live catalog rather than out of the revision file: what
    constrains production is the constraint Postgres actually installed, and a
    test that parsed the Python would keep passing against a hand-edited
    database or a revision that failed to apply. Postgres rewrites the
    `IN (...)` the migration wrote as `= ANY (ARRAY[...])`, so the values are
    pulled out of the normalised definition by their quoting.
    """
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            SELECT_CHECK_CONSTRAINT, {"table": table, "constraint": f"{table}_{column}_check"}
        ).fetchone()
    assert row is not None, f"no {table}_{column}_check constraint"
    return set(QUOTED_LITERAL.findall(row[0]))


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
    assert "runs_idempotency_key" in indexes


def test_a_run_row_cannot_exist_without_a_trace_id(scratch_dsn: str) -> None:
    # I7: the guarantee is a NOT NULL constraint, not a convention in the
    # writer -- so no future caller can create an untraceable run.
    upgrade_to_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        row = conn.execute(SELECT_NOT_NULL, {"table": "runs", "column": "trace_id"}).fetchone()
    assert row is not None and row[0] is True


def test_run_status_enum_matches_the_check_constraint(scratch_dsn: str) -> None:
    """The enum the code branches on and the constraint the database enforces
    are one vocabulary or they are a bug waiting for a deploy.

    Drift in either direction is caught: a status added to `RunStatus` and not
    to the migration fails on the first INSERT that uses it, and one added to
    the migration and not to the enum is dead schema no writer can produce.
    """
    upgrade_to_head(scratch_dsn)
    assert allowed_values(scratch_dsn, "runs", "status") == {s.value for s in RunStatus}


def test_task_state_enum_matches_the_check_constraint(scratch_dsn: str) -> None:
    upgrade_to_head(scratch_dsn)
    assert allowed_values(scratch_dsn, "tasks", "state") == {s.value for s in TaskState}


def test_migrations_are_reversible_and_reappliable(scratch_dsn: str) -> None:
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
