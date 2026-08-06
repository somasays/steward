"""Smoke test: the package imports and its metadata is well-formed."""

import steward_schemas


def test_importable() -> None:
    assert steward_schemas.__all__ == []
