"""Registered goals.

`noop` was the whole list at M0, and `scan_source` (issue #20) arrived exactly
as issue #19 predicted it would: another block in this file, and nothing else
in the system changed to admit it -- the API validates it, plans it and
enqueues it through the same registry path.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from steward_schemas import RunBudget

from steward_orchestration.registry import GoalParams, PlannedTask, goal

NOOP_GOAL = "noop"

NOOP_TASK_TYPE = "noop"
"""The task type `noop` plans.

The string is the seam between a planner and a handler: `steward_queue`
registers the handler under the same name, and the two packages agree on the
name rather than on an import -- the queue must not depend on orchestration
(it dispatches task types, it does not know goals exist) and orchestration does
not take a runtime dependency on the queue to borrow a constant. The agreement
is asserted, not assumed: `tests/test_goals.py` checks every registered goal's
allowlist against the queue's handler registry.
"""

NOOP_BUDGET = RunBudget(
    steps=32,
    tokens=200_000,
    cost_usd=Decimal("2.000000"),
    wall_clock=timedelta(minutes=15),
)
"""What a `noop` run may spend (I12).

Conservative rather than tight: `noop` runs no model, so every dimension here
is slack. It is stated anyway because a goal cannot be registered without hard
caps -- the budget being per-goal is what replaced the API store's single
hardcoded default (issue #19).
"""

NOOP_TASK_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=5),
)
"""What the one task a `noop` run plans may spend (issue #48).

Well inside `NOOP_BUDGET`, and deliberately so: the run budget is the pot, this
is the draw, and a goal that later plans a second task has room to declare one
without the run's advertised cap moving. The task budget, not the run's, is
what the queue enforces and what the worker's deadline is set from -- so the
handler that echoes a payload is bound by five minutes, not fifteen.
"""


class NoopParams(GoalParams):
    """`noop`'s parameters: a string to echo back, and nothing else.

    `echo` has a default, so `{}` is a valid payload; anything beyond it is
    not, because `GoalParams` forbids extras. That is the smallest schema that
    still demonstrates both rejections the boundary now makes.
    """

    echo: str = ""


@goal(
    NOOP_GOAL,
    params_model=NoopParams,
    allowed_task_types=[NOOP_TASK_TYPE],
    budget=NOOP_BUDGET,
    sample_payload={"echo": "steward"},
)
def plan_noop(params: NoopParams) -> tuple[PlannedTask, ...]:
    """Expand `noop` into the single task M0's exit criterion flows through."""
    return (PlannedTask(task_type=NOOP_TASK_TYPE, budget=NOOP_TASK_BUDGET, payload={"echo": params.echo}),)


SCAN_SOURCE_GOAL = "scan_source"

SCAN_SOURCE_TASK_TYPE = "scan_source"
"""The task type `scan_source` plans; `steward_catalog` registers its handler
under the same name. Same seam as `NOOP_TASK_TYPE`, checked the same way."""

# What a `scan_source` run may spend (I12). Tight, and it can be, because the
# plan below is exactly one task whose declared budget is this whole amount:
# the reservation (issue #48) is the run cap exactly, so what the API
# advertises for the run is what the run can spend, with nothing left over for
# a second task to be added quietly.
# `tokens` and `cost_usd` are zero because a metadata-only scan calls no model
# -- if a later slice makes it call one, the budget has to be raised
# deliberately rather than being found to be slack. `wall_clock` is also the
# source connection's connect and statement timeout
# (`steward_catalog.inspector`), which reads it off the *task* spec, so a
# source that accepts a connection and then stops answering cannot outlive the
# cap.
SCAN_SOURCE_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=10),
)

SCAN_SOURCE_TASK_BUDGET = SCAN_SOURCE_BUDGET
"""What the one task a `scan_source` run plans may spend.

The run's whole budget, because the plan is one task -- the degenerate case of
the reservation, and the only shape in which a task budget and a run budget are
allowed to be equal. Fanning out later means splitting this, not repeating it:
`plan` refuses a second task that does not come out of the same pot (#48).
"""


class ScanSourceParams(GoalParams):
    """`scan_source`'s parameters: which registered source to scan.

    A `UUID`, not a string, so a malformed id is a 422 at the boundary rather
    than a task that fails on a worker twenty seconds later (I3).
    """

    source_id: UUID


@goal(
    SCAN_SOURCE_GOAL,
    params_model=ScanSourceParams,
    allowed_task_types=[SCAN_SOURCE_TASK_TYPE],
    budget=SCAN_SOURCE_BUDGET,
    sample_payload={"source_id": "00000000-0000-0000-0000-000000000000"},
)
def plan_scan_source(params: ScanSourceParams) -> tuple[PlannedTask, ...]:
    """Expand `scan_source` into **exactly one** bounded task -- by choice (#48).

    The obvious plan is a fan-out -- discover the schema, then one
    `profile_table` per table -- and SPEC.md §3.1 sketches it. Until #48 that
    plan was *unrepresentable* safely: every planned task got the run's whole
    budget, so an N-way fan-out let one run spend N times the cap the API
    published for it (I12), and #20 shipped one task because one task was the
    only honest shape. That constraint is gone: a fan-out now declares a budget
    per branch and is refused if the branches do not fit the run.

    What keeps this plan single-task is that a deterministic metadata scan has
    nothing to gain from splitting. One round trip enumerates every table in a
    schema, so N tasks would be N connections to the customer's database for
    work one connection already does, and the convergence diff (`plan_convergence`)
    is computed against the whole observed catalog at once -- a per-table task
    would have to either re-read the rest or give up detecting a dropped table.
    Profiling (#49) is the opposite shape -- per-column work, per-column cost --
    and that is the fan-out this unblocked.
    """
    return (
        PlannedTask(
            task_type=SCAN_SOURCE_TASK_TYPE,
            budget=SCAN_SOURCE_TASK_BUDGET,
            payload={"source_id": str(params.source_id)},
        ),
    )
