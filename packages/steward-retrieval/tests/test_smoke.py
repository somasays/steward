"""Smoke test: the package imports and its metadata is well-formed."""

import steward_retrieval


def test_importable() -> None:
    assert steward_retrieval.__all__ == []
