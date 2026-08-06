"""AgentSpec — the declaration of one agent type's bounded execution
contract (SPEC.md §3.2, §3.3): which model alias it runs on, which tools it
may call, and the hard budget every run of it is subject to."""

from steward_schemas._base import SchemaModel
from steward_schemas.budget import RunBudget


class AgentSpec(SchemaModel):
    """Declares one agent type (e.g. "profiler", "documentarian").

    `model_alias` is a gateway alias (`steward-reasoning`, `steward-fast`,
    ...), never a provider/model name — provider access is gateway-only
    (I2), so this contract can't even express a bypass. `tools` is the
    explicit least-privilege allowlist SPEC.md §3.2 requires (e.g. the
    Classifier's list excludes `run_profile_sql`).
    """

    name: str
    model_alias: str
    tools: tuple[str, ...]
    limits: RunBudget
