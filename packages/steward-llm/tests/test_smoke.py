"""Smoke test: the package imports and its metadata is well-formed."""

import steward_llm


def test_importable() -> None:
    assert steward_llm.__all__ == []
