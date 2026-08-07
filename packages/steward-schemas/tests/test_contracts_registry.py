"""CONTRACTS registry: every published contract generates a JSON Schema that
round-trips through JSON (issue #2 acceptance criteria; feeds the S6
contract-compatibility check, GUARDRAILS.md).

Uses pytest.mark.parametrize: S1 (GUARDRAILS.md) scopes the schemas
independence contract to the installed package (`src/`), not `tests/`
(issue #12), so tests are free to import pytest (issue #13).
"""

import json

import pytest
from steward_schemas import CONTRACTS


def test_contracts_registry_is_nonempty() -> None:
    assert CONTRACTS
    assert len(CONTRACTS) == 13


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_contract_json_schema_roundtrips(name: str) -> None:
    model_cls = CONTRACTS[name]
    schema = model_cls.model_json_schema()
    restored = json.loads(json.dumps(schema))
    assert restored == schema


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_contract_schema_title_matches_class(name: str) -> None:
    model_cls = CONTRACTS[name]
    schema = model_cls.model_json_schema()
    assert schema["title"] == model_cls.__name__
