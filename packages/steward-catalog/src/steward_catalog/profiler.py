"""The profiling read side: statistics out of a customer's database (FR2, #49).

The same shape as `inspector`, one layer down. `SourceInspector` reads a
source's *metadata* through a read-only role; `SourceProfiler` reads its *data*
through the same connection function, and that is the difference this slice is
about -- it is the first code in Steward that looks at customer values.

Which is why the return type is the whole design. `profile()` returns a
`TableProfile`, whose value-carrying fields are `MaskedSample`s. There is no
function in this module that returns a value it read; the raw cells exist as
locals, typed `RawCell` (`masking`), for exactly as long as it takes to mask
them. A caller cannot obtain one, because nothing hands one back (I6).

Determinism (I8) is the other constraint, and it shapes the SQL rather than the
Python: statistics are aggregates, the sample is "the k most frequent values,
ties broken by the value", and nothing reads a clock or a random seed. Profiling
unchanged data twice produces an equal `TableProfile`, which is what lets
`profiles` stay append-only without growing a version per run.

Timeouts and the read-only role are the connection's, inherited from
`open_source_connection` -- so the I5 proof in `tests/test_read_only.py`, which
attempts a write on the connection a scan opens, covers the profiler's
connection too: it is the same function.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import TupleRow
from steward_queue import statement_timeout_ms
from steward_schemas import (
    ColumnProfile,
    MaskedSample,
    SemanticType,
    TableProfile,
    ValueFrequency,
)

from steward_catalog._profile_sql import (
    SET_LOCAL_STATEMENT_TIMEOUT,
    STATS_PER_COLUMN,
    TOP_VALUE_LIMIT,
    stats_query,
    top_values_query,
)
from steward_catalog.inspector import open_source_connection
from steward_catalog.masking import (
    LOW_CARDINALITY_MAX,
    RawCell,
    column_semantic_type,
    low_cardinality,
    mask,
    mask_optional,
    suppressed,
)
from steward_catalog.models import DiscoveredColumn, ProfileTarget
from steward_catalog.secrets import Secret

__all__ = ["RATIO_PLACES", "SourceProfiler", "SourceProfilerFactory", "postgres_profiler"]

RATIO_PLACES = Decimal("0.000001")
"""Ratios are quantized `Decimal`s, not floats.

Two profiles of unchanged data must be byte-identical after a JSON round trip
(I8), and binary floating point does not promise that a division repeats to the
last digit across platforms. Six places is finer than any consumer of a null
ratio needs and coarse enough that a row-count change of one in a million table
still moves the number.
"""

ZERO_RATIO = Decimal(0)


class SourceProfiler(Protocol):
    """Read-only statistical access to one relation of a registered source."""

    def profile(self, target: ProfileTarget) -> TableProfile:
        """`target`'s row count and one `ColumnProfile` per column it names,
        in the order it names them.

        Every value the profile carries has been through `masking.mask` -- the
        contract of this method is that a raw value cannot come out of it (I6).
        """
        ...


type SourceProfilerFactory = Callable[[Secret, timedelta], AbstractContextManager[SourceProfiler]]
"""Credential + wall-clock budget in, a scoped profiler out. Injected into the
handler exactly as `SourceInspectorFactory` is, so a test profiles a fixture
database without a secret store and a deployment swaps the connector without
touching the handler (N9)."""


def _ratio(part: int, whole: int) -> Decimal:
    """`part/whole` as a stable Decimal; zero when there are no rows at all.

    An empty table has no nulls *and* no non-nulls, so every ratio on it is
    zero by definition rather than undefined -- the alternative, `None`, would
    put an "it depends" in every consumer of a profile for a case that means
    "nothing to report".
    """
    if whole <= 0:
        return ZERO_RATIO
    return (Decimal(part) / Decimal(whole)).quantize(RATIO_PLACES)


def _cell(value: object) -> RawCell | None:
    """The one place a driver value becomes a `RawCell`.

    Everything is selected as `::text`, so the rendering is Postgres' own and
    this is a wrap rather than a conversion. `None` stays `None`: a null is
    counted, never sampled.
    """
    return None if value is None else RawCell(str(value))


@dataclass(frozen=True, slots=True)
class PostgresSourceProfiler:
    """`SourceProfiler` over a psycopg connection on the read-only role.

    Carries the wall-clock `budget` because the transaction it opens has to be
    bounded by something, and `statement_timeout` alone is not it: it bounds a
    *statement* (`steward_queue.db`), and a profile is one stats pass plus one
    query per column. Under autocommit that did not matter -- locks and snapshot
    were released between statements -- but one `REPEATABLE READ` transaction
    holds an `ACCESS SHARE` lock and pins `xmin` for as long as it lives, so N+1
    statements each allowed the whole budget would let a 60-column table hold a
    customer's relation for 61 times the cap, blocking their DDL and their
    VACUUM long after Steward recorded the task `budget_exceeded` (#49 review).

    So each statement is allowed only what is *left* of the budget: the last one
    gets the remainder, the total is the cap, and a profile that has already
    overrun fails on its next statement instead of running one more.
    """

    connection: psycopg.Connection[TupleRow]
    budget: timedelta
    started: float = field(default_factory=time.monotonic)
    """When the budget started running -- set by `postgres_profiler` *before* it
    connects, not at construction. `connect_timeout` is itself derived from the
    budget, so a slow connect could otherwise hand the transaction a fresh full
    budget on a task the worker had already recorded `budget_exceeded`."""

    def remaining(self) -> timedelta:
        """What is left of the budget. Never negative: an exhausted profile gets
        the floor `statement_timeout_ms` applies, so its next statement fails
        immediately rather than running unbounded."""
        return max(timedelta(0), self.budget - timedelta(seconds=time.monotonic() - self.started))

    def _bound_next_statement(self) -> None:
        """Allow the next statement only the budget that remains."""
        self.connection.execute(
            SET_LOCAL_STATEMENT_TIMEOUT, {"milliseconds": str(statement_timeout_ms(self.remaining()))}
        )

    def profile(self, target: ProfileTarget) -> TableProfile:
        try:
            profile = self._profile(target)
        except BaseException:
            # Guarded: on a connection the server has closed, `rollback()` raises
            # in its own right and would replace the original exception -- which
            # is the one the handler logs the type and SQLSTATE of, and the only
            # failure signal that path carries (N7 forbids the message itself).
            with suppress(psycopg.Error):
                self.connection.rollback()
            raise
        # End the snapshot with the profile that needed it. Holding one for the
        # profiler's whole lifetime would keep a transaction open on the
        # customer's database across everything the caller does next, and block
        # their DDL behind our read locks.
        self.connection.rollback()
        return profile

    def _profile(self, target: ProfileTarget) -> TableProfile:
        names = tuple(column.name for column in target.columns)
        self._bound_next_statement()
        row = self.connection.execute(stats_query(target.schema_name, target.name, names)).fetchone()
        if row is None:  # pragma: no cover -- an aggregate always returns a row
            raise RuntimeError(f"no statistics returned for {target.schema_name}.{target.name}")
        expected = 1 + STATS_PER_COLUMN * len(names)
        if len(row) != expected:
            # The row is sliced positionally below, so a mismatch between the
            # aggregates `_COLUMN_STATS` emits and `STATS_PER_COLUMN` would not
            # fail -- it would shift every column's min/max onto its neighbour
            # and produce a profile that is wrong rather than absent. The
            # comment on the constant says the two must agree; this is what
            # makes disagreeing loud.
            raise RuntimeError(
                f"statistics row has {len(row)} columns, expected {expected} "
                f"({len(names)} columns x {STATS_PER_COLUMN} aggregates + row count)"
            )
        row_count = int(row[0])
        columns = tuple(
            self._column_profile(target, column, row_count, row[1 + index * STATS_PER_COLUMN :])
            for index, column in enumerate(target.columns)
        )
        return TableProfile(row_count=row_count, columns=columns)

    def _column_profile(
        self, target: ProfileTarget, column: DiscoveredColumn, row_count: int, stats: Sequence[Any]
    ) -> ColumnProfile:
        non_null, distinct = int(stats[0]), int(stats[1])
        top_values = self._top_values(target, column)
        minimum, maximum = mask_optional(_cell(stats[2])), mask_optional(_cell(stats[3]))
        # Computed before suppression: afterwards every sample carries the
        # column's own type, so deriving it from the suppressed tuple would be
        # circular -- and reading it from a blanked tuple would report UNKNOWN.
        semantic = column_semantic_type(frequency.value for frequency in top_values)
        if low_cardinality(distinct) or len(top_values) <= LOW_CARDINALITY_MAX:
            # Too few distinct values for any difference between the masks to be
            # anything but the domain: `yes`/`no`, `M`/`F`, `true`/`false` are
            # each recovered from a mask, a length or a preserved delimiter.
            # Suppressed here rather than in `mask()` because this is where the
            # column's cardinality is known (#49 review). The counts survive, so
            # a consumer still learns the split -- just not which way round.
            minimum = _suppress_optional(minimum, semantic)
            maximum = _suppress_optional(maximum, semantic)
            top_values = tuple(
                ValueFrequency(value=suppressed(frequency.value, semantic), count=frequency.count)
                for frequency in top_values
            )
        return ColumnProfile(
            name=column.name,
            data_type=column.data_type,
            null_count=row_count - non_null,
            null_ratio=_ratio(row_count - non_null, row_count),
            distinct_count=distinct,
            distinct_ratio=_ratio(distinct, row_count),
            min_value=minimum,
            max_value=maximum,
            top_values=top_values,
            semantic_type=semantic,
        )

    def _top_values(self, target: ProfileTarget, column: DiscoveredColumn) -> tuple[ValueFrequency, ...]:
        self._bound_next_statement()
        rows = self.connection.execute(
            top_values_query(target.schema_name, target.name, column.name),
            {"limit": TOP_VALUE_LIMIT},
        ).fetchall()
        return tuple(ValueFrequency(value=_masked(value), count=int(count)) for value, count in rows)


def _suppress_optional(sample: MaskedSample | None, column_type: SemanticType) -> MaskedSample | None:
    return None if sample is None else suppressed(sample, column_type)


def _masked(value: object) -> MaskedSample:
    """A sampled value, masked. `WHERE ... IS NOT NULL` means there is one."""
    cell = _cell(value)
    if cell is None:  # pragma: no cover -- excluded by the query itself
        raise RuntimeError("a null reached the sampler")
    return mask(cell)


@contextmanager
def postgres_profiler(secret: Secret, budget: timedelta) -> Iterator[SourceProfiler]:
    """A `SourceProfiler` on a Postgres source, closed when the block ends.

    A context manager for the reason `postgres_inspector` is one: the
    connection is a resource on someone else's server, and a handler that
    raised halfway would otherwise leave it open until their socket timed out.

    **`REPEATABLE READ`, and autocommit off, which the metadata read does not
    need.** A profile is many statements -- one stats pass plus one grouped
    query per column -- and under autocommit each gets its own snapshot. The
    suppression decision is then read from *different data* than the sample it
    suppresses: a three-valued column whose third value drains between the two
    statements reads `distinct = 3`, skips suppression, and publishes a
    now-binary sample with its values distinguishable (#49 review). One
    snapshot makes the statistics and the samples describe the same table, which
    is also what "re-profiling converges" (I8) presumes.

    The snapshot is scoped to one `profile()` call, not to the profiler's
    lifetime: the cost is an open transaction on the customer's database for the
    length of a profile, and holding one any longer would keep our read locks
    across whatever the caller does next -- enough to block their DDL. It is
    bounded by the same budget-derived `statement_timeout` and by the task's
    deadline.
    """
    started = time.monotonic()
    connection = open_source_connection(secret, budget)
    try:
        connection.autocommit = False
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        yield PostgresSourceProfiler(connection, budget, started)
    finally:
        connection.close()
