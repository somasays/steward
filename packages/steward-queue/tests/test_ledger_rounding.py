"""`ledger_cost` must round the way the ledger's own column rounds.

A comparison between a computed cost and a stored one is only meaningful if both
sides round identically, and the two defaults disagree: `Decimal.quantize` rounds
ties to even, PostgreSQL's `numeric` rounds them away from zero. The disagreement
is invisible on almost every figure and appears on exact halves — so the failure
it causes is intermittent, lands on a *correct* run, and depends on the sixth
decimal place of whatever a model happened to charge.

The cases are checked against the database rather than against a table written
here. Asserting `ledger_cost(x) == 0.000057` would only prove the helper does
what whoever wrote the test believed Postgres does; asserting it equals
`SELECT x::numeric(14,6)` proves the two agree, and keeps proving it if the
column's scale ever changes.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from steward_queue import LEDGER_COST_SCALE, ledger_cost
from steward_queue.db import QueueConnection

pytestmark = pytest.mark.invariants

TIES = [
    "0.0000565",  # half, rounds *up* in postgres and *down* under half-even
    "0.0000545",  # half, the same disagreement one digit lower
    "0.0000555",
    "0.0000575",
    "0.0000005",
    "0.1234565",
]

ORDINARY = ["0", "0.000001", "0.0000004", "0.00005572", "1.9999994", "12.3456789"]


@pytest.mark.parametrize("raw", TIES + ORDINARY)
def test_the_helper_agrees_with_the_column(conn: QueueConnection, raw: str) -> None:
    """One source of truth for how a cost is stored: the column itself."""
    stored = conn.execute(f"SELECT {raw}::numeric(14,6)").fetchone()
    conn.rollback()
    assert stored is not None

    assert ledger_cost(Decimal(raw)) == stored[0], (
        f"{raw} stores as {stored[0]} and rounds to {ledger_cost(Decimal(raw))}"
    )


@pytest.mark.parametrize("raw", TIES)
def test_the_python_default_would_have_disagreed_on_at_least_one_tie(raw: str) -> None:
    """The bug this helper exists for, made visible.

    Not every tie disagrees — `0.0000555` rounds to `0.000056` either way — so
    this asserts across the set rather than per case, and the assertion is that
    the two rules are *not* interchangeable.
    """
    half_even = Decimal(raw).quantize(LEDGER_COST_SCALE, rounding=ROUND_HALF_EVEN)
    half_up = ledger_cost(Decimal(raw))

    assert half_even.compare(half_up) in (Decimal("0"), Decimal("-1"))


def test_the_two_rules_actually_differ_somewhere() -> None:
    """Otherwise the test above is satisfied by two identical rules, and this
    whole helper is ceremony."""
    differing = [
        raw
        for raw in TIES
        if Decimal(raw).quantize(LEDGER_COST_SCALE, rounding=ROUND_HALF_EVEN)
        != ledger_cost(Decimal(raw))
    ]

    assert differing, "half-even and half-up agreed on every tie case; the cases are not ties"


def test_a_cost_that_needs_no_rounding_is_unchanged() -> None:
    """The positive case: rounding must not perturb a figure already at scale."""
    exact = Decimal("0.000056")

    assert ledger_cost(exact) == exact
