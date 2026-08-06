"""Smoke test: the package imports and its metadata is well-formed."""

import steward_sdk


def test_importable() -> None:
    assert steward_sdk.__all__ == []
