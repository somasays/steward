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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Protocol, TypedDict

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


class _State(TypedDict):
    checkpoint: AgentCheckpoint


def _add(left: RunBudget, right: RunBudget) -> RunBudget:
    return RunBudget.total((left, right))


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
    ) -> None:
        self._client = client
        self._tools = tools
        self._checkpoints = checkpoints
        self._reservation = reservation
        self._on_spend = on_spend

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
                await self._tool_step(key, current, allowlist, spec.limits)
                if current.pending_tool_calls
                else await self._model_step(key, current, spec, prompt_version, schemas)
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
    ) -> AgentCheckpoint:
        self._preflight_model(current.usage, spec.limits)
        request = CompletionRequest(
            alias=spec.model_alias,
            messages=current.messages,
            prompt_version=prompt_version,
            tools=schemas,
            max_tokens=self._reservation.tokens,
        )
        try:
            completion = await self._client.complete(request)
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
        usage = _add(current.usage, spend)
        submitted = next(
            (call for call in completion.tool_calls if call.name == SUBMIT_RESULT), None
        )
        if submitted is None and completion.finish_reason is FinishReason.STOP:
            raise AgentRuntimeError(
                f"the agent stopped without calling {SUBMIT_RESULT!r}; "
                "a run's result is submitted through that tool, never as prose"
            )
        updated = current.model_copy(
            update={
                "messages": current.messages
                + (
                    Message(
                        role=Role.ASSISTANT,
                        content=completion.text,
                        tool_calls=completion.tool_calls,
                    ),
                ),
                "usage": usage,
                "output_json": submitted.arguments if submitted is not None else None,
                "pending_tool_calls": ()
                if submitted is not None
                else tuple(
                    call for call in completion.tool_calls if call.name != SUBMIT_RESULT
                ),
            }
        )
        await self._checkpoints.save(key, updated)
        return updated

    async def _tool_step(
        self,
        key: str,
        current: AgentCheckpoint,
        allowlist: frozenset[str],
        cap: RunBudget,
    ) -> AgentCheckpoint:
        call = current.pending_tool_calls[0]
        # Asked before the reservation and before the handler: a tool this agent
        # may not call is refused, not costed.
        if not self._tools.allows(call.name, allowlist):
            raise DisallowedTool(f"tool {call.name!r} is not allowed for this agent")
        self._preflight_tool(current.usage, cap, self._tools.reservation(call.name))
        started = time.monotonic()
        answered = current.answered_with_feedback
        try:
            content = (await self._tools.invoke(call, allowlist=allowlist)).model_dump_json()
        except ToolValidationError as exc:
            # Bad arguments are the model's to fix, once (SPEC.md §3.2). A second
            # failure on the same call is not feedback any more, it is a loop.
            if not exc.blames_model or call.id in current.answered_with_feedback:
                raise
            content = str(exc)
            answered = current.answered_with_feedback + (call.id,)

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

    def _preflight_model(self, used: RunBudget, cap: RunBudget) -> None:
        reserved = RunBudget(
            steps=1,
            tokens=self._reservation.tokens,
            cost_usd=self._reservation.cost_usd,
            wall_clock=self._reservation.wall_clock,
        )
        self._require_fit(_add(used, reserved), cap, "model")

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
