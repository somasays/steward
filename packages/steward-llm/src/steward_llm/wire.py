"""The request body, in one place, because two things need to agree on it.

The transport sends it; the budget bound is measured over it. When those were
two pieces of code the bound measured message contents and tool schemas while
the transport sent a JSON document wrapping them — so the ceiling was computed
over a subset of what went on the wire, and a bound over a subset is not a
bound. One serialiser makes the two agree by construction.
"""

from __future__ import annotations

import json
from typing import Any

from steward_llm.completion import CompletionRequest, Message

__all__ = ["request_body", "serialised_size"]


def request_body(request: CompletionRequest, *, max_tokens: int | None = None) -> dict[str, Any]:
    """Exactly what the proxy is sent for `request`.

    `model` is the Steward alias and nothing else: the proxy holds the table
    that turns it into an endpoint, so a worker naming a provider model would
    be routing, which the client exists not to do (I14).
    """
    body: dict[str, Any] = {
        "model": request.alias,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [_message(message) for message in request.messages],
    }
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    allowance = max_tokens if max_tokens is not None else request.max_tokens
    if allowance is not None:
        body["max_tokens"] = allowance
    return body


def serialised_size(body: dict[str, Any]) -> int:
    """The UTF-8 byte length of the body as it goes on the wire.

    The whole document, not the parts of it anyone remembered: keys, quoting,
    braces, the tool schemas, the role markers. Byte-level BPE builds each token
    from one or more bytes, so `tokens <= bytes` holds for any content — which
    is what makes this a ceiling rather than an estimate, at the cost of being a
    loose one.
    """
    return len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _message(message: Message) -> dict[str, Any]:
    """One message in the wire shape, carrying everything it holds.

    `tool_calls` are here because they are sent again on the next request, and
    their `arguments` are unbounded JSON — a bound computed from `content` alone
    would miss them entirely.
    """
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return payload
