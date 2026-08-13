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
from collections.abc import Callable, Mapping

import steward_catalog  # noqa: F401 -- imported for its side effect: registers `scan_source`
from steward_catalog import (
    CLASSIFY_ASSET_TASK_TYPE,
    classifier_bound,
    provide_classifier,
)
from steward_llm import (
    PROXY_KEY_ENV,
    PROXY_URL_ENV,
    GatewayConfig,
    GatewayTransport,
    LiteLLMProxyTransport,
    StubGateway,
    gateway_config_from_env,
    proxy_config_from_env,
)
from steward_queue import DSN_ENV, Worker, registered_types
from steward_telemetry import Tracer, tracer_from_env

from steward_workers import agent_tasks
from steward_workers.classifier import AgentColumnClassifier

WORKER_ID_ENV = "STEWARD_WORKER_ID"

CAPABILITIES: Mapping[str, Callable[[], bool]] = {
    CLASSIFY_ASSET_TASK_TYPE: classifier_bound,
}
"""Registered task types this process may only claim if it can actually do them.

Most handlers are executable by any process that imported their package. A few
need a *capability* the package cannot supply -- `classify_asset` needs a model,
which `steward-catalog` may not reach (I4) and only a composition root may
configure (I15) -- and those are registered systemwide but claimable per process.

The map is keyed on the pathology rather than listing the safe cases: a task
type absent from it is claimable, and a type in it is claimable exactly when its
predicate says the capability is present. Adding a second agent-backed handler
means adding a row, not remembering to edit a filter.
"""


def claimable_types() -> tuple[str, ...]:
    """The task types this process may claim.

    Not `registered_types()`, which is every handler the registry holds. A
    worker with no classifier bound would otherwise claim `classify_asset`,
    fail it, and keep failing it on every retry -- a task nobody can execute
    sitting in the queue is better than a worker that takes it in order to
    refuse it, because the first is visible as a backlog and the second looks
    like a broken classifier.
    """
    return tuple(name for name in registered_types() if CAPABILITIES.get(name, _always)())


def _always() -> bool:
    return True

AGENT_TRANSPORT_ENV = "STEWARD_AGENT_TRANSPORT"
"""How this worker reaches models: `proxy` for production, `stub` for a fixture.

Opt-in by name, exactly as development mode is. Defaulting to the stub would
give a production worker a handler that answers from a fixture -- green that
means nothing -- and defaulting to the proxy would have a credential-less worker
fail at the first model call instead of at boot. So a worker that says nothing
registers no agent handlers, and says so in its log line."""


def register_agent_tasks(dsn: str, gateway: GatewayConfig | None, tracer: Tracer) -> str:
    """Register the agent-backed task types this process can honestly execute.

    Returns what it did, for the log line: an operator should be able to see
    that a worker has no agent handlers rather than infer it from a task that
    is never claimed.
    """
    choice = os.environ.get(AGENT_TRANSPORT_ENV, "").strip().lower()
    if not choice:
        return "none registered (no transport configured)"
    if choice not in {"proxy", "stub"}:
        raise SystemExit(f"{AGENT_TRANSPORT_ENV}={choice!r} is not a transport ('proxy' or 'stub')")
    if gateway is None:
        raise SystemExit(
            f"{AGENT_TRANSPORT_ENV} is set but no gateway config is; "
            "an agent handler with no validated gateway cannot exist (I15)"
        )
    transport: GatewayTransport
    if choice == "proxy":
        proxy = proxy_config_from_env(os.environ)
        if proxy is None:
            raise SystemExit(
                f"{AGENT_TRANSPORT_ENV}=proxy needs {PROXY_URL_ENV} and {PROXY_KEY_ENV}; "
                "a worker that cannot reach the gateway should not start pretending it can"
            )
        transport = LiteLLMProxyTransport(proxy)
        where = f"the gateway at {proxy.base_url}"
    else:
        transport = StubGateway({})
        where = "the stub transport"
    agent_tasks.register(dsn=dsn, gateway=gateway, transport=transport, tracer=tracer)
    # The Classifier's handler is already registered -- `steward_catalog`
    # registered it at import, which is what lets `classify_asset` be a goal the
    # shipped registry carries (SPEC.md §13 D15). What this process supplies is
    # the capability behind it, and until it does, `claimable_types()` leaves the
    # task type off this worker's claim list.
    provide_classifier(
        AgentColumnClassifier(dsn=dsn, gateway=gateway, transport=transport, tracer=tracer)
    )
    return f"{agent_tasks.AGENT_ECHO}, {CLASSIFY_ASSET_TASK_TYPE} on {where}"

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

    The gateway config is validated first, before this process reads anything
    else about its environment: if a production alias resolves anywhere but an
    approved self-hosted endpoint the exception propagates and the process does
    not start (I15). No config named means no gateway at all, which is how M0/M1
    run credential-free -- not a degraded mode, since nothing in this process can
    call a model without one.

    First rather than after the DSN check because the refusal is the one outcome
    that must not depend on the rest of the environment being right -- and
    because that ordering is what the H12 harness reads: an entry point booted
    against an off-allowlist config must refuse for *that* reason, not exit
    earlier over an unset DSN and look like it refused.
    """
    gateway = gateway_config_from_env()
    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is not set")
    log = logging.getLogger(__name__)
    log.info(
        "gateway: %s",
        f"{len(gateway.bindings)} models from {gateway.source} ({gateway.mode.value})"
        if gateway
        else "none configured; this worker cannot call a model",
    )
    tracer = tracer_from_env()
    model_consuming = bool(os.environ.get(AGENT_TRANSPORT_ENV, "").strip())
    log.info("agent tasks: %s", register_agent_tasks(dsn, gateway, tracer))
    if model_consuming and not classifier_bound():
        # Asserting the effect of the call above rather than trusting its
        # intent. A worker asked for a transport is a worker meant to run the
        # Classifier; if registration silently stopped binding one, this process
        # would come up, claim everything except `classify_asset`, and look
        # perfectly healthy while the product capability it was started for was
        # simply absent.
        raise SystemExit(
            f"{AGENT_TRANSPORT_ENV} is set but no classifier was bound; "
            "this worker was started to run the Classifier and cannot"
        )
    worker_id = os.environ.get(WORKER_ID_ENV, "").strip() or default_worker_id()
    claims = claimable_types()
    log.info("worker %s claims %s", worker_id, ", ".join(claims))
    asyncio.run(run(Worker(dsn, worker_id, task_types=claims, tracer=tracer)))


if __name__ == "__main__":
    main()
