"""The read side: a connection to the *customer's* database, and nothing else.

This is one of the two connections a scan uses, and they are deliberately
different types. Steward writes through a `steward_queue.QueueConnection` (its
own system of record, I1); it reads a source through a `SourceInspector`, whose
only operations are `inspect()`. There is no method here that writes, so
"Steward never mutates customer data" is a property of the type a scan is
handed, not a rule a reviewer has to keep checking. Passing one where the other
belongs does not compile.

That is the *second* line of defence. The first is the one ARCHITECTURE.md I5
actually names: **the DSN belongs to a read-only role**, and a write fails in
Postgres with `42501 insufficient_privilege` before any Steward code runs.
This module deliberately does *not* also set `default_transaction_read_only`:
that flag would make a write-capable role's connection fail with `25006` and so
would mask a misconfigured role behind a session setting -- exactly the
application-level enforcement I5 rules out. The role is the guarantee; the test
that proves it (`tests/test_read_only.py`) asserts the privilege error, which
only a genuinely read-only role can produce.

Timeouts are the connection's own, for the same reason `steward_queue.db.connect`
sets one: the worker's `asyncio.timeout` can only cancel at an await point, and
a psycopg call is a blocking C call in a worker thread. A source that accepts a
connection and then never answers would otherwise burn the whole task budget
with nothing to interrupt. Both bounds derive from the task's wall-clock budget,
so a scan cannot outlive the cap the run was admitted under (I12).

SQL is static module constants (I5); `%(name)s` placeholders are bound by
psycopg, and nothing here is assembled from strings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import psycopg
from psycopg.rows import TupleRow
from steward_queue import statement_timeout_ms
from steward_schemas import AssetType

from steward_catalog.models import DiscoveredAsset, DiscoveredColumn, SchemaFilter
from steward_catalog.secrets import Secret

__all__ = [
    "SourceInspector",
    "SourceInspectorFactory",
    "open_source_connection",
    "postgres_inspector",
]

MIN_CONNECT_TIMEOUT_SECONDS = 1
"""libpq floors `connect_timeout` at 2s and reads 0 as "wait forever", which is
the opposite of what an exhausted budget means -- so the derived value never
goes below 1."""

# The relkind filter admits ordinary and partitioned tables and plain and
# materialized views. Everything else `pg_class` holds -- indexes, sequences,
# TOAST tables, composite types -- is storage machinery, not part of the estate.
LIST_ASSETS = """
SELECT n.nspname, c.relname, CASE WHEN c.relkind IN ('r', 'p') THEN 'table' ELSE 'view' END
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname <> ALL (%(exclude)s::text[])
  AND (cardinality(%(include)s::text[]) = 0 OR n.nspname = ANY (%(include)s::text[]))
ORDER BY n.nspname, c.relname
"""

LIST_COLUMNS = """
SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnum,
       NOT a.attnotnull
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname <> ALL (%(exclude)s::text[])
  AND (cardinality(%(include)s::text[]) = 0 OR n.nspname = ANY (%(include)s::text[]))
ORDER BY n.nspname, c.relname, a.attnum
"""


class SourceInspector(Protocol):
    """Read-only metadata access to one registered source."""

    def inspect(self, schemas: SchemaFilter) -> tuple[DiscoveredAsset, ...]:
        """Every asset the filter admits, ordered by (schema, name), with its
        columns in the source's own ordinal order.

        Ordering is part of the contract, not an accident of the query plan:
        the scan compares this observation with what is stored, and an
        unordered observation would make "nothing changed" depend on how
        Postgres felt about the scan that day.
        """
        ...


type SourceInspectorFactory = Callable[[Secret, timedelta], AbstractContextManager[SourceInspector]]
"""How the scan handler gets an inspector: credential + wall-clock budget in,
a scoped inspector out. `postgres_inspector` is the production implementation;
a test substitutes another without a database and without a secret store."""


def connect_timeout_seconds(budget: timedelta) -> int:
    """A wall-clock budget as a libpq `connect_timeout`, in whole seconds."""
    return max(MIN_CONNECT_TIMEOUT_SECONDS, int(budget.total_seconds()))


def open_source_connection(secret: Secret, budget: timedelta) -> psycopg.Connection[TupleRow]:
    """Open the customer-side connection a scan reads through.

    `secret` is a `Secret`, not a `str`: the reference stored on the source row
    cannot be passed here without going through the resolver, because it does
    not type-check (I5, N7).

    `autocommit=True` because every statement is a read and an idle-in-transaction
    session on a customer database is rude. Read-only-ness is the role's, not
    this flag's -- see the module docstring.

    Public (rather than folded into `postgres_inspector`) so the read-only proof
    can attempt a write on *the connection a scan actually opens*, not on a
    lookalike a test built for itself.
    """
    return psycopg.connect(
        secret.reveal(),
        autocommit=True,
        connect_timeout=connect_timeout_seconds(budget),
        options=f"-c statement_timeout={statement_timeout_ms(budget)}",
    )


@dataclass(frozen=True, slots=True)
class PostgresSourceInspector:
    """`SourceInspector` over a psycopg connection, read via `pg_catalog`.

    `pg_catalog` rather than `information_schema`: the latter hides
    materialized views entirely and renumbers ordinals after a dropped column,
    which would make a rescan report changes that did not happen.
    """

    connection: psycopg.Connection[TupleRow]

    def inspect(self, schemas: SchemaFilter) -> tuple[DiscoveredAsset, ...]:
        params = {"include": list(schemas.include), "exclude": list(schemas.exclude)}
        assets = self.connection.execute(LIST_ASSETS, params).fetchall()
        columns = self.connection.execute(LIST_COLUMNS, params).fetchall()
        by_asset: dict[tuple[str, str], list[DiscoveredColumn]] = {}
        for schema_name, relation, name, data_type, ordinal, nullable in columns:
            by_asset.setdefault((schema_name, relation), []).append(
                DiscoveredColumn(name=name, data_type=data_type, ordinal=ordinal, nullable=nullable)
            )
        return tuple(
            DiscoveredAsset(
                schema_name=schema_name,
                name=name,
                asset_type=AssetType(asset_type),
                columns=tuple(by_asset.get((schema_name, name), ())),
            )
            for schema_name, name, asset_type in assets
        )


@contextmanager
def postgres_inspector(secret: Secret, budget: timedelta) -> Iterator[SourceInspector]:
    """A `SourceInspector` on a Postgres source, closed when the block ends.

    A context manager rather than a returned object because the connection is a
    customer-side resource: a handler that raised halfway would otherwise leave
    it open on their server until the socket timed out.
    """
    connection = open_source_connection(secret, budget)
    try:
        yield PostgresSourceInspector(connection)
    finally:
        connection.close()
