"""Smoke test: the package imports and its metadata is well-formed."""

import steward_agents


def test_importable() -> None:
    assert steward_agents.__all__ == []
