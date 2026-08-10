"""tasks.used_*: what one task has spent, across all of its attempts

Retry admission (#69) asks a question the schema could not answer: may this run
afford another attempt at this task? Answering it with the task's *whole*
budget assumes a retry starts from nothing, which is wrong for anything that
checkpoints -- a resumed agent continues against the same cumulative cap and can
only spend what is left of it. Projecting the full budget therefore made every
failure that spent anything unaffordable the moment it was recorded, which for a
goal whose single task carries the run's whole budget (the degenerate
reservation, SPEC.md §13 D9) meant no task with non-zero usage ever retried.

So a task accumulates its own spend, the same way a run does, and admission
projects the *remainder* -- `budget - used`, floored at zero per dimension.

Two notes on the shape:

* **Mirrors `runs.used_*` exactly**, including the `numeric(14, 6)` for cost and
  the interval for wall clock. Two accumulators of the same quantity that
  disagree about representation is how a rounding difference becomes a budget
  difference.
* **No back-fill, and none is needed.** Existing rows default to zero, which is
  what they in fact spent as far as any record goes: before this revision a
  task's usage was never stored anywhere. A back-fill would have to invent it.

DDL is static SQL executed verbatim (I5); it is not assembled from strings.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_task_usage"
down_revision: str | None = "0004_profiles"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

ADD_TASK_USAGE = """
ALTER TABLE tasks
    ADD COLUMN used_steps integer NOT NULL DEFAULT 0 CHECK (used_steps >= 0),
    ADD COLUMN used_tokens integer NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
    ADD COLUMN used_cost_usd numeric(14, 6) NOT NULL DEFAULT 0 CHECK (used_cost_usd >= 0),
    ADD COLUMN used_wall_clock interval NOT NULL DEFAULT interval '0'
        CHECK (used_wall_clock >= interval '0')
"""

DROP_TASK_USAGE = """
ALTER TABLE tasks
    DROP COLUMN used_steps,
    DROP COLUMN used_tokens,
    DROP COLUMN used_cost_usd,
    DROP COLUMN used_wall_clock
"""

UPGRADE: tuple[str, ...] = (ADD_TASK_USAGE,)

DOWNGRADE: tuple[str, ...] = (DROP_TASK_USAGE,)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
