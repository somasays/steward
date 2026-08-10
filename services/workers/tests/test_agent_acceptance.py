"""#69's end-to-end proof: a bounded agent, executed by a real worker.

What each of these is for, since "end to end" can mean almost nothing:

* **The path is the real one.** A run is planned through the goal registry, its
  task is claimed by a `Worker` off a real Postgres queue, and the handler is the
  registered one — not the runtime called directly with hand-built arguments.
  Only the gateway is a stub, which is the point: the model's *answers* are
  fixed so the run's correctness is decidable, while everything carrying them is
  production code.
* **Resume is asserted across a kill, not simulated.** The first worker is
  stopped mid-run by a budget its second step cannot fit; the second reads the
  committed checkpoint. A test that called `run()` twice against an in-memory
  store would prove the store, not the durability.
* **H4's verdict has to name the budget.** A refused step must arrive as
  `budget_exceeded`, not as `handler raised` — which is what it would be if the
  handler let `BudgetExceeded` travel.
* **H6 fails on an empty trace.** A span-tree assertion over no spans passes
  trivially, so the tree is asserted to be non-empty *and* to contain each kind.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_agents import SUBMIT_RESULT, ModelReservation
from steward_llm import (
    DeploymentMode,
    EndpointAllowlist,
    GatewayConfig,
    ModelBinding,
    StubGateway,
    StubReply,
    ToolCall,
)
from steward_orchestration import GoalParams, PlannedTask, goal, plan_run
from steward_orchestration.registry import REGISTRY as GOAL_REGISTRY
from steward_queue import (
    REGISTRY,
    TaskState,
    Worker,
    create_run,
    enqueue,
    get_run,
    get_task,
    latest_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_schemas import RunBudget, TaskSpec
from steward_telemetry import Span, SpanOutcome
from steward_workers.agent_tasks import AGENT_ECHO, build_agent_echo

pytestmark = pytest.mark.acceptance

ENDPOINT = "http://127.0.0.1:8000/v1"

AGENT_ECHO_GOAL = "agent_echo"
AGENT_ECHO_TASK_TYPE = AGENT_ECHO

# What a proof run may spend (I12). `steps` is 6 rather than the 3 a clean run
# needs (model, tool, submit) because a step is spent whether or not it
# succeeded: a cap of exactly 3 is a run that cannot survive one lost
# connection, which would make the resume assertion below untestable for a
# reason that has nothing to do with resume.
PROOF_BUDGET = RunBudget(
    steps=6, tokens=8000, cost_usd=Decimal("0.100000"), wall_clock=timedelta(minutes=2)
)


class EchoParams(GoalParams):
    prompt: str = "echo the value 'steward'"


@pytest.fixture(autouse=True)
def proof_goal() -> Iterator[None]:
    """Register `agent_echo` for these tests only, then take it back out.

    It lives here rather than in `steward_orchestration.goals` on purpose. A
    goal in the shipped registry that no shipped package can execute is a run a
    client can create and nothing will ever claim -- which is what the
    orchestration suite's goal/handler seam check exists to catch, and it caught
    exactly that. The product's first agent goal arrives with the Classifier
    (#50); this one is scaffolding for the proof, so it is registered where the
    proof is.
    """

    @goal(
        AGENT_ECHO_GOAL,
        params_model=EchoParams,
        allowed_task_types=[AGENT_ECHO_TASK_TYPE],
        budget=PROOF_BUDGET,
        sample_payload={"prompt": "echo the value 'steward'"},
    )
    def plan_echo(params: EchoParams) -> tuple[PlannedTask, ...]:
        return (
            PlannedTask(
                task_type=AGENT_ECHO_TASK_TYPE,
                budget=PROOF_BUDGET,
                payload={"prompt": params.prompt},
            ),
        )

    yield
    GOAL_REGISTRY.pop(AGENT_ECHO_GOAL, None)


@dataclass
class RecordedSpan:
    kind: str
    attributes: dict[str, str]
    outcome: SpanOutcome | None = None

    measurements: dict[str, object] = field(default_factory=dict)

    def record(self, outcome: SpanOutcome, detail: str | None = None) -> None:
        self.outcome = outcome

    def observe(self, measurements: Mapping[str, object]) -> None:
        self.measurements.update(measurements)


@dataclass
class RecordingTracer:
    """A `Tracer` that keeps its spans. Spans are the observable output of
    tracing, so H6's assertions are about emitted events (GUARDRAILS §1)."""

    spans: list[RecordedSpan] = field(default_factory=list)

    @contextmanager
    def run_span(self, *, trace_id: str, run_id: UUID, goal: str) -> Iterator[Span]:
        span = RecordedSpan("run", {"trace_id": trace_id, "goal": goal})
        self.spans.append(span)
        yield span

    @contextmanager
    def task_span(
        self, *, trace_id: str, run_id: UUID, task_id: UUID, task_type: str
    ) -> Iterator[Span]:
        span = RecordedSpan(
            "task", {"trace_id": trace_id, "task_id": str(task_id), "task_type": task_type}
        )
        self.spans.append(span)
        yield span

    @contextmanager
    def generation_span(
        self, *, trace_id: str, task_id: UUID, model_alias: str, prompt_version: str
    ) -> Iterator[Span]:
        span = RecordedSpan(
            "generation",
            {
                "trace_id": trace_id,
                "task_id": str(task_id),
                "model_alias": model_alias,
                "prompt_version": prompt_version,
            },
        )
        self.spans.append(span)
        yield span

    @contextmanager
    def tool_span(self, *, trace_id: str, task_id: UUID, tool_name: str) -> Iterator[Span]:
        span = RecordedSpan(
            "tool", {"trace_id": trace_id, "task_id": str(task_id), "tool_name": tool_name}
        )
        self.spans.append(span)
        yield span

    def of(self, kind: str) -> list[RecordedSpan]:
        return [span for span in self.spans if span.kind == kind]


def gateway() -> GatewayConfig:
    return GatewayConfig(
        mode=DeploymentMode.DEVELOPMENT,
        source="acceptance",
        bindings=(ModelBinding(alias="steward-fast", model="openai/local", api_base=ENDPOINT),),
        allowlist=EndpointAllowlist.from_urls((ENDPOINT,)),
    )


def uses_the_tool_then_submits() -> list[StubReply]:
    """The shortest run that exercises a tool: call it, then submit."""
    return [
        StubReply.completed(
            "",
            prompt_tokens=12,
            completion_tokens=6,
            cost_usd=Decimal("0.001"),
            tool_calls=(ToolCall(id="c1", name="echo", arguments='{"value":"steward"}'),),
        ),
        StubReply.completed(
            "",
            prompt_tokens=20,
            completion_tokens=8,
            cost_usd=Decimal("0.002"),
            tool_calls=(
                ToolCall(id="s1", name=SUBMIT_RESULT, arguments='{"answer":"steward"}'),
            ),
        ),
    ]


def register_agent(
    dsn: str,
    replies: list[StubReply],
    tracer: RecordingTracer,
    reservation: ModelReservation | None = None,
) -> StubGateway:
    """Register `agent.echo` for this test, replacing any earlier registration."""
    stub = StubGateway({"steward-fast": replies})
    REGISTRY.pop(AGENT_ECHO, None)
    from steward_queue import task_handler

    task_handler(AGENT_ECHO, sample_payload={"prompt": "echo the value 'steward'"})(
        build_agent_echo(
            dsn=dsn, gateway=gateway(), transport=stub, tracer=tracer, reservation=reservation
        )
    )
    return stub


def planned(
    conn: QueueConnection,
    *,
    prompt: str = "echo the value 'steward'",
    max_attempts: int = 1,
) -> TaskSpec:
    """Plan an `agent_echo` run the way the API does, and enqueue its task.

    Through `GOALS` rather than by hand: the plan-time budget reservation (D9)
    is part of what is being proven, and a hand-built `TaskSpec` would skip it.
    """
    expansion = plan_run(AGENT_ECHO_GOAL, {"prompt": prompt})
    record = create_run(conn, goal=AGENT_ECHO_GOAL, budget=expansion.budget, payload={})
    planned_task = expansion.tasks[0]
    spec = TaskSpec(
        task_id=uuid4(),
        run_id=record.id,
        task_type=planned_task.task_type,
        payload=planned_task.payload,
        budget=planned_task.budget,
        max_attempts=max_attempts,
    )
    enqueue(conn, spec)
    conn.commit()
    return spec


class TestAgentEndToEnd:
    async def test_a_stub_backed_agent_runs_through_the_whole_path(
        self, dsn: str, conn: QueueConnection
    ) -> None:
        tracer = RecordingTracer()
        stub = register_agent(dsn, uses_the_tool_then_submits(), tracer)
        spec = planned(conn)

        assert await Worker(dsn, "w1", tracer=tracer).run_once() == 1

        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED
        # The typed result reached the queue, through the tool the model called.
        result = conn.execute(
            "SELECT result FROM tasks WHERE id = %s", (spec.task_id,)
        ).fetchone()
        assert result is not None
        assert result[0]["output"] == {"answer": "steward"}
        assert len(stub.calls) == 2

        run = get_run(conn, spec.run_id)
        assert run is not None
        assert run.usage.tokens == 46  # 18 + 28, both calls
        assert run.usage.over(run.budget) == ()

    async def test_the_trace_carries_the_generation_and_tool_spans(
        self, dsn: str, conn: QueueConnection
    ) -> None:
        """H6. Asserted non-empty first: a span-tree assertion over no spans
        agrees with itself forever."""
        tracer = RecordingTracer()
        register_agent(dsn, uses_the_tool_then_submits(), tracer)
        spec = planned(conn)

        await Worker(dsn, "w1", tracer=tracer).run_once()

        assert tracer.spans, "no spans at all — this assertion would prove nothing"
        assert len(tracer.of("task")) == 1
        assert len(tracer.of("generation")) == 2
        assert len(tracer.of("tool")) == 1
        # Every generation carries the prompt version (I7), and every span in
        # the tree is on the run's trace.
        assert all(span.attributes["prompt_version"] for span in tracer.of("generation"))
        assert all(span.attributes["model_alias"] == "steward-fast" for span in tracer.of("generation"))
        assert tracer.of("tool")[0].attributes["tool_name"] == "echo"

        # Identity is not the contract #69 states -- latency, tokens, cost and
        # validated I/O are. Asserted as *values*, so a span that carried the
        # field names and nothing in them fails here.
        first, second = tracer.of("generation")
        assert first.measurements["total_tokens"] == 18
        assert second.measurements["total_tokens"] == 28
        assert first.measurements["cost_usd"] == "0.001"
        assert float(first.measurements["latency_seconds"]) > 0  # type: ignore[arg-type]
        assert first.measurements["input"] == ["echo the value 'steward'"]
        assert first.measurements["tool_calls"] == ["echo"]
        tool_span = tracer.of("tool")[0]
        assert tool_span.measurements["input"] == '{"value":"steward"}'
        assert json.loads(str(tool_span.measurements["output"])) == {"value": "steward"}
        run = get_run(conn, spec.run_id)
        assert run is not None
        assert {span.attributes["trace_id"] for span in tracer.spans} == {run.trace_id}

    async def test_an_impossible_budget_terminates_as_budget_exceeded(
        self, dsn: str, conn: QueueConnection
    ) -> None:
        """H4's impossible-goal scenario, and the seam that decides whether its
        verdict is honest: the handler must return `budget_exceeded`, not let
        `BudgetExceeded` travel and be titled `handler raised`."""
        tracer = RecordingTracer()
        stub = register_agent(dsn, uses_the_tool_then_submits(), tracer)
        expansion = plan_run(AGENT_ECHO_GOAL, {"prompt": "go"})
        record = create_run(conn, goal=AGENT_ECHO_GOAL, budget=expansion.budget, payload={})
        # One step and a cent: the first model call cannot fit.
        impossible = RunBudget(
            steps=1, tokens=10, cost_usd=Decimal("0.01"), wall_clock=timedelta(seconds=30)
        )
        spec = TaskSpec(
            task_id=uuid4(),
            run_id=record.id,
            task_type=AGENT_ECHO_TASK_TYPE,
            payload={"prompt": "go"},
            budget=impossible,
            max_attempts=1,
        )
        enqueue(conn, spec)
        conn.commit()

        await Worker(dsn, "w1", tracer=tracer).run_once()

        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.DEAD
        recorded = conn.execute(
            "SELECT last_error FROM tasks WHERE id = %s", (spec.task_id,)
        ).fetchone()
        assert recorded is not None
        assert recorded[0]["title"] == "budget_exceeded"
        # Never started, so nothing was spent on it.
        assert stub.calls == []
        run = get_run(conn, record.id)
        assert run is not None
        assert run.usage.over(run.budget) == ()

    async def test_a_killed_worker_resumes_from_the_committed_checkpoint(
        self, dsn: str, conn: QueueConnection
    ) -> None:
        """N1: a worker dying mid-run costs at most one step.

        The first attempt dies on its second model call, after the tool step has
        been checkpointed. The second attempt reads that checkpoint and needs
        one model call to finish, not three -- if it re-ran the tool step the
        stub would be asked for two completions and run out.
        """
        tracer = RecordingTracer()
        replies = uses_the_tool_then_submits()
        first = register_agent(
            dsn,
            [
                replies[0],
                # The connection dies after the model has generated: the shape a
                # real mid-run failure takes, and the one that leaves spend
                # behind (D11).
                StubReply.streaming(
                    ("partial",),
                    prompt_tokens=9,
                    cost_per_token=Decimal("0.0001"),
                    fails_with=ConnectionResetError("gateway went away"),
                ),
            ],
            tracer,
        )
        spec = planned(conn, max_attempts=2)

        # No backoff: the retry this proves is the scheduling, not the waiting.
        await Worker(dsn, "w1", tracer=tracer, retry_base_delay=timedelta(0)).run_once()
        # Failed, and *scheduled for retry* by the production path -- not dead,
        # and not resurrected by this test. Retry admission projects what one
        # more attempt at this task can still cost (`budget - used`), so a task
        # that has spent part of its cap is still affordable; projecting the
        # whole budget again is what used to dead-letter it here (#69 review).
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.PENDING
        assert task.attempts == 1
        assert len(first.calls) == 2

        saved = latest_checkpoint(conn, spec.task_id)
        assert saved is not None, "nothing was checkpointed, so nothing can resume"
        assert saved["messages"], "an empty checkpoint would resume from the start"
        # The tool ran and its result is in the saved history: that is the step
        # the resumed attempt must not pay for again.
        assert any(message["role"] == "tool" for message in saved["messages"])
        charged_for_the_failure = get_run(conn, spec.run_id)
        assert charged_for_the_failure is not None
        failed_attempt_spend = charged_for_the_failure.usage

        # A second worker, given only the submitting reply: enough to finish
        # *if* it resumes, not enough to redo the tool step.
        resumed = register_agent(dsn, [replies[1]], tracer)

        assert await Worker(dsn, "w2", tracer=tracer).run_once() == 1
        task = get_task(conn, spec.task_id)
        assert task is not None and task.state is TaskState.SUCCEEDED
        assert len(resumed.calls) == 1  # one step re-executed at most
        result = conn.execute(
            "SELECT result FROM tasks WHERE id = %s", (spec.task_id,)
        ).fetchone()
        assert result is not None and result[0]["output"] == {"answer": "steward"}

        run = get_run(conn, spec.run_id)
        assert run is not None
        # The successful attempt is charged what *it* spent. The checkpoint's
        # usage is cumulative so the loop can bound the whole task against one
        # cap; reporting that on success would charge the run a second time for
        # the failed attempt it has already paid for (#69 review, finding 2).
        submitted = sum(chunk.usage.total_tokens for chunk in replies[1].chunks)
        assert run.usage.tokens == failed_attempt_spend.tokens + submitted
        # Not the cumulative figure: charging that would make this
        # `failed + (failed + submitted)`.
        assert run.usage.tokens < 2 * failed_attempt_spend.tokens + submitted
        assert run.usage.over(run.budget) == ()
