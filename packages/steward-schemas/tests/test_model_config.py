"""Contract config invariants: frozen + extra="forbid" by default (issue #2
item 1), and RunBudget's fields are all required — no default can be read as
"unlimited" (I12).

Uses pytest.raises/parametrize: S1 (GUARDRAILS.md) scopes the schemas
independence contract to the installed package (`src/`), not `tests/`
(issue #12), so tests are free to import pytest (issue #13).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from steward_schemas import CONTRACTS, ProblemDetails, RunBudget

# ProblemDetails is the one documented exception to the default frozen +
# extra="forbid" config (see steward_schemas.errors.ProblemDetails).
FROZEN_FORBID_CONTRACT_NAMES = [name for name in sorted(CONTRACTS) if CONTRACTS[name] is not ProblemDetails]


@pytest.mark.parametrize("name", FROZEN_FORBID_CONTRACT_NAMES)
def test_default_contracts_are_frozen_and_forbid_extra(name: str) -> None:
    model_cls = CONTRACTS[name]
    assert model_cls.model_config.get("frozen") is True
    assert model_cls.model_config.get("extra") == "forbid"


def test_problem_details_documents_its_extra_allow_deviation() -> None:
    # RFC 9457 defines problem types as extensible; ProblemDetails is the one
    # documented exception to the package's default extra="forbid" rule.
    assert ProblemDetails.model_config.get("frozen") is True
    assert ProblemDetails.model_config.get("extra") == "allow"


def test_frozen_contract_rejects_mutation() -> None:
    run_budget = RunBudget(steps=1, tokens=1, cost_usd=Decimal("0.01"), wall_clock=timedelta(seconds=1))
    with pytest.raises(ValidationError):
        run_budget.steps = 2  # type: ignore[misc]


ALL_RUN_BUDGET_FIELDS = {
    "steps": 1,
    "tokens": 1,
    "cost_usd": Decimal("0.01"),
    "wall_clock": timedelta(seconds=1),
}


@pytest.mark.parametrize("missing_field", sorted(ALL_RUN_BUDGET_FIELDS))
def test_run_budget_has_no_defaults(missing_field: str) -> None:
    fields = {k: v for k, v in ALL_RUN_BUDGET_FIELDS.items() if k != missing_field}
    with pytest.raises(ValidationError):
        RunBudget(**fields)  # type: ignore[arg-type]
