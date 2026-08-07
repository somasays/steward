"""run identity: payload, mandatory trace id, idempotency key

What issue #5 needs on the `runs` row for a run created over the API to be a
complete, traceable, replay-safe record:

* `payload` -- the goal's parameters. `RunCreate` carries them and the run is
  what they belong to; without a column they would have to be reconstructed
  from a task's payload, which makes the task the system of record for
  something the run owns (I1).
* `trace_id NOT NULL` -- I7 says every agent step is traced. A trace id is
  generated with no credentials and no network (`steward_telemetry.new_trace_id`),
  so "the exporter wasn't configured" is not a reason for a run to lack one.
  Making the column mandatory is what turns that from a convention into a
  constraint: an untraceable run is now unrepresentable. Existing rows are
  backfilled with random ids -- they predate any trace, and a synthetic id that
  resolves to nothing is more useful than a null that breaks the type.
* `idempotency_key` + a partial unique index -- SPEC.md §8 requires idempotency
  keys on every POST that creates a run. The key belongs on the row, not in an
  API process's memory: replays must converge across restarts and across
  replicas. Partial, so unkeyed runs (the common case) do not collide on NULL.

DDL is static SQL executed verbatim (I5).

Revision ID: 0002_run_identity
Revises: 0001_baseline
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_run_identity"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_PAYLOAD = """
ALTER TABLE runs ADD COLUMN payload jsonb NOT NULL DEFAULT '{}'::jsonb
"""

ADD_IDEMPOTENCY_KEY = """
ALTER TABLE runs ADD COLUMN idempotency_key text
"""

# 32 lowercase hex characters: the W3C trace-id shape `new_trace_id` produces.
BACKFILL_TRACE_ID = """
UPDATE runs SET trace_id = replace(gen_random_uuid()::text, '-', '') WHERE trace_id IS NULL
"""

REQUIRE_TRACE_ID = """
ALTER TABLE runs ALTER COLUMN trace_id SET NOT NULL
"""

CREATE_RUNS_IDEMPOTENCY_KEY = """
CREATE UNIQUE INDEX runs_idempotency_key ON runs (idempotency_key)
WHERE idempotency_key IS NOT NULL
"""

UPGRADE: tuple[str, ...] = (
    ADD_PAYLOAD,
    ADD_IDEMPOTENCY_KEY,
    BACKFILL_TRACE_ID,
    REQUIRE_TRACE_ID,
    CREATE_RUNS_IDEMPOTENCY_KEY,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX runs_idempotency_key",
    "ALTER TABLE runs ALTER COLUMN trace_id DROP NOT NULL",
    "ALTER TABLE runs DROP COLUMN idempotency_key",
    "ALTER TABLE runs DROP COLUMN payload",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
