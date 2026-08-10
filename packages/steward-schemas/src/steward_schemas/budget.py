"""RunBudget — the hard autonomy limits enforced on every agent run (I12).

Every field is required: a caller must state an explicit cap for every
resource dimension. There is no "unspecified" or defaulted value that could
be read as "unlimited" — the runtime that consumes this contract has nothing
to interpret, only limits to enforce.

The two operations budgets are compared and combined with live here, on the
type, rather than in each package that needs them. There is exactly one list
of what the dimensions *are*, so a fifth one is added in one place and every
check that enforces a cap picks it up — a duplicated tuple of field names in
`steward_orchestration` and `steward_queue` is how a dimension gets silently
left unenforced (I12).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from pydantic import Field

from steward_schemas._base import SchemaModel


class RunBudget(SchemaModel):
    """Hard caps on one bounded agent run or task.

    Also reused by `TaskResult.usage` to report what was actually consumed
    (ARCHITECTURE.md I12; SPEC.md §3.2) — same shape, no separate type needed
    for "amount spent" vs. "amount allowed".
    """

    steps: int = Field(ge=0)
    """Maximum (or consumed) number of agent-loop steps."""

    tokens: int = Field(ge=0)
    """Maximum (or consumed) total LLM tokens (prompt + completion)."""

    cost_usd: Decimal = Field(ge=0)
    """Maximum (or consumed) cost in US dollars, as tracked by the LLM gateway."""

    wall_clock: timedelta = Field(ge=timedelta(0))
    """Maximum (or consumed) wall-clock duration.

    Summed across tasks (`total`) this is **aggregate task time**, not a run's
    elapsed duration: two tasks that run at the same time on two workers cost
    two tasks' worth of it while the clock on the wall advances once. Treating
    it as additive is the conservative reading — it is exactly right when the
    tasks run one after another, and an over-estimate when they do not.
    """

    def over(self, cap: RunBudget) -> tuple[str, ...]:
        """The dimensions in which this budget exceeds `cap`, in field order.

        The one comparison every hard limit is decided by: the plan-time
        reservation check in `steward_orchestration` and the runtime's
        per-task usage check in `steward_queue` both ask this. It names the
        dimensions rather than answering yes or no because I12 requires the
        failure to be *visible* — an operator needs to know which cap was
        blown, not that one was.
        """
        breached = (
            ("steps", self.steps > cap.steps),
            ("tokens", self.tokens > cap.tokens),
            ("cost_usd", self.cost_usd > cap.cost_usd),
            ("wall_clock", self.wall_clock > cap.wall_clock),
        )
        return tuple(dimension for dimension, exceeded in breached if exceeded)

    def remaining(self, spent: RunBudget) -> RunBudget:
        """What is left of this budget after `spent`, floored at zero.

        The third question asked of these four dimensions, alongside `over` and
        `total`, and it lives here for the same reason they do: a caller that
        subtracted field by field would be a second place a fifth dimension has
        to be remembered.

        Flooring matters. A dimension already overspent has *nothing* left, not
        a negative allowance that would quietly fund an overrun somewhere else
        when this value is summed with another (I12).
        """
        return RunBudget(
            steps=max(0, self.steps - spent.steps),
            tokens=max(0, self.tokens - spent.tokens),
            cost_usd=max(Decimal(0), self.cost_usd - spent.cost_usd),
            wall_clock=max(timedelta(0), self.wall_clock - spent.wall_clock),
        )

    @classmethod
    def total(cls, budgets: Iterable[RunBudget]) -> RunBudget:
        """The dimension-wise sum of `budgets` — an empty one sums to zero.

        Used to add up what a plan's tasks reserve, and what a run's tasks
        have consumed, which are the same arithmetic and must stay so: a
        reservation computed differently from the usage it bounds is a bound
        that does not hold.
        """
        items = tuple(budgets)
        return cls(
            steps=sum(budget.steps for budget in items),
            tokens=sum(budget.tokens for budget in items),
            cost_usd=sum((budget.cost_usd for budget in items), Decimal(0)),
            wall_clock=sum((budget.wall_clock for budget in items), timedelta()),
        )
