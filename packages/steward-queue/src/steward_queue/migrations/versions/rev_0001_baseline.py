"""baseline: runs, tasks, checkpoints, audit_log

The M0 slice of SPEC.md §7's data model -- the four tables the runtime needs
before any catalog entity exists.

Design notes worth keeping:

* `runs` carries its budget columns from the very first revision (I12): there
  is no schema state in which a run could exist without hard caps, so
  "budgets were added later" is not a migration anyone can write.
* `tasks` carries the same four budget columns because `TaskSpec` hands a
  worker a per-task cap; `runs.used_*` is where consumption accumulates.
* `(run_id, dedup_key)` is unique: enqueue is idempotent within a run, which
  is what lets a retried orchestrator transaction converge on one task
  instead of a duplicate (I8).
* Partial indexes on the two hot predicates -- claimable and lease-expired --
  so `SKIP LOCKED` claiming does not degrade into a full scan as terminal
  rows accumulate.

DDL is static SQL executed verbatim (I5); it is not assembled from strings.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_RUNS = """
CREATE TABLE runs (
    id uuid PRIMARY KEY,
    goal text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    budget_steps integer NOT NULL CHECK (budget_steps >= 0),
    budget_tokens integer NOT NULL CHECK (budget_tokens >= 0),
    budget_cost_usd numeric(14, 6) NOT NULL CHECK (budget_cost_usd >= 0),
    budget_wall_clock interval NOT NULL CHECK (budget_wall_clock >= interval '0'),
    used_steps integer NOT NULL DEFAULT 0 CHECK (used_steps >= 0),
    used_tokens integer NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
    used_cost_usd numeric(14, 6) NOT NULL DEFAULT 0 CHECK (used_cost_usd >= 0),
    used_wall_clock interval NOT NULL DEFAULT interval '0' CHECK (used_wall_clock >= interval '0'),
    trace_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_TASKS = """
CREATE TABLE tasks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    task_type text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL
        CHECK (state IN ('pending', 'claimed', 'running', 'succeeded', 'failed', 'dead')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts >= 1),
    dedup_key text NOT NULL,
    budget_steps integer NOT NULL CHECK (budget_steps >= 0),
    budget_tokens integer NOT NULL CHECK (budget_tokens >= 0),
    budget_cost_usd numeric(14, 6) NOT NULL CHECK (budget_cost_usd >= 0),
    budget_wall_clock interval NOT NULL CHECK (budget_wall_clock >= interval '0'),
    available_at timestamptz NOT NULL DEFAULT now(),
    claimed_by text,
    claimed_at timestamptz,
    lease_expires_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    result jsonb,
    last_error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_TASKS_DEDUP = """
CREATE UNIQUE INDEX tasks_run_dedup_key ON tasks (run_id, dedup_key)
"""

CREATE_TASKS_CLAIMABLE = """
CREATE INDEX tasks_claimable ON tasks (available_at, created_at) WHERE state = 'pending'
"""

CREATE_TASKS_LEASE = """
CREATE INDEX tasks_lease ON tasks (lease_expires_at) WHERE state IN ('claimed', 'running')
"""

CREATE_CHECKPOINTS = """
CREATE TABLE checkpoints (
    task_id uuid NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    step integer NOT NULL CHECK (step >= 0),
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, step)
)
"""

CREATE_AUDIT_LOG = """
CREATE TABLE audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_kind text NOT NULL CHECK (actor_kind IN ('human', 'agent', 'policy', 'system')),
    actor_id text NOT NULL,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    before jsonb,
    after jsonb,
    at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_AUDIT_LOG_ENTITY = """
CREATE INDEX audit_log_entity ON audit_log (entity_type, entity_id, at)
"""

UPGRADE: tuple[str, ...] = (
    CREATE_RUNS,
    CREATE_TASKS,
    CREATE_TASKS_DEDUP,
    CREATE_TASKS_CLAIMABLE,
    CREATE_TASKS_LEASE,
    CREATE_CHECKPOINTS,
    CREATE_AUDIT_LOG,
    CREATE_AUDIT_LOG_ENTITY,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE audit_log",
    "DROP TABLE checkpoints",
    "DROP TABLE tasks",
    "DROP TABLE runs",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
