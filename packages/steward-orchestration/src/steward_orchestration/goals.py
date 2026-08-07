"""Registered goals.

`noop` is the whole list, and that is the point of issue #19: this module
delivers the mechanism with the goal M0 already ships as its first subject.
`scan_source` and the catalog it expands into are issue #20 -- they arrive as
another block in this file (or a sibling module imported by `__init__`), and
nothing else in the system changes to admit them.
"""

from datetime import timedelta
from decimal import Decimal

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
)
def plan_noop(params: NoopParams) -> tuple[PlannedTask, ...]:
    """Expand `noop` into the single task M0's exit criterion flows through."""
    return (PlannedTask(task_type=NOOP_TASK_TYPE, payload={"echo": params.echo}),)
