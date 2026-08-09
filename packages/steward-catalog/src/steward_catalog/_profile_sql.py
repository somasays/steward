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

from psycopg import sql

__all__ = [
    "ROW_COUNT_ONLY",
    "TOP_VALUE_LIMIT",
    "ORDERED_COLUMNS",
    "SET_LOCAL_STATEMENT_TIMEOUT",
    "stats_query",
    "top_values_query",
]

TOP_VALUE_LIMIT = 5
"""How many of a column's most frequent values a profile carries.

Small on purpose: the sample exists to show *shape* to a classifier (#50), and
every extra value is another masked payload stored forever.
"""

# Counting is always done on the `::text` rendering. That is what makes one code
# path cover every column type a source can hold: `json` has no equality
# operator, so `count(DISTINCT json)` fails outright.
#
# **Extrema are not.** `min(({col})::text)` orders the *renderings*, so a column
# of 2, 10, 100 reports a minimum of `10` and a maximum of `2` -- not a coarser
# fact than the truth but a different one, and #50 reasons over profile evidence
# (issue #70). So the cast moves outside the aggregate: `min({col})::text` picks
# the extreme by the column's own ordering and renders the winner. Where the
# type has no `min`/`max` at all, the profile publishes nothing rather than a
# lexical value wearing a semantic label -- see `ORDERED_COLUMNS`.
_TYPED_EXTREMA = sql.SQL("count({col}), count(DISTINCT ({col})::text), min({col})::text, max({col})::text")

_NO_EXTREMA = sql.SQL("count({col}), count(DISTINCT ({col})::text), NULL::text, NULL::text")
"""The same four slots for a type that cannot be ordered, so the reader's
positional slicing does not have to know which branch produced a column."""

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
"""Aggregates each column contributes: non-null, distinct, min, max. The reader
slices the result row by this, so the two must agree -- which is why the
unordered branch emits `NULL::text` twice rather than fewer columns."""

ORDERED_COLUMNS = """
WITH col AS (
    SELECT a.attname,
           COALESCE(NULLIF(t.typbasetype, 0), a.atttypid) AS effective
    FROM pg_catalog.pg_attribute AS a
    JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
    WHERE n.nspname = %(schema_name)s
      AND c.relname = %(name)s
      AND a.attnum > 0
      AND NOT a.attisdropped
)
SELECT col.attname
FROM col
JOIN pg_catalog.pg_type AS et ON et.oid = col.effective
WHERE (
    SELECT count(DISTINCT p.proname)
    FROM pg_catalog.pg_proc AS p
    WHERE p.proname IN ('min', 'max')
      AND p.prokind = 'a'
      AND p.pronargs = 1
      AND pg_catalog.pg_function_is_visible(p.oid)
      AND (
          p.proargtypes[0] = col.effective
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.pg_cast AS implicit
              WHERE implicit.castsource = col.effective
                AND implicit.casttarget = p.proargtypes[0]
                AND implicit.castcontext IN ('i', 'b')
          )
          OR (
              p.proargtypes[0] = 'anyarray'::regtype
              AND et.typcategory = 'A'
              AND EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_type AS elem
                  JOIN pg_catalog.pg_opclass AS oc
                    ON oc.opcintype = CASE
                           WHEN elem.typtype = 'e' THEN 'anyenum'::regtype
                           ELSE COALESCE(NULLIF(elem.typbasetype, 0), elem.oid)
                       END
                  JOIN pg_catalog.pg_am AS am ON am.oid = oc.opcmethod
                  WHERE elem.oid = et.typelem
                    AND am.amname = 'btree'
                    AND oc.opcdefault
              )
          )
          OR (p.proargtypes[0] = 'anyenum'::regtype AND et.typcategory = 'E')
      )
) = 2
"""
"""Which of a relation's columns can have `min`/`max` run over them.

**Asked of the server, not guessed from a type name**, and that distinction is
the whole point: an allowlist of "numeric and temporal types" drifts the moment
a source uses a domain, an enum, an array or an extension type, and the failure
mode of guessing *wrong* is a query that errors and fails the whole profile --
every column of it, since the extrema ride in one `stats_query`.

It is also not the question it first looks like. The obvious oracle -- does the
type have a default btree operator class -- is **wrong in six ways**, measured:
`uuid`, `bytea`, `jsonb` and `boolean` are all orderable and have no `min`
aggregate, while `varchar` and arrays have `min` and no matching opclass entry.
Ordering and aggregation are different facts about a type.

So it asks `pg_proc` the way Postgres resolves the call itself: an exact
argument-type match, an implicit or binary-coercible cast to one (`varchar` ->
`text`), or one of the two polymorphic signatures. Domains resolve through
`typbasetype`.

Three things that "does an aggregate exist" alone gets wrong, each of which
fails in the direction that errors the profile:

* **An array resolves and still cannot run.** `min(anyarray)` matches *every*
  array type, and executes only where the element type has a comparison
  function -- `min(json[])` over two distinct values is
  `could not identify a comparison function for type json`. So the opclass
  question, disproved above as the *sole* oracle, is the missing second half
  here: the `anyarray` branch requires a default btree opclass on the element
  type (resolved through the element's own base type for a domain, and through
  `anyenum` for an enum element, which is where `pg_opclass` files `enum_ops`).
  This is `uuid[]`, `boolean[]` and `jsonb[]` orderable while `json[]`,
  `point[]` and `box[]` are not -- the opposite of how their element types
  answer the aggregate question.
* **`pg_proc` is the cluster, not the session.** A `min` aggregate in a schema
  outside the connection's `search_path` satisfies the prediction and then
  `function min(...) does not exist` at run time, so `pg_function_is_visible`
  is required -- sound because `_ordered_columns` runs on the same connection
  as the statistics.
* **Both aggregates are needed, not one.** `_TYPED_EXTREMA` runs `min` *and*
  `max`, so the count over distinct `proname` must reach 2. `pronargs = 1`
  keeps a two-argument function of the same name out of it.

What it still under-predicts: an array whose element is a **composite** type.
Postgres compares those through `record_ops`, which `pg_opclass` files under
the `record` pseudo-type rather than under the composite, so the conjunct above
does not find it and the column publishes no extrema. That is the direction
this design chooses to be wrong in -- one fact lost, never a failed profile --
and `tests/test_profiler.py` asserts it by name rather than letting it hide
inside an inequality. That test runs `min` *and* `max` over a probe holding two
distinct non-null values in every column, so a type that resolves and fails to
execute is caught; an earlier version left the probe empty and could only ever
re-ask the prediction's own question.
"""


SET_LOCAL_STATEMENT_TIMEOUT = "SELECT set_config('statement_timeout', %(milliseconds)s, true)"
"""Charge the next statement only what is left of the budget.

A profile is one statement per column plus two -- the `ORDERED_COLUMNS` lookup
and the statistics pass -- all inside a single `REPEATABLE READ` transaction, so
a per-statement timeout of the whole budget bounds nothing that matters: the
transaction -- and the `ACCESS SHARE` lock and `xmin` pin it holds on a
*customer's* relation -- would live for N+2 times the cap (#49 review, #70).
Charging each statement the remaining time makes the transaction's total the
budget, and a profile that has already overrun fails on its next statement
rather than starting another.

`set_config(..., is_local => true)` rather than `SET LOCAL`: it is the same
thing, reverts when the transaction ends, and takes the value as a **bound
parameter** -- so this stays a static statement with a placeholder like every
other in this package, instead of composing a literal into SQL (I5). The queue
already writes its own timeout this way (`steward_queue._sql`); the floor and
the conversion are its `statement_timeout_ms`, not a second copy here.
"""


def _relation(schema_name: str, name: str) -> sql.Identifier:
    return sql.Identifier(schema_name, name)


def stats_query(
    schema_name: str, name: str, columns: tuple[str, ...], ordered: frozenset[str]
) -> sql.Composed:
    """Row count plus four aggregates per column, in one pass over the table.

    `ordered` names the columns whose type has a `min`/`max` aggregate
    (`ORDERED_COLUMNS`); the rest report no extrema rather than lexical ones.

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
    stats = sql.SQL(", ").join(
        (_TYPED_EXTREMA if column in ordered else _NO_EXTREMA).format(col=sql.Identifier(column))
        for column in columns
    )
    return _STATS.format(stats=stats, relation=relation)


def top_values_query(schema_name: str, name: str, column: str) -> sql.Composed:
    """One column's most frequent values and their counts, deterministically."""
    return _TOP_VALUES.format(col=sql.Identifier(column), relation=_relation(schema_name, name))
