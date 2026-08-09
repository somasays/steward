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
from steward_schemas import ColumnProfile, SemanticType, TableProfile

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


# The extrema fixture (issue #70). Every column is chosen so the *lexical* order
# of the text renderings disagrees with the type's own order, because a fixture
# where they agree cannot tell a fixed profiler from a broken one:
#   n: 2, 10, 100          lexical min "10", max "2"   typed min 2, max 100
#   d: 1 hour/2/10 days    lexical max "2 days"        typed max "10 days"
# `t` is text, where lexical *is* the type's order, and `j`/`u` have no `min`
# aggregate at all -- `u` being the case that disproves the obvious design, since
# uuid is orderable and still has no aggregate.
EXTREMA_TABLE = (
    "CREATE TABLE sales.extrema (n integer, d interval, t text, j json, u uuid)",
    "INSERT INTO sales.extrema (n, d, t, j, u) VALUES "
    "(2, '1 hour', 'apple', '{\"a\":1}', '11111111-1111-1111-1111-111111111111'),"
    "(10, '2 days', 'banana', '{\"b\":2}', '22222222-2222-2222-2222-222222222222'),"
    "(100, '10 days', 'cherry', '{\"c\":3}', '33333333-3333-3333-3333-333333333333')",
    "GRANT SELECT ON sales.extrema TO steward_reader",
)

EXTREMA_COLUMNS = (
    column("n", "integer", 1),
    column("d", "interval", 2),
    column("t", "text", 3),
    column("j", "json", 4),
    column("u", "uuid", 5),
)


@pytest.fixture
def extrema(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> dict[str, ColumnProfile]:
    for statement in EXTREMA_TABLE:
        source_admin.execute(statement)
    profile = profiler.profile(ProfileTarget(schema_name="sales", name="extrema", columns=EXTREMA_COLUMNS))
    return {column_profile.name: column_profile for column_profile in profile.columns}


def test_numeric_extrema_are_the_types_own_not_the_renderings(
    extrema: dict[str, ColumnProfile],
) -> None:
    """The defect this issue is about: 2, 10, 100 reported min `10`, max `2`.

    Asserted through `length`, because the values themselves are masked and stay
    that way -- `2` masks to `*` and `100` to `***`, so the published profile
    distinguishes them without publishing them. Lexical ordering would give a
    minimum of `10` (two characters) and a maximum of `2` (one).
    """
    numeric = extrema["n"]
    assert numeric.min_value is not None and numeric.max_value is not None

    assert numeric.min_value.length == 1, "the minimum is not 2"
    assert numeric.max_value.length == 3, "the maximum is not 100"
    assert numeric.min_value.masked == "*" and numeric.max_value.masked == "***"


def test_temporal_extrema_are_the_types_own_not_the_renderings(
    extrema: dict[str, ColumnProfile],
) -> None:
    """Dates render in an order-preserving format, so they cannot show this;
    intervals can. `10 days` sorts before `2 days` as text and after it in time.
    """
    interval = extrema["d"]
    assert interval.min_value is not None and interval.max_value is not None

    assert interval.min_value.length == len("01:00:00"), "the minimum is not 1 hour"
    assert interval.max_value.length == len("10 days"), "the maximum is not 10 days"


def test_text_extrema_keep_their_well_defined_lexical_order(
    extrema: dict[str, ColumnProfile],
) -> None:
    """For text the rendering *is* the value, so its order is the type's order --
    nothing to correct, and the fix must not take the extrema away."""
    text = extrema["t"]
    assert text.min_value is not None and text.max_value is not None

    assert text.min_value.masked == "a***e"  # apple
    assert text.max_value.masked == "c****y"  # cherry


@pytest.mark.parametrize("name", ["j", "u"])
def test_a_type_with_no_extrema_publishes_none_rather_than_a_lexical_stand_in(
    extrema: dict[str, ColumnProfile], name: str
) -> None:
    """A column whose type has no `min`/`max` reports no extrema at all.

    The alternative -- falling back to the text rendering -- is what this issue
    is about: a value true of the renderings and false of the column, in the
    field a classifier reads as the column's minimum. Counts still work, because
    those are computed on the rendering.
    """
    unordered = extrema[name]

    assert unordered.min_value is None
    assert unordered.max_value is None
    assert unordered.distinct_count == 3  # the rest of the profile is unaffected


def test_extrema_are_masked_like_every_other_sampled_value(
    extrema: dict[str, ColumnProfile],
) -> None:
    """I6 does not weaken because a value arrived through an aggregate."""
    for column_profile in extrema.values():
        for sample in (column_profile.min_value, column_profile.max_value):
            if sample is not None:
                assert "apple" not in sample.masked
                assert "cherry" not in sample.masked
                assert sample.masked.count("*") >= 1


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
    source_secret: Secret,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
    monkeypatch: pytest.MonkeyPatch,
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
        profile = _profile_with_drain(reader, target, source_admin, monkeypatch)

    [column_profile] = profile.columns
    assert column_profile.distinct_count == 3
    assert len(column_profile.top_values) == 3, "the sample saw a different table than the statistics"
    assert {frequency.value.masked for frequency in column_profile.top_values} != {"***"}


def _profile_with_drain(
    reader: SourceProfiler,
    target: ProfileTarget,
    admin: psycopg.Connection[psycopg.rows.TupleRow],
    monkeypatch: pytest.MonkeyPatch,
) -> TableProfile:
    """Run a profile with a committed DELETE landing between its two statements.

    The seam is the profiler's own `_top_values`: wrapping it is how the drain
    is placed *inside* one profile rather than between two, which is where the
    race lives.
    """
    original = PostgresSourceProfiler._top_values

    def draining(self: PostgresSourceProfiler, target_: ProfileTarget, column_: DiscoveredColumn):  # type: ignore[no-untyped-def]
        admin.execute(DRAIN_THE_THIRD_VALUE)
        return original(self, target_, column_)

    # `monkeypatch` rather than a hand-restored assignment: an interruption
    # between the assignment and the `try` would leave the class patched for the
    # rest of the session, and which test then failed would depend on ordering.
    monkeypatch.setattr(PostgresSourceProfiler, "_top_values", draining)
    return reader.profile(target)


SHOW_STATEMENT_TIMEOUT = "SHOW statement_timeout"

SLEEP_PAST_THE_FLOOR = "SELECT pg_sleep(1)"
"""Longer than the floor an exhausted budget leaves, so the timeout fires."""

MEASURABLE_DELAY = 0.05
"""Long enough that the budget shrinks by whole milliseconds between columns."""

TWO_COLUMN_TABLE = (
    "CREATE TABLE sales.timed (a text, b text)",
    "INSERT INTO sales.timed (a, b) VALUES ('x','p'),('y','q'),('z','r')",
    "GRANT SELECT ON sales.timed TO steward_reader",
)


def test_each_statement_is_charged_only_what_is_left_of_the_budget(
    source_secret: Secret,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I12 on a resource held on someone else's database.

    A profile is one stats pass plus one query per column, all in one
    `REPEATABLE READ` transaction. `statement_timeout` bounds a *statement*, so
    allowing each the whole budget would let a 60-column table hold an
    `ACCESS SHARE` lock and an `xmin` pin on a customer's relation for 61 times
    the advertised cap -- long after the worker recorded the task
    `budget_exceeded` (#49 review).

    Asserted on what the server reports inside the transaction. Which assertion
    catches which regression is worth naming, because they are not the same:
    removing *all* the binds leaves the connection-level value -- the whole
    budget -- and the `< budget_ms` assertion fails; removing only the
    per-column bind leaves the stats pass's `set_config`, which persists for the
    transaction, so both observations read the same value and it is
    `observed[1] < observed[0]` that fails. A test asserting only the first
    would pass on a profile that bound one statement and let the rest run at the
    full cap.
    """
    for statement in TWO_COLUMN_TABLE:
        source_admin.execute(statement)
    target = ProfileTarget(schema_name="sales", name="timed", columns=(column("a"), column("b", ordinal=2)))
    observed: list[int] = []

    with postgres_profiler(source_secret, BUDGET) as reader:
        original = PostgresSourceProfiler._top_values

        def observing(self: PostgresSourceProfiler, target_: ProfileTarget, column_: DiscoveredColumn):  # type: ignore[no-untyped-def]
            # Sleep first, so the budget measurably shrinks between the two
            # columns, then record what the server has *after* this column's
            # own bind. Without the delay both observations are the same
            # millisecond and the "never grows" assertion passes on a profile
            # that bound only its first statement -- `SET LOCAL` persists for
            # the transaction, so one early call looks identical to N.
            time.sleep(MEASURABLE_DELAY)
            result = original(self, target_, column_)
            [(value,)] = self.connection.execute(SHOW_STATEMENT_TIMEOUT).fetchall()
            observed.append(_timeout_ms(value))
            return result

        monkeypatch.setattr(PostgresSourceProfiler, "_top_values", observing)
        reader.profile(target)

    budget_ms = int(BUDGET.total_seconds() * 1000)
    assert len(observed) == 2, "both per-column statements should have been bounded"
    assert all(0 < seen < budget_ms for seen in observed), f"{observed} vs the whole budget {budget_ms}"
    # Strictly: the second column was charged less than the first, which is only
    # true if each statement is bound rather than one early call standing for all.
    assert observed[1] < observed[0], f"the later statement was not charged the elapsed time: {observed}"


TIMEOUT_UNITS_MS: tuple[tuple[str, int], ...] = (
    ("ms", 1),
    ("min", 60_000),
    ("h", 3_600_000),
    ("d", 86_400_000),
    ("s", 1_000),
)
"""Every unit `SHOW statement_timeout` can render, longest suffix first.

Probed against a real server: `0`, `1ms`, `29943ms`, `29s`, `30s`, `90s`,
`1min`, `30min`, `1h`. The minute and hour forms are unreachable from this
file's 30-second `BUDGET` but not from production's -- `PROFILE_ASSET_BUDGET`
is 30 minutes, which renders `30min` -- so a helper that handled only `ms`/`s`
would break the moment this test was aligned with the real budget (#49 review).
`ms` before `min` and `s` last, because `endswith` would otherwise match the
wrong suffix.
"""


def _timeout_ms(shown: str) -> int:
    """`SHOW statement_timeout`'s rendering as whole milliseconds."""
    for suffix, scale in TIMEOUT_UNITS_MS:
        if shown.endswith(suffix):
            return int(float(shown[: -len(suffix)]) * scale)
    return int(shown)  # bare `0`, the only unitless rendering


def test_an_exhausted_budget_leaves_no_time_for_another_statement(
    source_secret: Secret, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """The boundary: a profile that has already overrun must not start another
    statement on an unbounded clock.

    Asserted against the server rather than against the clamp alone. The clamp
    (`remaining()` never going negative) and the floor
    (`statement_timeout_ms`'s minimum) are each covered elsewhere; what this
    adds is that their *composition* reaches Postgres as a timeout that fires,
    which is the sentence the name makes.
    """
    for statement in TWO_COLUMN_TABLE:
        source_admin.execute(statement)

    with postgres_profiler(source_secret, BUDGET) as reader:
        assert isinstance(reader, PostgresSourceProfiler)
        spent = PostgresSourceProfiler(
            reader.connection, BUDGET, time.monotonic() - BUDGET.total_seconds() - 1
        )
        assert spent.remaining() == timedelta(0)

        spent._bound_next_statement()
        with pytest.raises(psycopg.errors.QueryCanceled):
            spent.connection.execute(SLEEP_PAST_THE_FLOOR).fetchall()
        spent.connection.rollback()


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


# Every type class a source might hold, including the ones that disprove the
# obvious designs. `ORDERED_COLUMNS` asks whether `min`/`max` aggregates resolve
# *and* can run; the tempting question -- does the type have a btree operator
# class -- gets `uuid`, `bytea`, `jsonb` and `boolean` wrong in one direction
# (orderable, no aggregate) and `varchar` and arrays wrong in the other. For an
# array the opclass is not a replacement for the aggregate question but the
# missing second half of it: `min(anyarray)` resolves for *every* array type and
# executes only where the element type has a comparison function, which is
# precisely what a default btree opclass on the element provides. So this table
# carries arrays on both sides of that line -- `uuid[]`/`boolean[]`/`jsonb[]`
# execute (no `min` aggregate on the element, but an opclass), `json[]`,
# `point[]` and `box[]` do not.
TYPE_PROBE = (
    "CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy')",
    "CREATE TYPE pair AS (a integer, b integer)",
    "CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0)",
    "CREATE DOMAIN int_array AS integer[]",
    "CREATE DOMAIN json_doc AS json",
    "CREATE TABLE sales.probe ("
    "  c_int integer, c_bigint bigint, c_smallint smallint, c_numeric numeric(10,2),"
    "  c_real real, c_float double precision, c_money money,"
    "  c_text text, c_varchar varchar(10), c_char char(4), c_name name,"
    "  c_bool boolean, c_uuid uuid, c_bytea bytea,"
    "  c_date date, c_ts timestamp, c_tstz timestamptz, c_time time, c_timetz timetz,"
    "  c_interval interval, c_json json, c_jsonb jsonb, c_xml xml, c_point point,"
    "  c_inet inet, c_cidr cidr, c_macaddr macaddr, c_array integer[],"
    "  c_textarray text[], c_domain positive_int, c_oid oid, c_bit bit(3),"
    "  c_tsvector tsvector, c_enum mood,"
    "  c_jsonarray json[], c_pointarray point[], c_boxarray box[], c_uuidarray uuid[],"
    "  c_jsonbarray jsonb[], c_boolarray boolean[], c_enumarray mood[],"
    "  c_domainarray int_array, c_domarr positive_int[], c_domjsonarray json_doc[],"
    "  c_pairarray pair[])",
    # Two rows, two distinct non-null values in every column but `c_xml`. This
    # is what makes the test a test: `min(anyarray)` *resolves* for every array
    # type and only fails to find a comparison function once it has two values
    # to compare, so an empty probe asks the same question `ORDERED_COLUMNS`
    # asks (does it resolve) and the prediction is compared with itself.
    "INSERT INTO sales.probe VALUES ("
    "  2, 2, 2, 2.5, 2.5, 2.5, '2.00',"
    "  'a', 'a', 'a', 'a',"
    "  false, '11111111-1111-1111-1111-111111111111', '\\x01',"
    "  '2020-01-01', '2020-01-01 00:00', '2020-01-01 00:00+00', '00:00', '00:00+00',"
    "  '1 hour', '{\"a\":1}', '{\"a\":1}', NULL, '(1,1)',"
    "  '10.0.0.1', '10.0.0.0/8', '08:00:2b:01:02:03', '{1}',"
    "  '{a}', 1, 1, B'001', to_tsvector('a'), 'sad',"
    "  ARRAY['{\"a\":1}'::json], ARRAY['(1,1)'::point], ARRAY['((0,0),(1,1))'::box],"
    "  ARRAY['11111111-1111-1111-1111-111111111111'::uuid], ARRAY['{\"a\":1}'::jsonb],"
    "  ARRAY[false], ARRAY['sad'::mood], '{1}', '{1}', ARRAY['{\"a\":1}'::json_doc],"
    "  ARRAY[(1,1)::pair])",
    "INSERT INTO sales.probe VALUES ("
    "  100, 100, 100, 100.5, 100.5, 100.5, '100.00',"
    "  'b', 'b', 'b', 'b',"
    "  true, '22222222-2222-2222-2222-222222222222', '\\x02',"
    "  '2021-01-01', '2021-01-01 00:00', '2021-01-01 00:00+00', '01:00', '01:00+00',"
    "  '10 days', '{\"b\":2}', '{\"b\":2}', NULL, '(2,2)',"
    "  '10.0.0.2', '10.1.0.0/16', '08:00:2b:01:02:04', '{2}',"
    "  '{b}', 2, 2, B'010', to_tsvector('b'), 'happy',"
    "  ARRAY['{\"b\":2}'::json], ARRAY['(2,2)'::point], ARRAY['((0,0),(2,2))'::box],"
    "  ARRAY['22222222-2222-2222-2222-222222222222'::uuid], ARRAY['{\"b\":2}'::jsonb],"
    "  ARRAY[true], ARRAY['happy'::mood], '{2}', '{2}', ARRAY['{\"b\":2}'::json_doc],"
    "  ARRAY[(2,2)::pair])",
    "GRANT SELECT ON sales.probe TO steward_reader",
)

UNVALUED_PROBE_COLUMN = "c_xml"
"""The one probe column that holds no value.

`pgserver`'s Postgres is built without libxml, so an `xml` literal is rejected
outright. It costs nothing here: `xml` has no `min` aggregate, so it fails at
*resolution* and how many rows exist never enters into it. Named rather than
skipped silently, because "this column has no values" is exactly the condition
that made the earlier version of this test vacuous.
"""

# `pair[]`: an array whose element is a composite type. Postgres compares those
# through `record_ops`, an opclass `pg_opclass` files under the `record`
# pseudo-type rather than under `pair`, so the element-opclass conjunct does not
# find it and the column is predicted *unordered* while `min()` runs fine. That
# is the safe direction -- a fact not published, never a query that errors --
# and it is asserted by name here so the residual stays visible instead of being
# absorbed into a `<=`.
UNDER_PREDICTED_BY_DESIGN = frozenset({"c_pairarray"})

PROBE_COLUMN_NAMES = (
    "SELECT attname FROM pg_attribute WHERE attrelid = 'sales.probe'::regclass "
    "AND attnum > 0 AND NOT attisdropped"
)

PROBE_COLUMN_VALUES = sql.SQL("SELECT count({col}), count(DISTINCT ({col})::text) FROM sales.probe")


def _aggregate_runs(
    connection: psycopg.Connection[psycopg.rows.TupleRow], aggregate: str, column: str
) -> bool:
    """Whether `aggregate(column)` actually executes on this connection.

    Inside a savepoint, so a failure does not abort the profiler's transaction
    and the next column is asked on the same session -- same role, same
    `search_path`, same snapshot as the prediction. That matters: an aggregate
    the prediction can see and the executing role cannot is one of the ways
    these two answers come apart.
    """
    try:
        with connection.transaction():
            connection.execute(
                sql.SQL("SELECT {agg}({col})::text FROM sales.probe").format(
                    agg=sql.Identifier(aggregate), col=sql.Identifier(column)
                )
            ).fetchall()
    except psycopg.Error:
        return False
    return True


def test_the_orderability_prediction_matches_what_min_and_max_actually_do(
    source_secret: Secret, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """`ORDERED_COLUMNS` must agree with the server, type class by type class.

    This is the check that keeps the prediction honest, and it only is one if
    the probe holds **data**. `min(anyarray)` resolves for every array type and
    fails at *execution* -- "could not identify a comparison function for type
    json" -- the moment there are two values to compare. An empty probe asks the
    catalog the same question `ORDERED_COLUMNS` asks it, so prediction and
    oracle were interrogating one fact and the test could not fail: `json[]`,
    `point[]` and `box[]` were predicted ordered, and a real column of either
    errored the *whole asset profile*, because the extrema ride in the single
    stats query. Hence the two rows and the guard below.

    Both aggregates are asked, not just `min`, because `_TYPED_EXTREMA` runs
    both; and both are asked on the profiler's own connection, because the
    prediction is only sound for the session that will run the statistics.

    The two directions of disagreement are not the same failure, so they are
    asserted separately. Predicting ordered where the aggregate cannot run
    errors the whole profile; predicting unordered where it can costs one fact.
    """
    for statement in TYPE_PROBE:
        source_admin.execute(statement)

    with postgres_profiler(source_secret, BUDGET) as reader:
        assert isinstance(reader, PostgresSourceProfiler)
        predicted = reader._ordered_columns(ProfileTarget(schema_name="sales", name="probe", columns=()))
        names = [str(name) for (name,) in reader.connection.execute(PROBE_COLUMN_NAMES).fetchall()]
        # The probe must actually hold values, or `min(anyarray)` never gets far
        # enough to look for a comparison function and this test asserts nothing.
        for name in names:
            counted = reader.connection.execute(
                PROBE_COLUMN_VALUES.format(col=sql.Identifier(name))
            ).fetchone()
            expected = (0, 0) if name == UNVALUED_PROBE_COLUMN else (2, 2)
            assert counted == expected, f"{name} holds {counted}, cannot exercise execution"
        actual = {
            name
            for name in names
            if _aggregate_runs(reader.connection, "min", name)
            and _aggregate_runs(reader.connection, "max", name)
        }
        reader.connection.rollback()

    assert predicted <= actual, (
        "predicted ordered but the aggregate cannot run -- this errors the whole "
        f"asset profile: {sorted(predicted - actual)}"
    )
    assert actual - predicted == UNDER_PREDICTED_BY_DESIGN, (
        f"unordered predictions that could have published a fact: {sorted(actual - predicted)}"
    )
    # ...and the probe covers both answers, so agreeing is not agreeing on an
    # empty set or on "everything is orderable".
    assert {"c_int", "c_text", "c_varchar", "c_array", "c_enum", "c_domain"} <= predicted
    assert {"c_uuidarray", "c_boolarray", "c_jsonbarray", "c_enumarray", "c_domainarray"} <= predicted
    assert {"c_uuid", "c_json", "c_jsonb", "c_bool", "c_bytea", "c_point"}.isdisjoint(predicted)
    assert {"c_jsonarray", "c_pointarray", "c_boxarray", "c_domjsonarray"}.isdisjoint(predicted)


ARRAY_TABLE = (
    "CREATE TABLE sales.arrays (tags json[], fence point[], labels text[])",
    "INSERT INTO sales.arrays (tags, fence, labels) VALUES "
    "(ARRAY['{\"a\":1}'::json], ARRAY['(1,1)'::point], ARRAY['alpha']),"
    "(ARRAY['{\"b\":2}'::json], ARRAY['(2,2)'::point], ARRAY['bravo']),"
    "(ARRAY['{\"c\":3}'::json], ARRAY['(3,3)'::point], ARRAY['cadet'])",
    "GRANT SELECT ON sales.arrays TO steward_reader",
)


def test_an_array_of_an_incomparable_element_type_still_profiles(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """The whole-asset failure the orderability prediction has to avoid.

    `min(anyarray)` resolves for `json[]` and `point[]` and then errors on the
    second distinct value -- and the extrema ride in the *single* stats query,
    so that error is not one missing fact but the whole profile: the handler
    records `urn:steward:asset-unprofilable` for the asset. A `tags json[]` or
    `geofence point[]` column profiles fine on a lexical `min((col)::text)`, so
    predicting these ordered would turn working profiles into hard failures --
    and data-dependently, green until a second distinct value lands.

    `text[]` is here as the control: its element has a comparison function, so
    it keeps typed extrema and the fix is not "arrays publish nothing".
    """
    for statement in ARRAY_TABLE:
        source_admin.execute(statement)

    profile = profiler.profile(
        ProfileTarget(
            schema_name="sales",
            name="arrays",
            columns=(
                column("tags", "json[]", 1),
                column("fence", "point[]", 2),
                column("labels", "text[]", 3),
            ),
        )
    )

    by_name = {column_profile.name: column_profile for column_profile in profile.columns}
    assert profile.row_count == 3
    for name in ("tags", "fence"):
        assert by_name[name].min_value is None and by_name[name].max_value is None
        assert by_name[name].distinct_count == 3  # the rest of the profile survives
    assert by_name["labels"].min_value is not None and by_name["labels"].max_value is not None


SHADOW_SCHEMA = (
    "CREATE SCHEMA shadow",
    "CREATE FUNCTION shadow.pick(json, json) RETURNS json LANGUAGE sql IMMUTABLE AS 'SELECT $1'",
    "CREATE AGGREGATE shadow.min(json) (sfunc = shadow.pick, stype = json)",
    "CREATE AGGREGATE shadow.max(json) (sfunc = shadow.pick, stype = json)",
    "CREATE TABLE sales.shadowed (payload json)",
    "INSERT INTO sales.shadowed (payload) VALUES ('{\"a\":1}'), ('{\"b\":2}')",
    "GRANT SELECT ON sales.shadowed TO steward_reader",
)

DROP_SHADOW_SCHEMA = "DROP SCHEMA shadow CASCADE"


def test_an_aggregate_the_reader_cannot_see_does_not_count_as_orderable(
    profiler: SourceProfiler, source_admin: psycopg.Connection[psycopg.rows.TupleRow]
) -> None:
    """An aggregate exists, and `min(payload)` still does not resolve.

    `pg_proc` is the whole cluster, not the connection's `search_path`, so a
    `min`/`max` pair defined in a schema the reader does not search satisfies a
    prediction the reader's own statement cannot execute. Nothing exotic is
    needed to arrange it -- a customer with a `stats` or `compat` schema on
    their own search path, which Steward's role does not share, is enough.

    The failure is the one that matters: the prediction says ordered, the stats
    query says `function min(json) does not exist`, and the asset is
    unprofilable. `pg_function_is_visible` is what keeps the question the
    reader's own.
    """
    for statement in SHADOW_SCHEMA:
        source_admin.execute(statement)
    try:
        profile = profiler.profile(
            ProfileTarget(schema_name="sales", name="shadowed", columns=(column("payload", "json"),))
        )
    finally:
        source_admin.execute(DROP_SHADOW_SCHEMA)

    [payload] = profile.columns
    assert payload.min_value is None and payload.max_value is None
    assert payload.distinct_count == 2


HALF_AN_AGGREGATE = (
    # In `public`, and the reader is granted USAGE on it for the duration --
    # `public` is on the default `search_path` but the fixture revokes it from
    # PUBLIC, and an aggregate the reader cannot see is excluded by the
    # visibility conjunct instead, which is a different test passing.
    "GRANT USAGE ON SCHEMA public TO steward_reader",
    "CREATE FUNCTION public.pick(json, json) RETURNS json LANGUAGE sql IMMUTABLE AS 'SELECT $1'",
    "CREATE AGGREGATE public.min(json) (sfunc = public.pick, stype = json)",
    "CREATE TABLE sales.halved (payload json)",
    "INSERT INTO sales.halved (payload) VALUES ('{\"a\":1}'), ('{\"b\":2}')",
    "GRANT SELECT ON sales.halved TO steward_reader",
)

DROP_HALF_AN_AGGREGATE = (
    "DROP AGGREGATE public.min(json)",
    "DROP FUNCTION public.pick(json, json)",
    "REVOKE USAGE ON SCHEMA public FROM steward_reader",
)

READER_SEES_THE_AGGREGATE = (
    "SELECT pg_function_is_visible(p.oid) FROM pg_proc AS p "
    "WHERE p.proname = 'min' AND p.proargtypes[0] = 'json'::regtype"
)


def test_a_type_with_only_a_min_aggregate_is_not_orderable(
    profiler: SourceProfiler,
    source_admin: psycopg.Connection[psycopg.rows.TupleRow],
    source_dsn: str,
) -> None:
    """`_TYPED_EXTREMA` runs both aggregates, so the prediction must need both.

    A visible `min(json)` and no `max(json)` -- a customer defining the
    convenience aggregate they wanted is all it takes -- makes an oracle that
    asks about `min` alone predict ordered, and then the stats query dies on
    `function max(json) does not exist`, taking every column of the asset with
    it. Asking for both names is the whole fix.

    The visibility of the planted aggregate is asserted *as the reader* before
    the profile runs: without that, the reader's missing USAGE on `public`
    excludes it and this passes for the visibility conjunct's reason rather
    than this one.
    """
    for statement in HALF_AN_AGGREGATE:
        source_admin.execute(statement)
    try:
        with psycopg.connect(source_dsn) as reader:
            assert reader.execute(READER_SEES_THE_AGGREGATE).fetchall() == [(True,)]
        profile = profiler.profile(
            ProfileTarget(schema_name="sales", name="halved", columns=(column("payload", "json"),))
        )
    finally:
        for statement in DROP_HALF_AN_AGGREGATE:
            source_admin.execute(statement)

    [payload] = profile.columns
    assert payload.min_value is None and payload.max_value is None
