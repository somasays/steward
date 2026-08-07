"""Smoke test: the package imports and its public surface is well-formed."""

import pytest
import steward_schemas


def test_importable() -> None:
    assert "RunBudget" in steward_schemas.__all__


@pytest.mark.parametrize("name", steward_schemas.__all__)
def test_export_resolves(name: str) -> None:
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
        "run",
    }
