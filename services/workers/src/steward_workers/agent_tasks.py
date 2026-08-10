"""Where the agent runtime meets the queue.

This module exists because neither package may import the other. `steward-agents`
is forbidden `steward-queue` by I4's layering — the runtime must not learn what a
task is — and the queue must not learn what an agent is. The join is wiring, so
it lives in the service, which is allowed to know about both.

Three seams are joined here, and each one is the answer to a specific failure:

* **Checkpoints become durable.** The runtime writes through a `CheckpointStore`
  protocol; here that protocol is satisfied by the `checkpoints` table on the
  handler's own connection, so a step's state commits with the step and a worker
  that dies costs one step rather than a run (N1).
* **Spend reaches the run.** `on_spend` is wired to `ctx.usage.debit`, so an
  attempt that raises or is killed at its cap still charges what it used —
  neither of those paths produces a `TaskResult` to report on (SPEC §13 D12).
* **A refused step is a typed failure, not a crash.** `BudgetExceeded` is caught
  and returned as a `budget_exceeded` `TaskResult`. Raised bare it would reach
  the queue as `handler raised`, and H4's verdict would name the wrong thing.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from steward_agents import (
    AgentCheckpoint,
    AgentRuntime,
    AgentRuntimeError,
    BudgetExceeded,
    ModelReservation,
    ToolRegistry,
    TraceContext,
)
from steward_llm import GatewayConfig, GatewayTransport, LLMClient, Message, Role
from steward_queue import (
    TaskContext,
    connect,
    guard_claim,
    latest_checkpoint,
    record_step_usage,
    task_handler,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_queue.registry import TaskHandler
from steward_schemas import AgentSpec, ProblemDetails, RunBudget, TaskResult, TaskStatus
from steward_telemetry import Tracer

NOTHING_SPENT = RunBudget(steps=0, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta())
"""What an agent task reports on its result: its spend is already recorded."""

AGENT_ECHO = "agent.echo"
"""The task type of the end-to-end proof.

Not a product agent: #50 brings the Classifier, and #51 the Documentarian. This
one exists so the path API -> queue -> worker -> runtime -> gateway is exercised
by something whose correctness is decidable without a model quality judgement.
"""

CHECKPOINT_STEP = 0
"""One row per task rather than one per step.

The runtime's checkpoint is already cumulative -- it carries the whole message
history and the running usage -- so a row per step would store N copies of a
growing prefix to recover the same state. The upsert on `(task_id, step)` makes
this a single moving row, which is also what makes resume read the furthest
committed state without a `max(step)` scan.
"""


class EchoRequest(BaseModel):
    """What the proof agent's one tool takes."""

    value: str


class EchoResponse(BaseModel):
    value: str


class EchoResult(BaseModel):
    """What the proof agent must submit to finish."""

    answer: str


async def _echo(request: BaseModel) -> BaseModel:
    parsed = EchoRequest.model_validate(request)
    return EchoResponse(value=parsed.value)


def echo_registry() -> ToolRegistry:
    """The proof agent's least-privilege toolset: one tool, and nothing else."""
    tools = ToolRegistry()
    tools.register(
        name="echo",
        description="Return the value it is given, unchanged.",
        input_model=EchoRequest,
        output_model=EchoResponse,
        handler=_echo,
        wall_clock=timedelta(seconds=5),
    )
    return tools


class DurableCheckpointStore:
    """`CheckpointStore` over the queue's `checkpoints` table, on a connection
    of its own.

    Its own, and that is the whole design decision. The obvious implementation
    writes through `ctx.connection` so a checkpoint commits with the step that
    produced it -- but the worker *rolls that transaction back* when an attempt
    fails, which is exactly when a checkpoint would be needed. Resume across a
    failure and per-step durability are therefore incompatible with a single
    transaction, and the queue's one-transaction rule is the one that has to
    give, because the alternative is a checkpoint that only ever survives runs
    that did not need it (SPEC §13 D13).

    What that costs, precisely: a checkpoint can outlive an attempt whose other
    writes were discarded. So the state here is a *replayable hint*, not a
    record of committed side effects -- which is the same thing the registry's
    idempotence clause already asks of a handler, since at-least-once execution
    means any step may run twice. A resumed run re-executes at most the step
    that was in flight, and may debit it twice; the conservative direction, and
    the same trade summed wall-clock takes (D9).

    Satisfies the protocol structurally; `steward-agents` never learns it exists.
    """

    def __init__(self, ctx: TaskContext, dsn: str) -> None:
        self._task_id = ctx.spec.task_id
        self._run_id = ctx.spec.run_id
        self._claimed_by = ctx.claimed_by
        self._attempts = ctx.attempts
        self._dsn = dsn
        self._conn: QueueConnection | None = None
        self._charged = NOTHING_SPENT
        """What this attempt has already been billed for, so a save charges the
        delta rather than the running total."""

    def _connection(self) -> QueueConnection:
        if self._conn is None:
            self._conn = connect(self._dsn)
        return self._conn

    def close(self) -> None:
        """Release the connection. Called by the handler however the run ends."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def load(self, key: str) -> AgentCheckpoint | None:
        state = latest_checkpoint(self._connection(), self._task_id)
        if state is None:
            return None
        return AgentCheckpoint.model_validate(state)

    async def save(self, key: str, checkpoint: AgentCheckpoint) -> None:
        """Persist this step, and charge what it cost, in one transaction.

        The two commit together because they are one fact. A checkpoint that
        outlived its charge is the crash case with the accounting removed: the
        worker died, so no `TaskResult` and no in-memory ledger survived to
        record anything, and a resumed attempt would read three model calls of
        progress and a cap that looked untouched -- then spend it again.
        """
        payload: dict[str, Any] = json.loads(checkpoint.model_dump_json())
        conn = self._connection()
        # Fenced, because this connection is not the worker's and nothing else
        # stops it. A handler whose lease expired mid-step is still running: its
        # task may already have been reaped and re-claimed, and without this it
        # would overwrite the live attempt's checkpoint and charge that run for
        # work nobody is supervising (N1, D7).
        guard_claim(conn, self._task_id, claimed_by=self._claimed_by, attempts=self._attempts)
        write_checkpoint(conn, self._task_id, step=CHECKPOINT_STEP, state=payload)
        record_step_usage(
            conn,
            run_id=self._run_id,
            task_id=self._task_id,
            amount=checkpoint.usage.remaining(self._charged),
        )
        conn.commit()
        self._charged = checkpoint.usage


def _failed(
    ctx: TaskContext, kind: str, title: str, status: int, exc: Exception
) -> TaskResult:
    """A typed failure carrying what this attempt spent before it stopped."""
    return TaskResult(
        task_id=ctx.spec.task_id,
        status=TaskStatus.FAILED,
        usage=NOTHING_SPENT,
        error=ProblemDetails(type=kind, title=title, status=status, detail=str(exc)),
    )


def build_agent_echo(
    *,
    dsn: str,
    gateway: GatewayConfig,
    transport: GatewayTransport,
    tracer: Tracer,
    reservation: ModelReservation | None = None,
) -> TaskHandler:
    """Build the `agent.echo` handler against a validated gateway.

    Takes the validated `GatewayConfig` rather than reading the environment: the
    composition root has already refused to boot if it resolves off the
    allowlist, and a handler that re-read the environment could reach a gateway
    that refusal never saw (I15).
    """
    # The per-call worst case, and it has to be small enough that a run of
    # `AGENT_ECHO_BUDGET`'s size can afford `steps` of them -- a reservation
    # larger than the cap refuses the first call and the agent never runs.
    limits = reservation or ModelReservation(
        tokens=2000, cost_usd=Decimal("0.02"), wall_clock=timedelta(seconds=30)
    )

    async def agent_echo(ctx: TaskContext) -> TaskResult:
        checkpoints = DurableCheckpointStore(ctx, dsn)
        runtime = AgentRuntime(
            client=LLMClient(gateway, transport),
            tools=echo_registry(),
            checkpoints=checkpoints,
            reservation=limits,
            # Not `ctx.usage.debit`: this agent's spend is charged where it
            # becomes durable, inside the checkpoint transaction, so the ledger
            # the worker reads on the failure paths must stay empty or the same
            # tokens would be billed twice.
            tracer=tracer,
        )
        prompt = str(ctx.spec.payload.get("prompt", "echo the value 'steward'"))
        try:
            result = await runtime.run(
                key=str(ctx.spec.task_id),
                spec=agent_spec(ctx.spec.budget),
                prompt_version=PROMPT_VERSION,
                messages=(Message(role=Role.USER, content=prompt),),
                output_model=EchoResult,
                trace=TraceContext(trace_id=ctx.trace_id, task_id=ctx.spec.task_id),
            )
        except BudgetExceeded as exc:
            # A refused step is the budget working, so it is reported as one.
            # Raised bare, the queue would title it `handler raised` and an
            # operator reading the trail would look for a bug instead of a cap.
            return _failed(ctx, "urn:steward:budget-exceeded", "budget_exceeded", 422, exc)
        except AgentRuntimeError as exc:
            return _failed(ctx, "urn:steward:agent-failed", "agent_failed", 500, exc)
        finally:
            # However the run ended, this connection is not the worker's to
            # reclaim -- an abandoned handler thread would otherwise leave it
            # open until the process exits.
            checkpoints.close()
        return TaskResult(
            task_id=ctx.spec.task_id,
            status=TaskStatus.SUCCEEDED,
            # This attempt's spend, not the checkpoint's. `AgentResult.usage` is
            # cumulative across attempts so the loop can bound the whole task
            # against one cap -- reporting it here would charge the run again
            # for everything the failed attempts were already charged for
            # (`steward_queue.usage`, SPEC §13 D12).
            usage=NOTHING_SPENT,
            output=result.output.model_dump(),
        )

    return agent_echo


PROMPT_VERSION = "agent.echo@v1"
"""Carried on every generation span (I7). Bumped when the prompt changes, which
is what makes a trace answer "which prompt produced this" rather than "which
code did"."""


def agent_spec(limits: RunBudget) -> AgentSpec:
    """The proof agent's declaration, drawing its caps from the task's own.

    The task's budget *is* the agent's: the plan reserved it out of the run's
    (D9), so a second, independently declared cap here would be a number nobody
    reserved and the two would drift.
    """
    return AgentSpec(
        name="echo-agent", model_alias="steward-fast", tools=("echo",), limits=limits
    )


def register(
    *, dsn: str, gateway: GatewayConfig, transport: GatewayTransport, tracer: Tracer
) -> None:
    """Register `agent.echo` for this process.

    A function rather than an import-time decorator because the handler needs a
    validated gateway and a tracer, and both are the composition root's to
    supply -- registering at import would mean deciding them at import (I15).
    """
    task_handler(
        AGENT_ECHO,
        sample_payload={"prompt": "echo the value 'steward'"},
    )(build_agent_echo(dsn=dsn, gateway=gateway, transport=transport, tracer=tracer))
