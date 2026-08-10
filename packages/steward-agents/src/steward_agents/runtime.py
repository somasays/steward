"""One bounded tool-use loop; LangGraph is contained in this module.

Three things this file is careful about, because each was a way the bound could
have been nominal rather than real:

* **Every step is refused before it runs, not audited after.** Both kinds of
  step reserve their worst case against what is left of the cap -- a model call
  from `ModelReservation`, a tool call from the figure that tool declared -- and
  a step that does not fit raises before any side effect. Actual usage is
  debited afterwards, so an overestimate costs headroom and never the cap (I12).
* **The framework never decides when to stop.** LangGraph executes the graph;
  the loop's exit is our own terminal tool, and its recursion limit is derived
  from the step cap so the budget always refuses first. Should it ever fire
  anyway -- or should any other framework fault escape -- it is converted at the
  boundary, because a `GraphRecursionError` reaching a caller would be a
  LangGraph type crossing a seam this package exists to keep closed (I2, I9).
* **Spend is reported as it happens.** `on_spend` is called with each increment
  at the moment it is incurred. A handler wires it to its task's usage ledger,
  which is what lets a run be charged for an attempt that raises or is killed at
  its wall-clock cap -- the two failures that carry no result to report on
  (SPEC.md §13 D9).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Protocol, TypedDict
from uuid import UUID

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, ValidationError
from steward_llm import (
    CompletionRequest,
    FinishReason,
    LLMClient,
    LLMError,
    Message,
    ModelUsage,
    Role,
    ToolCall,
    ToolSchema,
)
from steward_schemas import AgentSpec, RunBudget
from steward_telemetry import NoopTracer, SpanOutcome, Tracer

from steward_agents.tools import DisallowedTool, ToolRegistry, ToolValidationError

SUBMIT_RESULT = "submit_result"
"""The tool a run ends by calling.

SPEC.md §3.2 requires a task's terminal output to validate against its result
schema, and this is the mechanism: the schema goes to the model as this tool's
parameters, and the run finishes when the model calls it. Parsing the last
message as JSON instead would ask the model for a shape it was never shown.
"""

NOTHING_SPENT = RunBudget(steps=0, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta())


class AgentRuntimeError(RuntimeError):
    """The runtime could not complete the run, for a reason that is not a budget.

    Also the boundary for anything the framework raises: LangGraph faults are
    re-raised as this so no framework type reaches a caller (I2, I9).
    """


class BudgetExceeded(RuntimeError):
    """The next step cannot fit; it was refused before side effects began."""


class ModelReservation(BaseModel):
    """Worst-case amount reserved before each model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens: int = Field(ge=1)
    cost_usd: Decimal = Field(ge=0)
    wall_clock: timedelta = Field(gt=timedelta(0))


class AgentCheckpoint(BaseModel):
    """Portable state written after every completed or failed step."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    messages: tuple[Message, ...]
    usage: RunBudget
    """Cumulative across attempts -- what the *cap* is checked against.

    Distinct from the per-attempt ledger the worker reads: that one counts this
    attempt's increments so a retry is not charged for its predecessors again,
    while this one must carry everything so a resumed run cannot spend the cap
    twice.
    """

    pending_tool_calls: tuple[ToolCall, ...] = ()
    answered_with_feedback: tuple[str, ...] = ()
    """Tool call ids already handed back to the model once for bad arguments.

    Kept on the checkpoint rather than in a local so the one-retry allowance
    survives a resume; in a local, a worker restart would refill it and a model
    looping on the same malformed call could do so indefinitely.
    """

    output_json: str | None = None
    """The submitted result, verbatim. Set exactly when the run has finished."""

    @property
    def finished(self) -> bool:
        return self.output_json is not None


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    output: SerializeAsAny[BaseModel]
    """`SerializeAsAny` because the declared type is the base: without it
    Pydantic serialises against `BaseModel`'s (empty) schema and every field of
    a real result is dropped from `model_dump()`."""

    usage: RunBudget


class CheckpointStore(Protocol):
    async def load(self, key: str) -> AgentCheckpoint | None: ...

    async def save(self, key: str, checkpoint: AgentCheckpoint) -> None: ...


@dataclass(slots=True)
class InMemoryCheckpointStore:
    values: dict[str, AgentCheckpoint] = field(default_factory=dict)

    async def load(self, key: str) -> AgentCheckpoint | None:
        return self.values.get(key)

    async def save(self, key: str, checkpoint: AgentCheckpoint) -> None:
        self.values[key] = checkpoint


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Where this run's spans belong: the run's trace, and the task on it.

    Passed in rather than derived, because the trace id is the run's and this
    package never sees a run -- it is the queue's `ClaimedTask.trace_id`,
    carried down by the handler so a generation lands on the same trace as the
    task that caused it (I7).
    """

    trace_id: str
    task_id: UUID


class _State(TypedDict):
    checkpoint: AgentCheckpoint


def _add(left: RunBudget, right: RunBudget) -> RunBudget:
    return RunBudget.total((left, right))


CHARS_PER_TOKEN = 3
"""Conservative characters-per-token ratio for estimating a prompt's size.

Deliberately below the ~4 that English averages, so the estimate errs high and
the reservation errs toward refusing. It is an estimate and the residual is
stated rather than hidden: a single call can overshoot its reservation by the
estimator's error, and what catches that is the debit afterwards, which makes
the *next* preflight refuse. Exact accounting needs the gateway's own token
count before the call, which the transport seam cannot ask for yet (D11).
"""


def _estimated_prompt_tokens(messages: tuple[Message, ...]) -> int:
    """About how many tokens this history will cost to send."""
    characters = sum(len(message.content) for message in messages)
    return -(-characters // CHARS_PER_TOKEN)  # ceiling division


def _model_usage(usage: ModelUsage) -> RunBudget:
    return RunBudget(
        steps=1,
        tokens=usage.total_tokens,
        cost_usd=usage.cost_usd,
        wall_clock=usage.latency,
    )


class AgentRuntime:
    """Execute one registered agent with no delegation or dynamic tools."""

    def __init__(
        self,
        *,
        client: LLMClient,
        tools: ToolRegistry,
        checkpoints: CheckpointStore,
        reservation: ModelReservation,
        on_spend: Callable[[RunBudget], None] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._checkpoints = checkpoints
        self._reservation = reservation
        self._on_spend = on_spend
        self._tracer: Tracer = tracer if tracer is not None else NoopTracer()

    def _spent(self, amount: RunBudget) -> None:
        """Report an increment the moment it is incurred, before anything can fail."""
        if self._on_spend is not None:
            self._on_spend(amount)

    async def run(
        self,
        *,
        key: str,
        spec: AgentSpec,
        prompt_version: str,
        messages: tuple[Message, ...],
        output_model: type[BaseModel],
        trace: TraceContext,
    ) -> AgentResult:
        checkpoint = await self._checkpoints.load(key) or AgentCheckpoint(
            messages=messages, usage=NOTHING_SPENT
        )
        if checkpoint.finished:
            return self._result(checkpoint, output_model)

        allowlist = frozenset(spec.tools)
        schemas = self._tools.schemas(spec.tools) + (self._submit_schema(output_model),)

        async def cycle(state: _State) -> _State:
            current = state["checkpoint"]
            updated = (
                await self._tool_step(key, current, allowlist, spec.limits, trace)
                if current.pending_tool_calls
                else await self._model_step(
                    key, current, spec, prompt_version, schemas, output_model, trace
                )
            )
            return {"checkpoint": updated}

        graph = StateGraph(_State)
        graph.add_node("cycle", cycle)
        graph.add_edge(START, "cycle")
        graph.add_conditional_edges(
            "cycle",
            lambda state: END if state["checkpoint"].finished else "cycle",
            {END: END, "cycle": "cycle"},
        )
        compiled = graph.compile()
        try:
            final = await compiled.ainvoke(
                {"checkpoint": checkpoint},
                # Derived from the cap so the budget refuses first: every node
                # run debits at least one step, so the steps check fires before
                # this does. It is a backstop against a graph that stops
                # debiting, not the bound itself.
                config={"recursion_limit": max(spec.limits.steps, 1) + 2},
            )
        except GraphRecursionError as exc:
            raise AgentRuntimeError(
                "the agent graph hit its recursion backstop before the step budget "
                "refused a step, which means a cycle ran without debiting"
            ) from exc
        return self._result(final["checkpoint"], output_model)

    @staticmethod
    def _submit_schema(output_model: type[BaseModel]) -> ToolSchema:
        return ToolSchema(
            name=SUBMIT_RESULT,
            description=(
                "Submit the final result of this task. Call this exactly once, "
                "with the completed result, when the work is done."
            ),
            parameters=output_model.model_json_schema(),
        )

    async def _model_step(
        self,
        key: str,
        current: AgentCheckpoint,
        spec: AgentSpec,
        prompt_version: str,
        schemas: tuple[ToolSchema, ...],
        output_model: type[BaseModel],
        trace: TraceContext,
    ) -> AgentCheckpoint:
        self._preflight_model(current.usage, spec.limits, current.messages)
        request = CompletionRequest(
            alias=spec.model_alias,
            messages=current.messages,
            prompt_version=prompt_version,
            tools=schemas,
            # `max_tokens` bounds the *completion*, while the reservation is a
            # total. Sending the reservation here would let a call cost the
            # prompt on top of everything that was checked for.
            max_tokens=self._completion_allowance(current.messages),
        )
        span = self._tracer.generation_span(
            trace_id=trace.trace_id,
            task_id=trace.task_id,
            model_alias=spec.model_alias,
            prompt_version=prompt_version,
        )
        try:
            with span as generation:
                completion = await self._client.complete(request)
                # What the call cost and what passed through it (I7). The
                # request's messages are what the model was actually sent, and
                # anything customer-derived reached them masked (I6) -- this
                # seam exports, it does not sanitise.
                generation.observe(
                    {
                        "latency_seconds": completion.usage.latency.total_seconds(),
                        "prompt_tokens": completion.usage.prompt_tokens,
                        "completion_tokens": completion.usage.completion_tokens,
                        "total_tokens": completion.usage.total_tokens,
                        "cost_usd": str(completion.usage.cost_usd),
                        "finish_reason": completion.finish_reason.value,
                        "input": [message.content for message in request.messages],
                        "output": completion.text,
                        "tool_calls": [call.name for call in completion.tool_calls],
                    }
                )
                generation.record(SpanOutcome.OK)
        except LLMError as exc:
            # The call spent what it generated before it died; report and
            # checkpoint that before letting the failure travel, or a retry
            # would start from a cap that looks untouched.
            spend = _model_usage(exc.usage)
            self._spent(spend)
            await self._checkpoints.save(
                key, current.model_copy(update={"usage": _add(current.usage, spend)})
            )
            raise

        spend = _model_usage(completion.usage)
        self._spent(spend)
        answered = current.messages + (
            Message(
                role=Role.ASSISTANT,
                content=completion.text,
                tool_calls=completion.tool_calls,
            ),
        )
        base = current.model_copy(
            update={"messages": answered, "usage": _add(current.usage, spend)}
        )
        submitted = next(
            (call for call in completion.tool_calls if call.name == SUBMIT_RESULT), None
        )
        try:
            if submitted is None:
                if completion.finish_reason is FinishReason.STOP:
                    raise AgentRuntimeError(
                        f"the agent stopped without calling {SUBMIT_RESULT!r}; "
                        "a run's result is submitted through that tool, never as prose"
                    )
                updated = base.model_copy(
                    update={"pending_tool_calls": completion.tool_calls}
                )
            else:
                updated = self._submission(base, submitted, completion.tool_calls, output_model)
        except Exception:
            # The call was made and its tokens are gone, whatever we decided
            # about the answer. `base` already carries that spend, so
            # checkpointing it before the failure travels is what stops a resumed
            # attempt from believing the call never happened.
            await self._checkpoints.save(key, base)
            raise
        await self._checkpoints.save(key, updated)
        return updated

    def _submission(
        self,
        base: AgentCheckpoint,
        submitted: ToolCall,
        calls: tuple[ToolCall, ...],
        output_model: type[BaseModel],
    ) -> AgentCheckpoint:
        """Accept a submitted result, or hand it back once for correction.

        Validated *here*, before anything terminal is written. Checkpointing an
        unvalidated `output_json` and letting `_result` reject it later would
        make the checkpoint the record of a finished run that cannot finish: a
        resume reads `finished`, re-validates, fails again, and does so forever
        without ever giving the model the correction SPEC §3.2 promises it.

        Submitting alongside other tool calls is refused rather than guessed
        at. Running them and finishing would discard the results the model
        asked for; finishing without them silently drops work it thought it had
        done. Either way the run's meaning would depend on ordering nobody
        declared, so the model is told to submit on its own.
        """
        problem = self._rejection(submitted, calls, output_model)
        if problem is None:
            return base.model_copy(
                update={"output_json": submitted.arguments, "pending_tool_calls": ()}
            )
        if submitted.id in base.answered_with_feedback:
            raise AgentRuntimeError(f"{SUBMIT_RESULT} was rejected twice: {problem}")
        return base.model_copy(
            update={
                "messages": base.messages
                + (Message(role=Role.TOOL, content=problem, tool_call_id=submitted.id),),
                "answered_with_feedback": base.answered_with_feedback + (submitted.id,),
                "pending_tool_calls": (),
            }
        )

    @staticmethod
    def _rejection(
        submitted: ToolCall, calls: tuple[ToolCall, ...], output_model: type[BaseModel]
    ) -> str | None:
        """Why this submission cannot be accepted, in words the model can act on."""
        if len(calls) > 1:
            others = ", ".join(sorted({c.name for c in calls if c.name != SUBMIT_RESULT}))
            return (
                f"{SUBMIT_RESULT} must be the only call in a response; it arrived "
                f"alongside {others}. Finish those first, then submit on its own."
            )
        try:
            output_model.model_validate_json(submitted.arguments)
        except ValidationError as exc:
            return f"invalid result for {SUBMIT_RESULT}: {exc}"
        return None

    async def _tool_step(
        self,
        key: str,
        current: AgentCheckpoint,
        allowlist: frozenset[str],
        cap: RunBudget,
        trace: TraceContext,
    ) -> AgentCheckpoint:
        call = current.pending_tool_calls[0]
        # Asked before the reservation and before the handler: a tool this agent
        # may not call is refused, not costed.
        if not self._tools.allows(call.name, allowlist):
            raise DisallowedTool(f"tool {call.name!r} is not allowed for this agent")
        reserved = self._tools.reservation(call.name)
        self._preflight_tool(current.usage, cap, reserved)
        started = time.monotonic()
        answered = current.answered_with_feedback
        try:
            # The reservation is only a bound if something enforces it. Without
            # this, a tool could be reserved for a second and run for an hour,
            # and the overrun would be discovered by debiting it afterwards --
            # the audit fence again, one level down. This binds a tool that
            # awaits; one blocked in C is still the worker deadline's problem
            # (SPEC §13 D7).
            async with asyncio.timeout(reserved.total_seconds()):
                with self._tracer.tool_span(
                    trace_id=trace.trace_id, task_id=trace.task_id, tool_name=call.name
                ) as tool:
                    content = (
                        await self._tools.invoke(call, allowlist=allowlist)
                    ).model_dump_json()
                    # Both sides are the *validated* models' own JSON, so what
                    # the trace shows is what the registry admitted, not what
                    # the model happened to emit.
                    tool.observe(
                        {
                            "latency_seconds": time.monotonic() - started,
                            "input": call.arguments,
                            "output": content,
                        }
                    )
        except TimeoutError as exc:
            # The step is charged the time it was *allowed*, and the charge is
            # checkpointed before the failure travels. A debit the ledger sees
            # but the checkpoint does not is a resumed run reconsidering this
            # step with more cap than it really has left (#69 review, finding 3).
            await self._charge_failed_step(
                key,
                current,
                RunBudget(steps=1, tokens=0, cost_usd=Decimal(0), wall_clock=reserved),
            )
            raise AgentRuntimeError(
                f"tool {call.name!r} outran the {reserved} it declared"
            ) from exc
        except ToolValidationError as exc:
            # Bad arguments are the model's to fix, once (SPEC.md §3.2). A second
            # failure on the same call is not feedback any more, it is a loop.
            if not exc.blames_model or call.id in current.answered_with_feedback:
                await self._charge_failed_step(key, current, self._elapsed_step(started))
                raise
            content = str(exc)
            answered = current.answered_with_feedback + (call.id,)
        except Exception:
            # Anything else the tool raised still consumed the time it ran for.
            await self._charge_failed_step(key, current, self._elapsed_step(started))
            raise

        spend = RunBudget(
            steps=1,
            tokens=0,
            cost_usd=Decimal(0),
            wall_clock=timedelta(seconds=time.monotonic() - started),
        )
        self._spent(spend)
        updated = current.model_copy(
            update={
                "messages": current.messages
                + (Message(role=Role.TOOL, content=content, tool_call_id=call.id),),
                "usage": _add(current.usage, spend),
                "pending_tool_calls": current.pending_tool_calls[1:],
                "answered_with_feedback": answered,
            }
        )
        await self._checkpoints.save(key, updated)
        return updated

    @staticmethod
    def _elapsed_step(started: float) -> RunBudget:
        """One step, and the wall clock it actually took."""
        return RunBudget(
            steps=1,
            tokens=0,
            cost_usd=Decimal(0),
            wall_clock=timedelta(seconds=time.monotonic() - started),
        )

    async def _charge_failed_step(
        self, key: str, current: AgentCheckpoint, spend: RunBudget
    ) -> None:
        """Report and checkpoint what a step spent before it failed.

        Both halves, in that order, on every path that can raise after work has
        begun. The ledger is what charges the run for an attempt that never
        returns; the checkpoint is what stops a *resumed* attempt from spending
        the same allowance twice. Reporting without checkpointing bills the run
        correctly and lets the agent overrun its cap on resume, which is the
        harder failure to see (#69 review, finding 3).
        """
        self._spent(spend)
        await self._checkpoints.save(
            key, current.model_copy(update={"usage": _add(current.usage, spend)})
        )

    def _preflight_model(
        self, used: RunBudget, cap: RunBudget, messages: tuple[Message, ...]
    ) -> None:
        """Refuse a model step whose worst case does not fit what is left.

        The reservation is a *total* -- prompt plus completion -- so a growing
        message history eats into what the completion may be. When the prompt
        alone no longer fits, there is no completion allowance left to ask for
        and the step is refused here rather than sent as a request that could
        only overrun.
        """
        prompt = _estimated_prompt_tokens(messages)
        reserved = RunBudget(
            steps=1,
            tokens=max(self._reservation.tokens, prompt),
            cost_usd=self._reservation.cost_usd,
            wall_clock=self._reservation.wall_clock,
        )
        self._require_fit(_add(used, reserved), cap, "model")
        if prompt >= self._reservation.tokens:
            raise BudgetExceeded(
                f"model step refused before execution; the prompt alone is about {prompt} "
                f"tokens, which leaves nothing inside the {self._reservation.tokens}-token "
                "reservation for a completion"
            )

    def _completion_allowance(self, messages: tuple[Message, ...]) -> int:
        """What is left of the reservation once the prompt is paid for."""
        return max(1, self._reservation.tokens - _estimated_prompt_tokens(messages))

    @staticmethod
    def _preflight_tool(used: RunBudget, cap: RunBudget, wall_clock: timedelta) -> None:
        AgentRuntime._require_fit(
            _add(
                used,
                RunBudget(steps=1, tokens=0, cost_usd=Decimal(0), wall_clock=wall_clock),
            ),
            cap,
            "tool",
        )

    @staticmethod
    def _require_fit(projected: RunBudget, cap: RunBudget, kind: str) -> None:
        if dimensions := projected.over(cap):
            raise BudgetExceeded(
                f"{kind} step refused before execution; insufficient {', '.join(dimensions)} budget"
            )

    @staticmethod
    def _result(checkpoint: AgentCheckpoint, output_model: type[BaseModel]) -> AgentResult:
        if checkpoint.output_json is None:
            raise AgentRuntimeError("the run ended without a submitted result")
        try:
            output = output_model.model_validate_json(checkpoint.output_json)
        except ValidationError as exc:
            raise AgentRuntimeError(
                f"submitted result failed {output_model.__name__} validation: {exc}"
            ) from exc
        return AgentResult(output=output, usage=checkpoint.usage)
