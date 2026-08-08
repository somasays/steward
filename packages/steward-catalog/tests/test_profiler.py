"""The profiling read side against a real source database (#49).

No Steward database here: this is about what comes back out of a customer's
database and in what shape. Persistence and convergence are
`test_profile_convergence.py`; the end-to-end privacy claim is H7.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from steward_catalog import DiscoveredColumn, ProfileTarget, Secret, postgres_profiler
from steward_catalog.profiler import SourceProfiler
from steward_schemas import SemanticType

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

    assert profile.row_count == 3
    by_name = {column_profile.name: column_profile for column_profile in profile.columns}
    assert by_name["id"].null_count == 0
    assert by_name["id"].distinct_count == 3
    assert by_name["customer"].null_count == 1
    assert by_name["customer"].null_ratio == Decimal("0.333333")
    assert by_name["customer"].distinct_count == 2


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
    assert [frequency.value.masked for frequency in card.top_values] == ["****-****-****-****"]
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
    ]
    assert [frequency.count for frequency in total.top_values] == [2, 1]


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
    assert source_admin.execute("SELECT count(*) FROM sales.customers").fetchone() == (2,)


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
