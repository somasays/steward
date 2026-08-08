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

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from psycopg.rows import TupleRow
from steward_schemas import ColumnProfile, MaskedSample, TableProfile, ValueFrequency

from steward_catalog._profile_sql import STATS_PER_COLUMN, TOP_VALUE_LIMIT, stats_query, top_values_query
from steward_catalog.inspector import open_source_connection
from steward_catalog.masking import (
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
    """`SourceProfiler` over a psycopg connection on the read-only role."""

    connection: psycopg.Connection[TupleRow]

    def profile(self, target: ProfileTarget) -> TableProfile:
        names = tuple(column.name for column in target.columns)
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
        if low_cardinality(distinct):
            # Too few distinct values for any difference between the masks to be
            # anything but the domain: `yes`/`no`, `M`/`F`, `true`/`false` are
            # each recovered from a mask, a length or a preserved delimiter.
            # Suppressed here rather than in `mask()` because this is where the
            # column's cardinality is known (#49 review). The counts survive, so
            # a consumer still learns the split -- just not which way round.
            minimum, maximum = _suppress_optional(minimum), _suppress_optional(maximum)
            top_values = tuple(
                ValueFrequency(value=suppressed(frequency.value), count=frequency.count)
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
            semantic_type=column_semantic_type(frequency.value for frequency in top_values),
        )

    def _top_values(self, target: ProfileTarget, column: DiscoveredColumn) -> tuple[ValueFrequency, ...]:
        rows = self.connection.execute(
            top_values_query(target.schema_name, target.name, column.name),
            {"limit": TOP_VALUE_LIMIT},
        ).fetchall()
        return tuple(ValueFrequency(value=_masked(value), count=int(count)) for value, count in rows)


def _suppress_optional(sample: MaskedSample | None) -> MaskedSample | None:
    return None if sample is None else suppressed(sample)


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
    """
    connection = open_source_connection(secret, budget)
    try:
        yield PostgresSourceProfiler(connection)
    finally:
        connection.close()
