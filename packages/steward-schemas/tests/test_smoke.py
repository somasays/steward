"""Smoke test: the package imports and its public surface is well-formed."""

import steward_schemas


def test_importable() -> None:
    assert "RunBudget" in steward_schemas.__all__


def test_all_exports_resolve() -> None:
    for name in steward_schemas.__all__:
        assert hasattr(steward_schemas, name)


def test_contracts_registry_exported() -> None:
    assert set(steward_schemas.CONTRACTS) == {
        "source",
        "asset",
        "column",
        "task_spec",
        "task_result",
        "run_budget",
        "agent_spec",
        "problem_details",
        "run_create",
        "run_response",
    }
