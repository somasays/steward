"""The profiling read side against a real source database (#49).

No Steward database here: this is about what comes back out of a customer's
database and in what shape. Persistence and convergence are
`test_profile_convergence.py`; the end-to-end privacy claim is H7.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from steward_catalog import DiscoveredColumn, ProfileTarget, Secret, postgres_profiler
from steward_catalog.profiler import PostgresSourceProfiler, SourceProfiler
from steward_schemas import SemanticType, TableProfile

BUDGET = timedelta(seconds=30)

# A column name that is a SQL injection attempt. It reaches the query as a
# `psycopg.sql.Identifier`, so the only way this test passes is if psycopg
# quoted it -- if the name were concatenated, the DROP would run and the
# statement after it would fail (I5).
HOSTILE_COLUMN = 'evil"; DROP TABLE sales.customers; --'


def column(name: str, data_type: str = "text", ordinal: int = 1) -> DiscoveredColumn:
    return DiscoveredColumn(name=name, data_type=data_type, ordinal=ordinal, nullable=True)


@pytest.fixture
def profiler(source_secret: Secret) -> Iterator[SourceProfiler]:
    with postgres_profiler(source_secret, BUDGET) as reader:
        yield reader


def test_a_profile_counts_rows_nulls_and_distinct_values(profiler: SourceProfiler) -> None:
    profile = profiler.profile(
        ProfileTarget(
            schema_name="sales",
            name="orders",
            columns=(column("id", "bigint", 1), column("customer", "text", 2)),
        )
    )

    assert profile.row_count == 4
    by_name = {column_profile.name: column_profile for column_profile in profile.columns}
    assert by_name["id"].null_count == 0
    assert by_name["id"].distinct_count == 4
    assert by_name["customer"].null_count == 1
    assert by_name["customer"].null_ratio == Decimal("0.250000")
    assert by_name["customer"].distinct_count == 3


def test_every_value_a_profile_carries_is_masked(profiler: SourceProfiler, canary_email: str) -> None:
    """I6 at the seam that reads data: min, max and every sampled value."""
    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name="customers", columns=(column("email"),))
    )

    [email] = profile.columns
    assert email.semantic_type is SemanticType.EMAIL
    assert email.min_value is not None and email.max_value is not None
    rendered = [email.min_value.masked, email.max_value.masked]
    rendered += [frequency.value.masked for frequency in email.top_values]
    assert all(canary_email not in value for value in rendered)
    assert "c***@s***.****" in rendered  # the canary row, masked -- TLD included


def test_a_columns_semantic_type_comes_from_its_values(profiler: SourceProfiler, canary_card: str) -> None:
    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name="customers", columns=(column("card"),))
    )

    [card] = profile.columns
    assert card.semantic_type is SemanticType.CREDIT_CARD
    # No digit of the card reaches the profile -- not even the last four, which
    # this used to assert as expected behaviour. `_is_card` is a Luhn checksum
    # over the value, so it fires on IMEIs and on roughly one in ten long
    # numeric ids; a reveal riding on it published their tails too (#49 review).
    assert [frequency.value.masked for frequency in card.top_values] == ["****-****-****-****"] * 3
    assert card.null_count == 1
    assert canary_card not in card.top_values[0].value.masked
    assert canary_card[-4:] not in card.top_values[0].value.masked


def test_top_values_are_ordered_by_frequency_then_value(profiler: SourceProfiler) -> None:
    """Determinism (I8): `LIMIT` without a total order would make two profiles
    of the same table disagree about which values they sampled.

    Both values here mask to `**.**` -- they are four digits each, below the
    floor at which a mask reveals anything -- so the frequencies are what
    distinguishes them. That is the privacy trade working as designed, not a
    lost assertion: the ordering is asserted on the counts, which is what the
    query orders by.
    """
    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name="orders", columns=(column("total", "numeric"),))
    )

    [total] = profile.columns
    assert [(frequency.value.masked, frequency.count) for frequency in total.top_values] == [
        ("**.**", 2),
        ("**.**", 1),
        ("**.**", 1),
    ]
    assert [frequency.count for frequency in total.top_values] == [2, 1, 1]


# Six distinct values competing for five sample slots, with a three-way tie
# across the cut: `dates44`, `elder55` and `figgy66` all occur twice and only
# two can be kept. `ORDER BY 2 DESC, 1 ASC` decides which; without the `1 ASC`
# Postgres decides, and differently between plans. Seven characters each so
# every mask is distinct (`a*****1`), which is what lets the assertion below
# see the decision at all.
TIED_COLUMN = (
    "CREATE TABLE sales.tied (v text)",
    "INSERT INTO sales.tied (v) VALUES "
    "('apple11'),('apple11'),('apple11'),('apple11'),"
    "('berry22'),('berry22'),('berry22'),"
    "('cocoa33'),('cocoa33'),('cocoa33'),"
    "('dates44'),('dates44'),"
    "('elder55'),('elder55'),"
    "('figgy66'),('figgy66')",
    "GRANT SELECT ON sales.tied TO steward_reader",
)


# Two-valued domains that are not `boolean`. Each was fully recoverable from
# the mask, the length or a preserved delimiter before column-level suppression
# (#49 review): the motivating column is protected as `is_hiv_positive boolean`
# and was not as `hiv_status text CHECK (v IN ('yes','no'))`.
BINARY_DOMAINS: tuple[tuple[str, str, str], ...] = (
    ("consent", "yes", "no"),
    ("sex", "male", "female"),
    ("state", "active", "inactive"),
    ("blood", "O+", "A-"),
    ("flag", "true", "false"),
    # Type-heterogeneous, which the five above are not: an empty string infers
    # EMPTY and `Y` infers TEXT, so the per-entry `semantic_type` told the two
    # apart even with mask and length blanked -- the third distinguishing field,
    # after the mask and the length (#49 review).
    ("flagged", "", "Y"),
    ("mixed", "1", "no"),
)


@pytest.mark.parametrize(("name", "first", "second"), BINARY_DOMAINS)
def test_a_two_valued_column_publishes_nothing_that_tells_its_values_apart(
    profiler: SourceProfiler,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
    name: str,
    first: str,
    second: str,
) -> None:
    """I6 at the column level, which is where "closed domain" is a property.

    Masking one value at a time cannot see this: `yes` and `no` mask to `***`
    and `**`, `O+` and `A-` to `*+` and `*-` through preserved delimiters, and
    `male`/`female` differ in length. With two distinct values any difference
    between the masks *is* the domain, so an `is_hiv_positive` column published
    every sampled value and its distribution — protected only if it happened to
    be typed `boolean`.

    The counts survive: a consumer still learns the split, just not which way
    round.
    """
    relation = sql.Identifier("sales", f"binary_{name}")
    source_admin.execute(sql.SQL("CREATE TABLE {} (v text)").format(relation))
    source_admin.execute(
        sql.SQL("INSERT INTO {} (v) VALUES (%(a)s), (%(a)s), (%(a)s), (%(b)s)").format(relation),
        {"a": first, "b": second},
    )
    source_admin.execute(sql.SQL("GRANT SELECT ON {} TO steward_reader").format(relation))

    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name=f"binary_{name}", columns=(column("v"),))
    )

    [column_profile] = profile.columns
    assert column_profile.distinct_count == 2
    # Every published field of every sample, not just the mask: each of mask,
    # length and semantic_type has been a way to tell suppressed values apart.
    published = {
        (frequency.value.masked, frequency.value.semantic_type, frequency.value.length)
        for frequency in column_profile.top_values
    }
    assert len(published) == 1, f"{name}: the two values are distinguishable: {published}"
    assert [frequency.count for frequency in column_profile.top_values] == [3, 1]  # the split survives
    assert column_profile.min_value == column_profile.max_value  # ...and so do min/max
    assert column_profile.min_value is not None and column_profile.min_value.masked == "***"


DRAINING_COLUMN = (
    "CREATE TABLE sales.draining (v text)",
    "INSERT INTO sales.draining (v) VALUES ('yes'),('yes'),('yes'),('no'),('no'),('pending')",
    "GRANT SELECT ON sales.draining TO steward_reader",
)

DRAIN_THE_THIRD_VALUE = "DELETE FROM sales.draining WHERE v = 'pending'"


def test_a_profile_reads_one_snapshot_even_while_the_table_changes(
    source_secret: Secret, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """The statistics and the samples must describe the same table (I8).

    A profile is one stats pass plus one query per column. Under autocommit each
    got its own snapshot, so a three-valued column whose third value drained in
    between reported `distinct_count = 3` from the first statement and a
    two-row sample from the second -- a now-binary column published with its
    values distinguishable, because the suppression decision was made against
    data that no longer existed (#49 review).

    The drain here is committed by a *different* connection while the profile is
    open, which is the only way to exercise it: under `REPEATABLE READ` the
    profiler cannot see it, so `distinct_count` and the sample agree and nothing
    is suppressed. Remove the isolation level and this fails -- the count says
    three and the sample carries two.
    """
    for statement in DRAINING_COLUMN:
        source_admin.execute(statement)
    target = ProfileTarget(schema_name="sales", name="draining", columns=(column("v"),))

    with postgres_profiler(source_secret, BUDGET) as reader:
        profile = _profile_with_drain(reader, target, source_admin)

    [column_profile] = profile.columns
    assert column_profile.distinct_count == 3
    assert len(column_profile.top_values) == 3, "the sample saw a different table than the statistics"
    assert {frequency.value.masked for frequency in column_profile.top_values} != {"***"}


def _profile_with_drain(
    reader: SourceProfiler,
    target: ProfileTarget,
    admin: psycopg.Connection[psycopg.rows.TupleRow],
) -> TableProfile:
    """Run a profile with a committed DELETE landing between its two statements.

    The seam is the profiler's own `_top_values`: wrapping it is how the drain
    is placed *inside* one profile rather than between two, which is where the
    race lives.
    """
    original = type(reader)._top_values

    def draining(self: PostgresSourceProfiler, *args: object, **kwargs: object) -> object:
        admin.execute(DRAIN_THE_THIRD_VALUE)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    type(reader)._top_values = draining  # type: ignore[assignment,method-assign]
    try:
        return reader.profile(target)
    finally:
        type(reader)._top_values = original  # type: ignore[method-assign]


SHOW_STATEMENT_TIMEOUT = "SHOW statement_timeout"

MEASURABLE_DELAY = 0.05
"""Long enough that the budget shrinks by whole milliseconds between columns."""

TWO_COLUMN_TABLE = (
    "CREATE TABLE sales.timed (a text, b text)",
    "INSERT INTO sales.timed (a, b) VALUES ('x','p'),('y','q'),('z','r')",
    "GRANT SELECT ON sales.timed TO steward_reader",
)


def test_each_statement_is_charged_only_what_is_left_of_the_budget(
    source_secret: Secret, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """I12 on a resource held on someone else's database.

    A profile is one stats pass plus one query per column, all in one
    `REPEATABLE READ` transaction. `statement_timeout` bounds a *statement*, so
    allowing each the whole budget would let a 60-column table hold an
    `ACCESS SHARE` lock and an `xmin` pin on a customer's relation for 61 times
    the advertised cap -- long after the worker recorded the task
    `budget_exceeded` (#49 review).

    Asserted on what the server reports inside the transaction: the timeout is
    below the whole budget and never grows. Remove either `_bound_next_statement`
    call and the session keeps the connection-level value, which is the whole
    budget, and the first assertion fails.
    """
    for statement in TWO_COLUMN_TABLE:
        source_admin.execute(statement)
    target = ProfileTarget(schema_name="sales", name="timed", columns=(column("a"), column("b", ordinal=2)))
    observed: list[int] = []

    with postgres_profiler(source_secret, BUDGET) as reader:
        original = type(reader)._top_values

        def observing(self: PostgresSourceProfiler, *args: object, **kwargs: object) -> object:
            # Sleep first, so the budget measurably shrinks between the two
            # columns, then record what the server has *after* this column's
            # own bind. Without the delay both observations are the same
            # millisecond and the "never grows" assertion passes on a profile
            # that bound only its first statement -- `SET LOCAL` persists for
            # the transaction, so one early call looks identical to N.
            time.sleep(MEASURABLE_DELAY)
            result = original(self, *args, **kwargs)  # type: ignore[arg-type]
            [(value,)] = self.connection.execute(SHOW_STATEMENT_TIMEOUT).fetchall()
            observed.append(_timeout_ms(value))
            return result

        type(reader)._top_values = observing  # type: ignore[assignment,method-assign]
        try:
            reader.profile(target)
        finally:
            type(reader)._top_values = original  # type: ignore[method-assign]

    budget_ms = int(BUDGET.total_seconds() * 1000)
    assert len(observed) == 2, "both per-column statements should have been bounded"
    assert all(0 < seen < budget_ms for seen in observed), f"{observed} vs the whole budget {budget_ms}"
    # Strictly: the second column was charged less than the first, which is only
    # true if each statement is bound rather than one early call standing for all.
    assert observed[1] < observed[0], f"the later statement was not charged the elapsed time: {observed}"


def _timeout_ms(shown: str) -> int:
    """Postgres renders `statement_timeout` as `30s`, `29500ms` or `0`."""
    if shown.endswith("ms"):
        return int(shown[:-2])
    if shown.endswith("s"):
        return int(float(shown[:-1]) * 1000)
    return int(shown) * 1000


def test_an_exhausted_budget_leaves_no_time_for_another_statement(
    source_secret: Secret, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """The floor, at the boundary: a profile that has already overrun must not
    start another statement on an unbounded clock."""
    for statement in TWO_COLUMN_TABLE:
        source_admin.execute(statement)

    with postgres_profiler(source_secret, BUDGET) as reader:
        assert isinstance(reader, PostgresSourceProfiler)
        spent = PostgresSourceProfiler(
            reader.connection, BUDGET, time.monotonic() - BUDGET.total_seconds() - 1
        )
        assert spent.remaining() == timedelta(0)


def test_top_values_truncate_deterministically_when_a_tie_spans_the_cut(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """I8, at the one place a profile is not a pure aggregate.

    `LIMIT` without a total order is the classic non-determinism, and the
    fixture estate could not reach it: no column had more than four distinct
    values, so `LIMIT 5` never truncated and the tie-break never decided
    anything -- deleting `, 1 ASC` from `_TOP_VALUES` left the whole suite
    green (#49 review). Here three values tie for two remaining slots, so the
    tie-break picks the winners *and* orders the pair above them.
    """
    for statement in TIED_COLUMN:
        source_admin.execute(statement)
    target = ProfileTarget(schema_name="sales", name="tied", columns=(column("v"),))

    first = profiler.profile(target)
    second = profiler.profile(target)

    [profile] = first.columns
    assert [(f.value.masked, f.count) for f in profile.top_values] == [
        ("a*****1", 4),
        ("b*****2", 3),  # ties with cocoa33 on count; value order decides which is first
        ("c*****3", 3),
        ("d*****4", 2),  # three values tie for two slots; the two lowest win
        ("e*****5", 2),
    ]
    assert "f*****6" not in [f.value.masked for f in profile.top_values]
    assert profile.distinct_count == 6  # more distinct values than the sample carries
    assert first == second


def test_profiling_the_same_table_twice_returns_an_equal_profile(profiler: SourceProfiler) -> None:
    target = ProfileTarget(
        schema_name="sales", name="customers", columns=(column("email"), column("card", ordinal=2))
    )

    assert profiler.profile(target) == profiler.profile(target)


def test_a_hostile_column_name_is_an_identifier_not_a_statement(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    source_admin.execute(
        sql.SQL("CREATE TABLE sales.hostile (id bigint, {col} text)").format(
            col=sql.Identifier(HOSTILE_COLUMN)
        )
    )
    source_admin.execute("INSERT INTO sales.hostile VALUES (1, 'value')")
    source_admin.execute("GRANT SELECT ON sales.hostile TO steward_reader")

    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name="hostile", columns=(column(HOSTILE_COLUMN),))
    )

    assert profile.row_count == 1
    assert profile.columns[0].name == HOSTILE_COLUMN
    # The table the name tried to drop is still there.
    assert source_admin.execute("SELECT count(*) FROM sales.customers").fetchone() == (4,)


def test_an_empty_table_profiles_as_zeroes_rather_than_failing(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    source_admin.execute("CREATE TABLE sales.untouched (id bigint, note text)")
    source_admin.execute("GRANT SELECT ON sales.untouched TO steward_reader")

    profile = profiler.profile(
        ProfileTarget(schema_name="sales", name="untouched", columns=(column("id", "bigint"), column("note")))
    )

    assert profile.row_count == 0
    for column_profile in profile.columns:
        assert column_profile.null_count == 0
        assert column_profile.null_ratio == Decimal(0)
        assert column_profile.distinct_ratio == Decimal(0)
        assert column_profile.min_value is None
        assert column_profile.top_values == ()
        assert column_profile.semantic_type is SemanticType.UNKNOWN


def test_a_relation_with_no_columns_to_profile_still_reports_its_row_count(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """Postgres permits a table with no columns, and a profile of one must be a
    row count rather than a syntax error (`_profile_sql.ROW_COUNT_ONLY`)."""
    source_admin.execute("CREATE TABLE sales.columnless ()")
    source_admin.execute("INSERT INTO sales.columnless DEFAULT VALUES")
    source_admin.execute("GRANT SELECT ON sales.columnless TO steward_reader")

    profile = profiler.profile(ProfileTarget(schema_name="sales", name="columnless"))

    assert profile.row_count == 1
    assert profile.columns == ()
