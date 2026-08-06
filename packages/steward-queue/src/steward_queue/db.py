"""The connection type this package's API speaks.

Every queue function takes a caller-owned connection and never commits it.
That is what makes I8's "enqueue is transactional with the state change that
caused it" mechanical rather than a convention: the caller opens the
transaction, writes its domain change, enqueues, and commits once. It is also
how I7 holds -- the audit row is written on the same connection, inside the
same transaction, as the mutation it records.
"""

import psycopg
from psycopg.rows import TupleRow

type QueueConnection = psycopg.Connection[TupleRow]
"""A psycopg 3 connection in manual-commit mode, owned by the caller."""


def connect(dsn: str) -> QueueConnection:
    """Open a queue connection. Manual commit: the caller owns transactions."""
    return psycopg.connect(dsn, autocommit=False)
