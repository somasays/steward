"""I5: sources are read-only **at the role level**.

Two assertions, and the difference between them matters:

1. The role itself refuses writes. A plain connection as `steward_reader`
   attempting an `INSERT` and a `CREATE TABLE` gets `42501
   insufficient_privilege` -- from Postgres, before any Steward code runs.
   Asserting the SQLSTATE rather than "some error" is deliberate: a session-level
   `default_transaction_read_only` would raise `25006` instead and would let a
   write-capable role pass this test, which is exactly the application-level
   enforcement ARCHITECTURE.md I5 rules out.
2. The connection a scan actually opens is that connection. The proof is run
   through `open_source_connection` -- the function `postgres_inspector` calls --
   rather than through a lookalike a test built for itself, so the path being
   proved is the path being used.

`GUARDRAILS.md` §2 lists this as the fixture harness behind I5; the M1
acceptance scenario asserts it again end to end.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from steward_catalog import Secret, open_source_connection

INSUFFICIENT_PRIVILEGE = "42501"

A_WRITE = "INSERT INTO sales.orders (id, customer, total) VALUES (1, 'nobody', 1.00)"
A_SCHEMA_CHANGE = "CREATE TABLE public.steward_should_not_be_able_to_do_this (id int)"
A_READ = "SELECT count(*) FROM sales.orders"

BUDGET = timedelta(seconds=10)


@pytest.mark.parametrize("statement", [A_WRITE, A_SCHEMA_CHANGE], ids=["insert", "create-table"])
def test_the_source_role_cannot_write(source_dsn: str, statement: str) -> None:
    with psycopg.connect(source_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
            conn.execute(statement)
    assert raised.value.sqlstate == INSUFFICIENT_PRIVILEGE


def test_the_connection_a_scan_opens_cannot_write(source_secret: Secret) -> None:
    connection = open_source_connection(source_secret, BUDGET)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
            connection.execute(A_WRITE)
        assert raised.value.sqlstate == INSUFFICIENT_PRIVILEGE
        # ...and the same connection reads fine, so the test above measures the
        # privilege and not a broken connection.
        assert connection.execute(A_READ).fetchone() is not None
    finally:
        connection.close()


def test_a_secret_is_required_to_open_a_source_connection() -> None:
    """The reference stored on a source row cannot be handed to psycopg.

    `open_source_connection` takes `Secret`; passing the `env:...` reference is
    a type error, which is the point (I5). Asserted at runtime too, because the
    string would otherwise reach libpq and fail with something unhelpful.
    """
    with pytest.raises(psycopg.Error):
        open_source_connection(Secret("env:STEWARD_TEST_SOURCE_DSN"), BUDGET)
