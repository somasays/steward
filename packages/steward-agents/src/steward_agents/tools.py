"""Typed tools and the least-privilege registry used by agent runs.

Every tool declares what it costs before it may be called. `wall_clock` is
required and has no default for the same reason `RunBudget`'s fields do not: a
tool whose worst case is unstated is a tool the loop cannot refuse, and a step
that cannot be refused is charged after the fact instead of bounded (I12). The
loop reserves that figure before the call and debits the real elapsed time
after, so an optimistic declaration costs accuracy but never the cap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel, ValidationError
from steward_llm import ToolCall, ToolSchema


class DisallowedTool(PermissionError):
    """A model requested a tool outside its agent's explicit allowlist."""


class ToolValidationError(ValueError):
    """A tool's input or output did not satisfy its owned Pydantic model.

    Raised out of `invoke`, but an *input* failure is the model's mistake and
    the loop answers it by handing the message back rather than ending the run
    (SPEC.md §3.2). An output failure is the tool's own bug and is terminal.
    """

    def __init__(self, message: str, *, blames_model: bool) -> None:
        super().__init__(message)
        self.blames_model = blames_model
        """Whether the model can fix this by calling again with better arguments."""


type ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    wall_clock: timedelta
    """The worst case this tool is reserved against before it is allowed to run."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )


class ToolRegistry:
    """A registry that validates both sides of every tool boundary."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        handler: ToolHandler,
        wall_clock: timedelta,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if wall_clock <= timedelta(0):
            raise ValueError(f"tool {name!r} must reserve a positive wall-clock worst case")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            wall_clock=wall_clock,
        )

    def schemas(self, allowlist: Iterable[str]) -> tuple[ToolSchema, ...]:
        return tuple(self._resolve(name).schema for name in allowlist)

    def reservation(self, name: str) -> timedelta:
        """What a call to `name` must have left before it may start."""
        return self._resolve(name).wall_clock

    def allows(self, name: str, allowlist: frozenset[str]) -> bool:
        """Whether this agent may call `name` at all -- asked before execution."""
        return name in allowlist and name in self._tools

    async def invoke(
        self, call: ToolCall, *, allowlist: frozenset[str]
    ) -> tuple[BaseModel, BaseModel]:
        """Run one tool, returning what it was *given* and what it returned.

        Both validated. The request is handed back because a trace of the raw
        `call.arguments` shows what the model emitted, which is precisely what
        has not been checked yet -- I7 asks for validated I/O, and the validated
        input only exists here.
        """
        if call.name not in allowlist:
            raise DisallowedTool(f"tool {call.name!r} is not allowed for this agent")
        tool = self._resolve(call.name)
        try:
            request = tool.input_model.model_validate_json(call.arguments)
        except ValidationError as exc:
            raise ToolValidationError(
                f"invalid input for tool {call.name!r}: {exc}", blames_model=True
            ) from exc
        response = await tool.handler(request)
        try:
            return request, tool.output_model.model_validate(response)
        except ValidationError as exc:
            raise ToolValidationError(
                f"invalid output from tool {call.name!r}: {exc}", blames_model=False
            ) from exc

    def _resolve(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise DisallowedTool(f"tool {name!r} is not registered") from exc
