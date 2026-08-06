"""RunBudget — the hard autonomy limits enforced on every agent run (I12).

Every field is required: a caller must state an explicit cap for every
resource dimension. There is no "unspecified" or defaulted value that could
be read as "unlimited" — the runtime that consumes this contract has nothing
to interpret, only limits to enforce.
"""

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
    """Maximum (or consumed) wall-clock duration."""
