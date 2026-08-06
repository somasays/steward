from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from steward_api.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A fresh app (and fresh in-memory `RunStore`) per test, so idempotency
    keys and run ids from one test never leak into another."""
    with TestClient(create_app()) as test_client:
        yield test_client
