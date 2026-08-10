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
import json
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
    TokenPricing,
    ToolCall,
    ToolSchema,
)
from steward_schemas import AgentSpec, RunBudget
from steward_telemetry import NoopTracer, SpanOutcome, Tracer

from steward_agents.tools import DisallowedTool, ToolRegistry, ToolValidationError

MAX_CORRECTIONS = 1
"""How many validation errors one attempt may hand back before failing.

SPEC §3.2 says "one retry", and this is that number in one place rather than a
condition repeated at each site that can raise a validation error.
"""

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
    """What *this attempt* has spent, against the budget this attempt was given.

    Per-attempt rather than cumulative, because the cap handed to a resumed
    attempt has already had earlier attempts subtracted from it by the queue
    (`tasks.used_*`). One layer accounts across attempts and one accounts within
    one; when both did, a resumed run was refused for spend that had already
    been deducted from its cap.

    What *does* carry across a resume is everything else on this checkpoint --
    the message history, the pending calls, the one-retry allowance -- which is
    what makes a restart cost one step rather than a run.
    """

    pending_tool_calls: tuple[ToolCall, ...] = ()
    corrections: int = 0
    """How many times this attempt has handed a validation error back.

    A **count**, not a set of call ids. Ids are chosen by the model, so a model
    that reissues the same malformed call under a fresh id earns a fresh
    correction every time -- the allowance keyed on ids was one the caller
    being limited got to mint more of, which is no limit at all. What stopped
    such a loop was the step budget, eventually, which is the audit fence
    wearing a retry policy's clothes.

    Kept on the checkpoint rather than in a local so the allowance survives a
    resume; in a local, a restart would refill it.
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


PER_MESSAGE_TOKEN_OVERHEAD = 8
"""Tokens a chat template spends per message on role markers and separators.

Additive and generous, so it only ever makes the ceiling below more
conservative. The exact number is the gateway's business and differs per
template; what matters here is that it is never negative.
"""


def _prompt_token_ceiling(messages: tuple[Message, ...], tools: tuple[ToolSchema, ...]) -> int:
    """An upper bound -- not an estimate -- on what this request's prompt costs.

    The bound is the UTF-8 **byte** length of everything sent. Byte-level BPE,
    which every model behind our aliases uses (Qwen, Llama and the GPT family
    all tokenize bytes), builds each token from one or more bytes, so a token
    can never be cheaper than a byte and `tokens <= bytes` holds for any input
    -- including the identifiers, JSON, and non-ASCII text a
    characters-divided-by-three estimate under-counts.

    Tool schemas are counted because they are part of the prompt: a request
    offering four tools sends their JSON Schema every time, and leaving it out
    made the reservation wrong by however many tools an agent had.

    So is everything else a message carries. An assistant turn's `tool_calls`
    are preserved precisely so they are sent again on the next request, and
    their `arguments` are unbounded JSON -- a single large call could push the
    real prompt past a ceiling computed from `content` alone, which is a ceiling
    that does not hold. `tool_call_id` is counted for the same reason: small,
    but sent.

    Loose, deliberately. A ceiling that is never exceeded is worth more here
    than a closer figure that sometimes is: this is what makes "a step that
    cannot fit is never started" a bound rather than a hope (I12). The one
    assumption is stated above and is a property of the tokenizer family, not
    of this code -- a model tokenizing something other than bytes would need
    this revisited, and the gateway is where that would be known (D11).
    """
    prompt = sum(_message_bytes(message) for message in messages)
    overhead = PER_MESSAGE_TOKEN_OVERHEAD * len(messages)
    schemas = sum(
        len(tool.name.encode("utf-8"))
        + len(tool.description.encode("utf-8"))
        + len(json.dumps(tool.parameters).encode("utf-8"))
        for tool in tools
    )
    return prompt + overhead + schemas


def _usage_fields(usage: ModelUsage) -> dict[str, object]:
    """What a generation cost, as trace fields (I7).

    One renderer for both outcomes, so a failed call cannot quietly carry fewer
    fields than a successful one -- which is the shape the omission took.
    """
    return {
        "latency_seconds": usage.latency.total_seconds(),
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cost_usd": str(usage.cost_usd),
    }


def _message_bytes(message: Message) -> int:
    """Every byte of one message that goes back over the wire.

    `content` is the obvious part and was once the only part. The rest is what
    a conversation carries between turns: the calls an assistant emitted, their
    arguments, and the id a tool result answers. Anything omitted here is a hole
    in the bound, not a rounding error.
    """
    return (
        len(message.content.encode("utf-8"))
        + len((message.tool_call_id or "").encode("utf-8"))
        + sum(
            len(call.id.encode("utf-8"))
            + len(call.name.encode("utf-8"))
            + len(call.arguments.encode("utf-8"))
            for call in message.tool_calls
        )
    )


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
        resumed = await self._checkpoints.load(key)
        # A resumed attempt starts its usage at zero, and that is not an
        # oversight about what earlier attempts spent -- it is because the cap
        # it is given has *already* had that subtracted. The queue owns
        # cross-attempt accounting (`tasks.used_*`, and the remaining budget a
        # claim hands out); this loop owns one attempt against the budget it was
        # handed. Carrying the cumulative figure here as well would subtract the
        # same spend twice and refuse a resume that is affordable.
        checkpoint = (
            resumed.model_copy(update={"usage": NOTHING_SPENT})
            if resumed is not None
            else AgentCheckpoint(messages=messages, usage=NOTHING_SPENT)
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
        self._preflight_model(
            current.usage, spec.limits, current.messages, schemas, spec.model_alias
        )
        request = CompletionRequest(
            alias=spec.model_alias,
            messages=current.messages,
            prompt_version=prompt_version,
            tools=schemas,
            # `max_tokens` bounds the *completion*, while the reservation is a
            # total. Sending the reservation here would let a call cost the
            # prompt on top of everything that was checked for.
            max_tokens=self._completion_allowance(current.messages, schemas),
        )
        span = self._tracer.generation_span(
            trace_id=trace.trace_id,
            task_id=trace.task_id,
            model_alias=spec.model_alias,
            prompt_version=prompt_version,
        )
        try:
            with span as generation:
                try:
                    completion = await self._client.complete(request)
                except LLMError as failure:
                    # A call that died after generating is the accounting case
                    # that matters most, so its span carries the same fields a
                    # successful one does. Observed *inside* the span, because
                    # once the exception leaves this block the observation is
                    # closed and there is nothing left to attach them to.
                    generation.observe(
                        _usage_fields(failure.usage)
                        | {
                            "failed": True,
                            # The same input a successful call records. It is in
                            # hand either way, and a trace that shows what a
                            # working call saw but not what a failing one saw is
                            # missing the case an operator opens the trace for.
                            "input": [message.content for message in request.messages],
                        }
                    )
                    raise
                # What the call cost and what passed through it (I7). The
                # request's messages are what the model was actually sent, and
                # anything customer-derived reached them masked (I6) -- this
                # seam exports, it does not sanitise.
                generation.observe(
                    _usage_fields(completion.usage)
                    | {
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
        if base.corrections >= MAX_CORRECTIONS:
            raise AgentRuntimeError(f"{SUBMIT_RESULT} was rejected twice: {problem}")
        return base.model_copy(
            update={
                "messages": base.messages
                + (Message(role=Role.TOOL, content=problem, tool_call_id=submitted.id),),
                "corrections": base.corrections + 1,
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
        corrections = current.corrections
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
                    validated, result = await self._tools.invoke(call, allowlist=allowlist)
                    content = result.model_dump_json()
                    # Both sides are the *validated* models' own JSON, so what
                    # the trace shows is what the registry admitted, not the
                    # arguments the model emitted -- which are the unchecked
                    # thing, and would make "validated I/O" a label rather than
                    # a description (I7).
                    tool.observe(
                        {
                            "latency_seconds": time.monotonic() - started,
                            "input": validated.model_dump_json(),
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
            if not exc.blames_model or current.corrections >= MAX_CORRECTIONS:
                await self._charge_failed_step(key, current, self._elapsed_step(started))
                raise
            content = str(exc)
            corrections = current.corrections + 1
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
                "corrections": corrections,
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

    def _price(self, alias: str) -> TokenPricing:
        """What a token costs on this alias, or a refusal.

        An alias whose bindings carry no prices cannot be cost-bounded before a
        call, and I12 asks for a step that cannot fit to never be *started* --
        so it is refused rather than run against a declared figure nothing
        checks. The prices are validated configuration (`steward_llm.config`),
        which is why this can trust them.
        """
        pricing = self._client.pricing_for(alias)
        if pricing is None:
            raise BudgetExceeded(
                f"{alias!r} declares no token prices, so this step cannot be bounded "
                "in dollars before it runs"
            )
        return pricing

    def _preflight_model(
        self,
        used: RunBudget,
        cap: RunBudget,
        messages: tuple[Message, ...],
        tools: tuple[ToolSchema, ...],
        alias: str,
    ) -> None:
        """Refuse a model step whose worst case does not fit what is left.

        The reservation is a *total* -- prompt plus completion -- and both
        halves are bounded rather than guessed: the prompt by the byte ceiling
        (`_prompt_token_ceiling`), the completion by the `max_tokens` the
        request carries. A step is started only when the two together fit, so
        the call cannot come back having spent more than was checked for.
        """
        ceiling = _prompt_token_ceiling(messages, tools)
        allowance = self._completion_allowance(messages, tools)
        # The cost bound is computed, not declared: the most this call can cost
        # is its bounded prompt at the input price plus its bounded completion
        # at the output price. `ModelReservation.cost_usd` is the *ceiling a
        # caller is willing to pay* and the larger of the two is reserved, so a
        # cheap alias does not get charged an expensive caller's guess and an
        # expensive one is not started on an optimistic one.
        priced = self._price(alias).ceiling(
            prompt_tokens=ceiling, completion_tokens=allowance
        )
        reserved = RunBudget(
            steps=1,
            tokens=max(self._reservation.tokens, ceiling),
            cost_usd=max(self._reservation.cost_usd, priced),
            wall_clock=self._reservation.wall_clock,
        )
        self._require_fit(_add(used, reserved), cap, "model")
        if ceiling >= self._reservation.tokens:
            raise BudgetExceeded(
                f"model step refused before execution; the prompt is at most {ceiling} "
                f"tokens, which leaves nothing inside the {self._reservation.tokens}-token "
                "reservation for a completion"
            )

    def _completion_allowance(
        self, messages: tuple[Message, ...], tools: tuple[ToolSchema, ...]
    ) -> int:
        """What is left of the reservation once the prompt's ceiling is paid for.

        Sent as `max_tokens`, which bounds the completion at the gateway. With
        the prompt bounded above and the completion bounded here, the call's
        total cannot exceed the reservation the preflight approved.
        """
        return max(1, self._reservation.tokens - _prompt_token_ceiling(messages, tools))

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
