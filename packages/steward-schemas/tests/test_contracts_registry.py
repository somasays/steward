"""CONTRACTS registry: every published contract generates a JSON Schema that
round-trips through JSON (issue #2 acceptance criteria; feeds the future S6
contract-compatibility check, GUARDRAILS.md).

Plain `test_*` functions, no third-party test framework import — see the
note in test_roundtrip.py (I4, enforced by S1 across this whole package).
"""

import json

from steward_schemas import CONTRACTS


def test_contracts_registry_is_nonempty() -> None:
    assert CONTRACTS
    assert len(CONTRACTS) == 8


def test_every_contract_json_schema_roundtrips() -> None:
    for name in sorted(CONTRACTS):
        model_cls = CONTRACTS[name]
        schema = model_cls.model_json_schema()
        restored = json.loads(json.dumps(schema))
        assert restored == schema, name


def test_every_contract_schema_title_matches_class() -> None:
    for name in sorted(CONTRACTS):
        model_cls = CONTRACTS[name]
        schema = model_cls.model_json_schema()
        assert schema["title"] == model_cls.__name__, name
