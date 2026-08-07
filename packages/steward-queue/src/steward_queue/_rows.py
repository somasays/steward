"""Translation between the tuple rows the statements return and typed values.

One job, shared by both aggregates because both store the same budget columns:
a `RunBudget` as SQL parameters, a row's budget columns back as a `RunBudget`,
and narrowing a `RETURNING` row the statement guarantees exists. Private,
because the shape of a row is an implementation detail of `_sql`, not something
this package offers.
"""

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from steward_schemas import RunBudget


def _require_row(row: Sequence[Any] | None, what: str) -> Sequence[Any]:
    """Narrow a `RETURNING` result that the statement guarantees exists."""
    if row is None:  # pragma: no cover -- unreachable unless the schema drifts
        raise RuntimeError(what)
    return row


def _budget_params(budget: RunBudget) -> dict[str, Any]:
    return {
        "budget_steps": budget.steps,
        "budget_tokens": budget.tokens,
        "budget_cost_usd": budget.cost_usd,
        "budget_wall_clock": budget.wall_clock,
    }


def _budget_from(steps: int, tokens: int, cost_usd: Decimal, wall_clock: timedelta) -> RunBudget:
    return RunBudget(steps=steps, tokens=tokens, cost_usd=cost_usd, wall_clock=wall_clock)
