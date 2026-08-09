"""Typed tools and the least-privilege registry used by agent runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from steward_llm import ToolCall, ToolSchema


class DisallowedTool(PermissionError):
    """A model requested a tool outside its agent's explicit allowlist."""


class ToolValidationError(ValueError):
    """A tool's input or output did not satisfy its owned Pydantic model."""


type ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler

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
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
        )

    def schemas(self, allowlist: Iterable[str]) -> tuple[ToolSchema, ...]:
        return tuple(self._resolve(name).schema for name in allowlist)

    async def invoke(self, call: ToolCall, *, allowlist: frozenset[str]) -> BaseModel:
        if call.name not in allowlist:
            raise DisallowedTool(f"tool {call.name!r} is not allowed for this agent")
        tool = self._resolve(call.name)
        try:
            request = tool.input_model.model_validate_json(call.arguments)
        except ValidationError as exc:
            raise ToolValidationError(f"invalid input for tool {call.name!r}: {exc}") from exc
        response = await tool.handler(request)
        try:
            return tool.output_model.model_validate(response)
        except ValidationError as exc:
            raise ToolValidationError(f"invalid output from tool {call.name!r}: {exc}") from exc

    def _resolve(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise DisallowedTool(f"tool {name!r} is not registered") from exc
