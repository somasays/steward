"""Contract config invariants: frozen + extra="forbid" by default (issue #2
item 1), and RunBudget's fields are all required — no default can be read as
"unlimited" (I12).

Plain `test_*` functions, no third-party test framework import — see the
note in test_roundtrip.py (I4, enforced by S1 across this whole package).
"""

from datetime import timedelta
from decimal import Decimal

from pydantic import ValidationError
from steward_schemas import CONTRACTS, ProblemDetails, RunBudget


def test_default_contracts_are_frozen_and_forbid_extra() -> None:
    for name in sorted(CONTRACTS):
        if CONTRACTS[name] is ProblemDetails:
            continue
        model_cls = CONTRACTS[name]
        assert model_cls.model_config.get("frozen") is True, name
        assert model_cls.model_config.get("extra") == "forbid", name


def test_problem_details_documents_its_extra_allow_deviation() -> None:
    # RFC 9457 defines problem types as extensible; ProblemDetails is the one
    # documented exception to the package's default extra="forbid" rule.
    assert ProblemDetails.model_config.get("frozen") is True
    assert ProblemDetails.model_config.get("extra") == "allow"


def test_frozen_contract_rejects_mutation() -> None:
    run_budget = RunBudget(steps=1, tokens=1, cost_usd=Decimal("0.01"), wall_clock=timedelta(seconds=1))
    try:
        run_budget.steps = 2  # type: ignore[misc]
    except ValidationError:
        pass
    else:
        raise AssertionError("expected mutating a frozen model to raise ValidationError")


def test_run_budget_has_no_defaults() -> None:
    all_fields = {
        "steps": 1,
        "tokens": 1,
        "cost_usd": Decimal("0.01"),
        "wall_clock": timedelta(seconds=1),
    }
    for missing_field in all_fields:
        fields = {k: v for k, v in all_fields.items() if k != missing_field}
        try:
            RunBudget(**fields)  # type: ignore[arg-type]
        except ValidationError:
            pass
        else:
            raise AssertionError(f"expected RunBudget without {missing_field!r} to raise ValidationError")
