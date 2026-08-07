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
    return (PlannedTask(task_type=NOOP_TASK_TYPE, payload={"echo": params.echo}),)


SCAN_SOURCE_GOAL = "scan_source"

SCAN_SOURCE_TASK_TYPE = "scan_source"
"""The task type `scan_source` plans; `steward_catalog` registers its handler
under the same name. Same seam as `NOOP_TASK_TYPE`, checked the same way."""

# What a `scan_source` run may spend (I12). Tight, and it can be, because the
# plan below is exactly one task: the per-task cap the queue enforces *is* the
# run cap, so what the API advertises for the run is what the run can spend.
# `tokens` and `cost_usd` are zero because a metadata-only scan calls no model
# -- if a later slice makes it call one, the budget has to be raised
# deliberately rather than being found to be slack. `wall_clock` is also the
# source connection's connect and statement timeout
# (`steward_catalog.inspector`), so a source that accepts a connection and then
# stops answering cannot outlive the cap.
SCAN_SOURCE_BUDGET = RunBudget(
    steps=1,
    tokens=0,
    cost_usd=Decimal("0.000000"),
    wall_clock=timedelta(minutes=10),
)


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
    """Expand `scan_source` into **exactly one** bounded task (#37).

    The obvious plan is a fan-out -- discover the schema, then one
    `profile_table` per table -- and SPEC.md §3.1 sketches it. It is wrong to
    ship it here: `RunPlan.task_specs` gives every planned task the run's whole
    budget, so an N-way fan-out lets one run spend N times the cap the API
    published for it (I12). Fan-out waits for run-level budget reservation, and
    a deterministic metadata scan does not need it to be correct.
    """
    return (PlannedTask(task_type=SCAN_SOURCE_TASK_TYPE, payload={"source_id": str(params.source_id)}),)
