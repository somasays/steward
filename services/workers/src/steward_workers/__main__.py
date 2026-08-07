"""Run a worker: `python -m steward_workers` or the `steward-worker` console
script (`[project.scripts]` in pyproject.toml).

The composition root for a worker process: the only place that reads the
environment, and the only place that decides which tracer this process gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid

from steward_queue import DSN_ENV, Worker
from steward_telemetry import tracer_from_env

WORKER_ID_ENV = "STEWARD_WORKER_ID"

SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def default_worker_id() -> str:
    """A worker id that is stable within a process and unique across them.

    It is written to `tasks.claimed_by` and to every audit row the worker
    causes, and it is the fencing token that stops a stalled worker from
    stomping a task a reaper handed to someone else -- so two live workers
    sharing an id would silently break that guarantee. Hostname plus a random
    suffix survives the Kubernetes case (a restarted pod reusing its name)
    without needing coordination.
    """
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def run(worker: Worker) -> None:
    """Poll until the process is asked to stop, then finish the task in hand.

    SIGTERM sets the stop event rather than killing the loop: `run_forever`
    returns after the current execution records its outcome, so a rolling
    deploy costs no re-executed work. A worker killed harder than that is the
    case leases exist for (N1) -- the task is reaped and retried.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError):  # not every platform has add_signal_handler
            loop.add_signal_handler(sig, stop.set)
    await worker.run_forever(stop)


def main() -> None:
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is not set")
    worker_id = os.environ.get(WORKER_ID_ENV, "").strip() or default_worker_id()
    asyncio.run(run(Worker(dsn, worker_id, tracer=tracer_from_env())))


if __name__ == "__main__":
    main()
