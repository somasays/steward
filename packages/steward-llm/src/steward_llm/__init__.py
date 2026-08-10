"""steward-llm: thin gateway wrapper — typed completions via model aliases.

Provider SDKs (openai, anthropic, ...) and litellm itself are contained to
this package (I2); callers elsewhere use model aliases only.

Two halves. The first must be true before a model is ever called: production
aliases resolve only to approved self-hosted endpoints (I15), checked at process
start by `gateway_config_from_env`, which refuses rather than degrades. The
second is `LLMClient`, which takes that validated `GatewayConfig` and nothing
else — no path, no environment — so a process that skipped the check cannot
construct a client, and reaches models through a `GatewayTransport` seam whose
one implementation today is the deterministic `StubGateway` (see
`steward_llm.client` for why the LiteLLM transport is not written yet).
"""

from steward_llm.client import LLMClient
from steward_llm.completion import (
    CompletionRequest,
    CompletionResult,
    FinishReason,
    Message,
    ModelUsage,
    Role,
    ToolCall,
    ToolSchema,
)
from steward_llm.config import (
    APPROVED_ENDPOINTS_ENV,
    CONFIG_PATH_ENV,
    MODE_ENV,
    PRODUCTION_ALIASES,
    DeploymentMode,
    GatewayConfig,
    InvalidGatewayConfig,
    ModelBinding,
    TokenPricing,
    committed_production_config,
    gateway_config_from_env,
)
from steward_llm.endpoints import (
    Endpoint,
    EndpointAllowlist,
    GatewayConfigError,
    MalformedEndpoint,
    NonApprovedEndpoint,
)
from steward_llm.errors import CompletionFailed, CompletionTimedOut, LLMError, UnboundAlias
from steward_llm.proxy import (
    PROXY_KEY_ENV,
    PROXY_URL_ENV,
    InvalidProxyConfig,
    LiteLLMProxyTransport,
    ProxyConfig,
    proxy_config_from_env,
)
from steward_llm.stub import StubGateway, StubReply
from steward_llm.transport import CompletionChunk, GatewayCall, GatewayTransport

__all__ = [
    "APPROVED_ENDPOINTS_ENV",
    "CONFIG_PATH_ENV",
    "MODE_ENV",
    "PRODUCTION_ALIASES",
    "PROXY_KEY_ENV",
    "PROXY_URL_ENV",
    "CompletionChunk",
    "InvalidProxyConfig",
    "LiteLLMProxyTransport",
    "ProxyConfig",
    "proxy_config_from_env",
    "CompletionFailed",
    "CompletionRequest",
    "CompletionResult",
    "CompletionTimedOut",
    "DeploymentMode",
    "Endpoint",
    "EndpointAllowlist",
    "FinishReason",
    "GatewayCall",
    "GatewayConfig",
    "GatewayConfigError",
    "GatewayTransport",
    "InvalidGatewayConfig",
    "LLMClient",
    "LLMError",
    "MalformedEndpoint",
    "Message",
    "ModelBinding",
    "TokenPricing",
    "ModelUsage",
    "NonApprovedEndpoint",
    "Role",
    "StubGateway",
    "StubReply",
    "ToolCall",
    "ToolSchema",
    "UnboundAlias",
    "committed_production_config",
    "gateway_config_from_env",
]
