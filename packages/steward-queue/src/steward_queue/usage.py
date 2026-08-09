"""What an attempt has spent so far, readable while it is still spending.

A handler that returns reports its usage on its `TaskResult`, and that is the
number the run is charged. Two failures carry no result at all and were
therefore charged nothing: a handler that *raises* (the result never exists)
and one the worker *abandons* at its wall-clock cap (the thread is still
running when the loop gives up on it). Those are not cheap failures — a task
killed at its cap is by definition one that spent the cap — so leaving them at
zero made `runs.used_*` a lower bound that undercounted exactly the expensive
cases (I12, SPEC.md §13 D9).

The ledger closes that by being written *as the spending happens* rather than
reported at the end. The handler debits each increment as it commits to it; the
worker reads the total on the paths where no result survives.

Two properties this type exists to guarantee:

* **It is read from a thread that is not writing it.** The handler runs on its
  own thread and the worker abandons it from the event loop, so the read on the
  deadline path races the handler's next debit by construction. Every access
  takes the lock, and `total()` returns an immutable `RunBudget` rather than a
  view, so a caller cannot observe a half-applied increment.
* **It counts one attempt.** Retries get a fresh ledger. The cumulative figure
  an agent loop checks its budget against is a different quantity living in that
  loop's checkpoint; conflating them would charge a retry for everything its
  predecessors already paid for.

What it does not promise: a total on the abandoned path is a *snapshot*. The
handler's thread outlives the worker's read and may spend more before its own
timeouts fire, and that spend is never recorded. The recorded figure is
therefore a lower bound on a killed task, which is the conservative direction
and the same tradeoff `RunBudget.wall_clock` takes when it sums task time.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

from steward_schemas import RunBudget

__all__ = ["NOTHING_SPENT", "UsageLedger"]

NOTHING_SPENT = RunBudget(steps=0, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta())
"""The zero of the four dimensions -- an attempt that has not spent anything yet."""


class UsageLedger:
    """One attempt's spend, accumulated by the handler and read by the worker."""

    __slots__ = ("_lock", "_total")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = NOTHING_SPENT

    def debit(self, amount: RunBudget) -> None:
        """Add `amount` to this attempt's spend.

        Called by the handler at the point the resource is *gone* -- after a
        model call returns or fails, not before it is made. A debit recorded
        ahead of the spend would charge for work a refusal prevented.
        """
        with self._lock:
            self._total = RunBudget.total((self._total, amount))

    def total(self) -> RunBudget:
        """This attempt's spend so far. Safe to call while the handler runs."""
        with self._lock:
            return self._total
