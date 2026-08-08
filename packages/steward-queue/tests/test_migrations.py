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
from steward_schemas import SECRET_REF_PATTERN, AssetLifecycle, AssetType, SourceEngine

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
CATALOG_TABLES = {"sources", "assets", "columns", "profiles"}
ALL_TABLES = QUEUE_TABLES | CATALOG_TABLES

QUOTED_LITERAL = re.compile(r"'([^']*)'")

INSERT_SOURCE = """
INSERT INTO sources (id, workspace_id, name, engine, host, database_name,
                     include_schemas, exclude_schemas, dsn_secret_ref)
VALUES (gen_random_uuid(), '00000000-0000-0000-0000-000000000000', 'probe', 'postgres',
        'db.example.com', 'analytics', '{}'::text[], '{}'::text[], %(ref)s)
"""

A_DSN_WITH_A_PASSWORD = "postgresql://steward:hunter2@db.example.com:5432/analytics"


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


def test_the_catalog_revision_creates_its_tables_and_natural_keys(scratch_dsn: str) -> None:
    # I8: re-registering a source or rescanning a database converges on the
    # existing row because the database will not hold a second one.
    upgrade_to_head(scratch_dsn)
    assert CATALOG_TABLES <= names(scratch_dsn, SELECT_TABLES)
    assert {"sources_natural_key", "assets_natural_key", "columns_natural_key"} <= names(
        scratch_dsn, SELECT_INDEXES
    )


def test_the_profiles_revision_versions_one_profile_per_asset(scratch_dsn: str) -> None:
    """I8 for profiling: two writers cannot both create version N of an asset's
    profile, so the history is a total order rather than a fork.

    Asserted against the installed index for the same reason the CHECK
    constraints above are read out of the catalog: what protects production is
    what Postgres installed, not what the revision file says.
    """
    upgrade_to_head(scratch_dsn)
    assert "profiles" in names(scratch_dsn, SELECT_TABLES)
    assert "profiles_asset_version" in names(scratch_dsn, SELECT_INDEXES)


def test_a_source_row_cannot_hold_a_dsn(scratch_dsn: str) -> None:
    """N7/I5: a credential is not merely discouraged in `sources`, it is
    rejected by the database.

    The column holds a `scheme:name` reference into the secret store; every DSN
    shape carries `/` and `@`, which the CHECK does not admit. This is the
    constraint that makes "a DSN with a password must be impossible to read
    back out of the database" (issue #20) a property of the schema rather than
    of whoever writes the next INSERT.
    """
    upgrade_to_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(INSERT_SOURCE, {"ref": A_DSN_WITH_A_PASSWORD})
        conn.rollback()
        conn.execute(INSERT_SOURCE, {"ref": "env:STEWARD_SOURCE_DSN"})  # the reference form is fine
        conn.rollback()


def test_the_secret_reference_check_matches_the_published_contract(scratch_dsn: str) -> None:
    """One rule, three enforcement points -- and this is what stops them drifting.

    `steward_schemas.SECRET_REF_PATTERN` is validated by `SourceCreate`, parsed
    by `steward_catalog.secrets`, and enforced by this constraint. If the
    contract loosened and the column did not, a credential would be caught only
    by the database, whose rejection message quotes the failing row (N7).
    """
    upgrade_to_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        row = conn.execute(
            SELECT_CHECK_CONSTRAINT,
            {"table": "sources", "constraint": "sources_dsn_secret_ref_check"},
        ).fetchone()
    assert row is not None
    assert SECRET_REF_PATTERN in row[0]


def test_catalog_enums_match_their_check_constraints(scratch_dsn: str) -> None:
    # Same one-vocabulary rule the queue's state enums are held to: a lifecycle
    # the code can produce and the database rejects is a bug waiting for a scan.
    upgrade_to_head(scratch_dsn)
    assert allowed_values(scratch_dsn, "sources", "engine") == {e.value for e in SourceEngine}
    assert allowed_values(scratch_dsn, "assets", "asset_type") == {t.value for t in AssetType}
    assert allowed_values(scratch_dsn, "assets", "lifecycle") == {lc.value for lc in AssetLifecycle}
    assert allowed_values(scratch_dsn, "columns", "lifecycle") == {lc.value for lc in AssetLifecycle}


def test_migrations_are_reversible_and_reappliable(scratch_dsn: str) -> None:
    upgrade_to_head(scratch_dsn)
    downgrade_to_base(scratch_dsn)
    assert names(scratch_dsn, SELECT_TABLES) & ALL_TABLES == set()
    upgrade_to_head(scratch_dsn)
    assert ALL_TABLES <= names(scratch_dsn, SELECT_TABLES)


def test_a_config_without_a_url_fails_loudly(scratch_dsn: str) -> None:
    # "Checks fail loud, skip honest" (GUARDRAILS.md §3) applies to migrations
    # too: an unconfigured environment must not quietly do nothing.
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    with pytest.raises(RuntimeError, match="no database URL"):
        command.upgrade(config, "head")
