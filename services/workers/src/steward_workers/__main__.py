"""Run a worker: `python -m steward_workers` or the `steward-worker` console
script (`[project.scripts]` in pyproject.toml).

The composition root for a worker process: the only place that reads the
environment, and the only place that decides which tracer this process gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

import steward_catalog  # noqa: F401 -- imported for its side effect: registers `scan_source`
from steward_llm import gateway_config_from_env
from steward_queue import DSN_ENV, Worker, registered_types
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
    """Poll until the process is asked to stop, then stop.

    SIGTERM sets the stop event rather than killing the loop, and `run_forever`
    returns within a poll interval whatever a handler happens to be doing --
    the handler runs on a thread of its own, so the signal is seen at once and
    the loop is not waiting on it (SPEC.md §13, D7). An attempt still in flight
    is left to its lease: a reaper requeues it and an idempotent handler runs it
    again (I8), which is the same trade N1 already makes for a worker that dies
    outright. Bounding a rolling deploy at a poll interval is worth one
    re-executed attempt; bounding it at a task's wall-clock budget was not.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError):  # not every platform has add_signal_handler
            loop.add_signal_handler(sig, stop.set)
    await worker.run_forever(stop)


def main() -> None:
    """Start a worker for every handler this process has.

    `Worker` snapshots the registry when it is constructed, so the packages
    whose handlers this process should execute must be imported before that --
    hence the side-effecting import above, and the log line that makes the
    resulting claim list visible rather than something to infer from silence.

    The gateway config is validated here, before any task can be claimed: if a
    production alias resolves anywhere but an approved self-hosted endpoint the
    exception propagates and the process does not start (I15). No config named
    means no gateway at all, which is how M0/M1 run credential-free -- not a
    degraded mode, since nothing in this process can call a model without one.
    """
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is not set")
    gateway = gateway_config_from_env()
    log = logging.getLogger(__name__)
    log.info(
        "gateway: %s",
        f"{len(gateway.bindings)} models from {gateway.source} ({gateway.mode.value})"
        if gateway
        else "none configured; this worker cannot call a model",
    )
    worker_id = os.environ.get(WORKER_ID_ENV, "").strip() or default_worker_id()
    log.info("worker %s claims %s", worker_id, ", ".join(registered_types()))
    asyncio.run(run(Worker(dsn, worker_id, tracer=tracer_from_env())))


if __name__ == "__main__":
    main()
