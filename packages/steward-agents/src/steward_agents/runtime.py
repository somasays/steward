"""One bounded tool-use loop; LangGraph is contained in this module."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from steward_llm import (
    CompletionRequest,
    FinishReason,
    LLMClient,
    LLMError,
    Message,
    ModelUsage,
    Role,
    ToolCall,
)
from steward_schemas import AgentSpec, RunBudget

from steward_agents.tools import ToolRegistry


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
    pending_tool_calls: tuple[ToolCall, ...] = ()
    finished: bool = False


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    output: BaseModel
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
    ) -> None:
        self._client = client
        self._tools = tools
        self._checkpoints = checkpoints
        self._reservation = reservation

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
            messages=messages,
            usage=RunBudget(steps=0, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta()),
        )
        if checkpoint.finished:
            return self._result(checkpoint, output_model)

        async def cycle(state: _State) -> _State:
            current = state["checkpoint"]
            if current.pending_tool_calls:
                call = current.pending_tool_calls[0]
                self._preflight_tool(current.usage, spec.limits)
                started = time.monotonic()
                tool_output = await self._tools.invoke(call, allowlist=frozenset(spec.tools))
                tool_usage = RunBudget(
                    steps=1,
                    tokens=0,
                    cost_usd=Decimal(0),
                    wall_clock=timedelta(seconds=time.monotonic() - started),
                )
                updated = current.model_copy(update={
                    "messages": current.messages + (
                        Message(
                            role=Role.TOOL,
                            content=tool_output.model_dump_json(),
                            tool_call_id=call.id,
                        ),
                    ),
                    "usage": _add(current.usage, tool_usage),
                    "pending_tool_calls": current.pending_tool_calls[1:],
                })
                await self._checkpoints.save(key, updated)
                return {"checkpoint": updated}

            self._preflight_model(current.usage, spec.limits)
            request = CompletionRequest(
                alias=spec.model_alias,
                messages=current.messages,
                prompt_version=prompt_version,
                tools=self._tools.schemas(spec.tools),
                max_tokens=self._reservation.tokens,
            )
            try:
                completion = await self._client.complete(request)
                usage = _add(current.usage, _model_usage(completion.usage))
            except LLMError as exc:
                usage = _add(current.usage, _model_usage(exc.usage))
                failed = current.model_copy(update={"usage": usage})
                await self._checkpoints.save(key, failed)
                raise

            updated = current.model_copy(update={
                "messages": current.messages + (
                    Message(
                        role=Role.ASSISTANT,
                        content=completion.text,
                        tool_calls=completion.tool_calls,
                    ),
                ),
                "usage": usage,
                "finished": completion.finish_reason is FinishReason.STOP,
                "pending_tool_calls": completion.tool_calls,
            })
            await self._checkpoints.save(key, updated)
            return {"checkpoint": updated}

        graph = StateGraph(_State)
        graph.add_node("cycle", cycle)
        graph.add_edge(START, "cycle")
        graph.add_conditional_edges(
            "cycle",
            lambda state: END if state["checkpoint"].finished else "cycle",
            {END: END, "cycle": "cycle"},
        )
        final = await graph.compile().ainvoke({"checkpoint": checkpoint})
        return self._result(final["checkpoint"], output_model)

    def _preflight_model(self, used: RunBudget, cap: RunBudget) -> None:
        reserved = RunBudget(
            steps=1,
            tokens=self._reservation.tokens,
            cost_usd=self._reservation.cost_usd,
            wall_clock=self._reservation.wall_clock,
        )
        self._require_fit(_add(used, reserved), cap, "model")

    @staticmethod
    def _preflight_tool(used: RunBudget, cap: RunBudget) -> None:
        AgentRuntime._require_fit(
            _add(
                used,
                RunBudget(steps=1, tokens=0, cost_usd=Decimal(0), wall_clock=timedelta()),
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
        content = checkpoint.messages[-1].content
        try:
            output = output_model.model_validate_json(content)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"agent output failed {output_model.__name__} validation: {exc}") from exc
        return AgentResult(output=output, usage=checkpoint.usage)
