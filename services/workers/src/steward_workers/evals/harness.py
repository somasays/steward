"""The part of B2 that reaches a model and a database (issue #50).

Kept apart from `scoring` so that scoring stays pure and testable without
either, and apart from `classification` so the suite's *rules* can be read
without the plumbing that executes them.

The classifier built here is the real `AgentColumnClassifier` on the real
transport: an eval that stubbed it would measure the fixture, not the product.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pgserver
from steward_catalog import ClassificationRequest, ClassificationRun, ClassifierFailed
from steward_llm import GatewayConfig, proxy_config_from_env
from steward_llm.proxy import LiteLLMProxyTransport
from steward_queue import SYSTEM_ACTOR, connect, create_run, enqueue, upgrade_to_head
from steward_schemas import ClassificationProposal, RunBudget, TableProfile, TaskSpec
from steward_telemetry import NoopTracer

from steward_workers.classifier import AgentColumnClassifier
from steward_workers.evals.classification import (
    EvaluationInfrastructureError,
    EvaluationResult,
)

__all__ = ["classify_once"]

RETRYABLE = (httpx.TransportError, TimeoutError, ConnectionError)
"""Failure *types* that mean "no working model was reached".

`httpx.TransportError` covers connect, read, write and pool timeouts and every
connection failure beneath them. Matching on types rather than on message text is
the whole point: it is the difference between a rule and a coincidence.

**A completed 5xx response is deliberately *not* retryable.** `LiteLLMProxyTransport`
turns any status >= 400 into `CompletionFailed` (`proxy.py`), which is a
`steward-llm` type and not an `httpx.TransportError` — so a 502 or 503 from the
proxy is classified as a result and fails the run immediately. That is the
conservative direction and it is chosen, not overlooked: the alternative is
reading the status back out of a rendered message, which is the message
inspection this boundary exists to remove. Making it retryable needs typed status
metadata on the failure — a status field on `CompletionFailed` — at which point
`RETRYABLE` gains a predicate rather than a substring.

Verified against the real proxy: a refused connection surfaces `ConnectError` and
a proxy killed mid-stream surfaces `RemoteProtocolError`, both retryable.

The chain is walked because the seam wraps them — `AgentColumnClassifier`
converts a transport failure into `ClassifierFailed` so that `steward-catalog`
never sees a `steward-agents` type (I4), and `raise ... from exc` keeps the
original reachable on `__cause__`. Walking to find a typed cause is honest;
grepping the rendered message is not.
"""

EVAL_BUDGET = RunBudget(
    steps=6,
    tokens=120_000,
    cost_usd=Decimal("0.500000"),
    wall_clock=timedelta(minutes=10),
)
"""`classify_asset`'s own budget. The eval must run under the cap production
runs under, or it measures a classifier nobody deploys."""

CLAIM_TASK = "UPDATE tasks SET state = 'running', claimed_by = %(who)s, attempts = 1 WHERE id = %(id)s"


def classify_once(gateway: GatewayConfig, profile: TableProfile) -> ClassificationProposal:
    """Classify one profile through the real runtime, once.

    An ephemeral Postgres per call, because the bounded runtime checkpoints
    durably and fences those writes against a claimed task row (D7). Standing one
    up is the price of running the *real* path rather than a version of it with
    the durability removed — and it is what makes the eval behave the same on a
    laptop and in CI.
    """
    proxy = proxy_config_from_env(os.environ)
    if proxy is None:
        # Belt and braces. `_require_gateway` now refuses a missing proxy up
        # front, which is where the tri-state can still be honoured; reaching
        # here means someone called this directly, so it stays a hard failure
        # rather than a silent None. The pragma that used to sit on this line
        # claimed "the caller checks first" before any caller did, so a real
        # configuration escaped as a traceback and the line was never covered.
        raise ClassifierFailed("no proxy configured")

    with tempfile.TemporaryDirectory(prefix="steward-b2") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")  # type: ignore[attr-defined]  # pgserver ships no py.typed
        try:
            dsn = server.get_uri()
            upgrade_to_head(dsn)
            transport = LiteLLMProxyTransport(proxy)
            classifier = AgentColumnClassifier(
                dsn=dsn, gateway=gateway, transport=transport, tracer=NoopTracer()
            )
            run = _claimed_task(dsn)
            asset_id = uuid4()
            request = ClassificationRequest(
                asset_id=asset_id, profile_version=1, profile=profile
            )
            try:
                proposed = asyncio.run(classifier.classify(run, request))
            except ClassifierFailed as exc:
                raise _classify_failure(exc) from exc
            # The classifier returns columns and the provenance only it knows;
            # the *handler* is what turns that into a proposal about a specific
            # asset and profile version, and the scorer needs that shape. Built
            # the same way here rather than scoring a different type.
            return ClassificationProposal(
                asset_id=asset_id,
                profile_version=1,
                prompt_version=proposed.prompt_version,
                model_alias=proposed.model_alias,
                columns=proposed.columns,
            )
        finally:
            server.cleanup()


def _classify_failure(exc: ClassifierFailed) -> Exception:
    """Decide whether a failed classification is the network's or the model's.

    Infrastructure only when a retryable *type* is found in the cause chain.
    Anything else — a refusal, unparseable output, a citation that resolves to
    nothing, a budget exhausted — is a completed run with an unusable answer and
    must not be retried.
    """
    seen: Exception | BaseException | None = exc
    while seen is not None:
        if isinstance(seen, RETRYABLE):
            return EvaluationInfrastructureError(f"{type(seen).__name__}: {seen}")
        seen = seen.__cause__ or seen.__context__
    return EvaluationResult(str(exc))


def _claimed_task(dsn: str) -> ClassificationRun:
    """A real run and a claimed task, so checkpoint writes have a row to fence
    against. Without one the store raises `StaleClaim` and masks whatever the
    model actually did (#85)."""
    with connect(dsn) as conn:
        created = create_run(conn, goal="classify_asset", budget=EVAL_BUDGET, actor=SYSTEM_ACTOR)
        spec = TaskSpec(
            task_id=uuid4(),
            run_id=created.id,
            task_type="classify_asset",
            payload={},
            budget=EVAL_BUDGET,
            max_attempts=1,
        )
        enqueue(conn, spec, actor=SYSTEM_ACTOR)
        conn.execute(CLAIM_TASK, {"who": "b2-eval", "id": spec.task_id})
        conn.commit()
    return ClassificationRun(
        run_id=created.id,
        task_id=spec.task_id,
        trace_id="0" * 32,
        claimed_by="b2-eval",
        attempts=1,
        budget=EVAL_BUDGET,
    )
