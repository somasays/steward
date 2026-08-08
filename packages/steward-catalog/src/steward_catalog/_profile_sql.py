"""The statements profiling runs against a *customer's* database.

Separate from `_sql` (Steward's own database) because these are the only
statements in the system that have to name a relation and a column, and that
distinction is worth a module boundary rather than a comment.

**Why this is not string-assembled SQL (I5).** A profile of `sales.orders.email`
cannot be written with `%(name)s` placeholders: a parameter is a *value*, and no
database binds an identifier as one. The alternative to composition is
concatenation, which is what I5 forbids and what S3 (ruff S608) catches. So the
templates below are static `psycopg.sql.SQL` constants and the only thing
substituted into them is a `psycopg.sql.Identifier`, which psycopg renders with
the server's own quoting rules -- `"weird""; DROP TABLE x; --"` comes out as one
quoted identifier, not as a statement. This is SPEC.md §13 D5's "parameterized
templates" for the Profiler, expressed in the mechanism psycopg provides for it.

Two further constraints keep that safe in practice, and both are properties of
the caller rather than of this module:

* **Identifiers come from the catalog, never from a request.** The handler
  builds its `ProfileTarget` from `assets`/`columns` rows, which a scan read out
  of `pg_catalog` (issue #20). A client cannot put a name here; it can only name
  an asset id.
* **The connection is the read-only role's** (`open_source_connection`), so even
  a statement that got past the above cannot write. The role is I5's actual
  guarantee; this module is the layer that must not undermine it.

Determinism is the other design rule here (I8). Every query has a total order or
no order at all: `LIMIT` never appears without an `ORDER BY` that breaks ties on
the value itself, so two profiles of unchanged data are equal rather than
merely equivalent.
"""

from __future__ import annotations

from datetime import timedelta

from psycopg import sql

__all__ = [
    "ROW_COUNT_ONLY",
    "TOP_VALUE_LIMIT",
    "remaining_statement_timeout",
    "stats_query",
    "top_values_query",
]

TOP_VALUE_LIMIT = 5
"""How many of a column's most frequent values a profile carries.

Small on purpose: the sample exists to show *shape* to a classifier (#50), and
every extra value is another masked payload stored forever.
"""

# Every value is profiled through its `::text` rendering. That is what makes one
# code path cover every column type a source can hold: `json` has no equality
# operator (so `count(DISTINCT json)` fails), several types have no `min`/`max`
# aggregate at all, and the masker would otherwise need a branch per driver type
# instead of one function over text.
_COLUMN_STATS = sql.SQL("count({col}), count(DISTINCT ({col})::text), min(({col})::text), max(({col})::text)")

_STATS = sql.SQL("SELECT count(*), {stats} FROM {relation}")

ROW_COUNT_ONLY = sql.SQL("SELECT count(*) FROM {relation}")
"""The degenerate statement for a relation with no active columns. Postgres
permits a table with zero columns, and `SELECT count(*), FROM t` is a syntax
error -- so the shape without a stats list is its own template rather than a
string built by leaving a comma off."""

_TOP_VALUES = sql.SQL(
    "SELECT ({col})::text, count(*) FROM {relation} "
    "WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT %(limit)s"
)
"""Ties are broken by the value itself, ascending, so "the five most common
values" is one answer rather than whichever five the plan happened to emit."""

STATS_PER_COLUMN = 4
"""Aggregates `_COLUMN_STATS` contributes per column: non-null, distinct, min,
max. The reader slices the result row by this, so the two must agree."""


MIN_STATEMENT_TIMEOUT_MS = 1
"""libpq reads 0 as "no timeout", which is the opposite of what an exhausted
budget means -- the same floor `inspector.MIN_CONNECT_TIMEOUT_SECONDS` exists
for."""


def remaining_statement_timeout(budget: timedelta, elapsed: float) -> sql.Composed:
    """`SET LOCAL statement_timeout` to whatever is left of `budget`.

    A profile is one statement per column plus one, all inside a single
    `REPEATABLE READ` transaction, so a per-statement timeout of the whole
    budget bounds nothing that matters: the transaction -- and the `ACCESS
    SHARE` lock and `xmin` pin it holds on a *customer's* relation -- would live
    for N+1 times the cap (#49 review). Charging each statement the remaining
    time makes the transaction's total the budget, and a profile that has
    already overrun fails on its next statement rather than starting another.

    `SET LOCAL` rather than `SET`: it reverts when the transaction ends, so the
    connection is not left carrying a timeout from a profile that finished.
    `sql.Literal` because `SET` takes no bind parameters -- it is composition,
    not interpolation, and the value is an integer this module computed (I5).
    """
    remaining = int(budget.total_seconds() * 1000) - int(elapsed * 1000)
    return sql.SQL("SET LOCAL statement_timeout = {}").format(
        sql.Literal(max(MIN_STATEMENT_TIMEOUT_MS, remaining))
    )


def _relation(schema_name: str, name: str) -> sql.Identifier:
    return sql.Identifier(schema_name, name)


def stats_query(schema_name: str, name: str, columns: tuple[str, ...]) -> sql.Composed:
    """Row count plus four aggregates per column, in one pass over the table.

    One statement rather than one per column, so the *aggregates* cost one pass
    over the relation however wide it is. That argument covers this query only:
    `top_values_query` below still runs one grouped scan per column, so a full
    profile is 1 + N passes and the honest bound on it is the task's wall-clock
    budget, not this shape. Folding the frequencies into the same pass wants a
    lateral aggregate and belongs with whatever first profiles a table wide
    enough to notice (N5).
    """
    relation = _relation(schema_name, name)
    if not columns:
        return ROW_COUNT_ONLY.format(relation=relation)
    stats = sql.SQL(", ").join(_COLUMN_STATS.format(col=sql.Identifier(column)) for column in columns)
    return _STATS.format(stats=stats, relation=relation)


def top_values_query(schema_name: str, name: str, column: str) -> sql.Composed:
    """One column's most frequent values and their counts, deterministically."""
    return _TOP_VALUES.format(col=sql.Identifier(column), relation=_relation(schema_name, name))
