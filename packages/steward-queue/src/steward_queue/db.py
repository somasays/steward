"""The connection type this package's API speaks.

Every queue function takes a caller-owned connection and never commits it.
That is what makes I8's "enqueue is transactional with the state change that
caused it" mechanical rather than a convention: the caller opens the
transaction, writes its domain change, enqueues, and commits once. It is also
how I7 holds -- the audit row is written on the same connection, inside the
same transaction, as the mutation it records.
"""

from datetime import timedelta

import psycopg
from psycopg.rows import TupleRow

from steward_queue import _sql

type QueueConnection = psycopg.Connection[TupleRow]
"""A psycopg 3 connection in manual-commit mode, owned by the caller."""

DSN_ENV = "STEWARD_DATABASE_URL"
"""Environment variable every Steward process reads its Postgres DSN from.

The name lives here, next to `connect`, so the API and the workers cannot drift
onto two spellings of the same setting. Reading the environment is still the
services' job (CLAUDE.md: business logic in packages, wiring in services) --
this package only declares what the setting is called.
"""

MIN_STATEMENT_TIMEOUT_MS = 1
"""The floor for a derived `statement_timeout`, in milliseconds.

Postgres reads `statement_timeout = 0` as "no timeout", which is the exact
opposite of what a zero wall-clock budget means. A budget of `timedelta(0)` is
an already-exhausted budget: it must fail immediately, never run forever. Every
non-positive budget therefore floors to 1 ms here, and the guard that turns
that into a typed `budget_exceeded` failure is the worker's (I12).
"""


def statement_timeout_ms(budget: timedelta) -> int:
    """A wall-clock budget as a Postgres `statement_timeout`, in milliseconds."""
    return max(MIN_STATEMENT_TIMEOUT_MS, int(budget.total_seconds() * 1000))


def connect(dsn: str, *, statement_timeout: timedelta | None = None) -> QueueConnection:
    """Open a queue connection. Manual commit: the caller owns transactions.

    `statement_timeout` is one of the three bounds a wall-clock budget rests on
    (SPEC.md §13, D7). It is the cheapest: Postgres aborts the statement, so a
    handler waiting on a lock or a pathological query comes back at its cap
    instead of holding a worker slot until its lease expires -- and it comes
    back on its own thread, where the exception is the handler's to unwind
    (I12). It is not the last word, because it bounds a statement rather than
    an execution: a handler that never calls the database, or calls it N times,
    is bounded by the worker's deadline instead. Passed as a libpq connection
    option, so it applies to every statement on the connection without a
    per-transaction `SET`.
    """
    if statement_timeout is None:
        return psycopg.connect(dsn, autocommit=False)
    options = f"-c statement_timeout={statement_timeout_ms(statement_timeout)}"
    return psycopg.connect(dsn, autocommit=False, options=options)


def set_statement_timeout(conn: QueueConnection, budget: timedelta) -> None:
    """Re-point an open connection's statement timeout at a different bound.

    A worker's connection lives under two different deadlines in turn: the
    task's wall-clock budget while a handler is running, and the claim's lease
    while the worker is doing its own bookkeeping. Leaving the handler's
    (possibly very small) budget in place afterwards would let a tight budget
    abort the statements that record the outcome -- turning a clean
    `budget_exceeded` into a task nobody could write a result for.
    """
    conn.execute(_sql.SET_STATEMENT_TIMEOUT, {"milliseconds": str(statement_timeout_ms(budget))})


def terminate_backend(conn: QueueConnection, backend_pid: int) -> bool:
    """Have Postgres end the session running on `backend_pid`.

    The interesting part is what this is *not*: it is not a method on the
    connection being ended. The worker uses it to dispose of the session of a
    handler running on another thread, and psycopg connections are not
    thread-safe -- so the only thing that crosses the thread boundary is the
    backend's pid, an integer, and the disposal travels over the caller's own
    connection. A connection object is therefore never reachable from two
    contexts at once, by construction rather than by timing (SPEC.md §13, D7).

    Terminating rather than cancelling is what makes it useful. The session
    being abandoned is usually idle inside a transaction nobody will ever
    commit -- there is no statement to cancel, and its row locks would block
    the very statements the worker needs to record the outcome. Ending the
    session drops the transaction and the locks with it, which is also what
    turns "the abandoned handler's writes are uncommitted" into "the abandoned
    handler can no longer write".

    Returns whether Postgres found the backend; one that has already finished
    is a false, not an error.
    """
    row = conn.execute(_sql.TERMINATE_BACKEND, {"pid": backend_pid}).fetchone()
    return bool(row is not None and row[0])
