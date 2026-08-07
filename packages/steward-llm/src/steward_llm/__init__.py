"""steward-llm: thin LiteLLM gateway wrapper — typed completions via model aliases.

Provider SDKs (openai, anthropic, ...) and litellm itself are contained to
this package (I2); callers elsewhere use model aliases only.

What exists today is the half that must be true before a model is ever called:
production aliases resolve only to approved self-hosted endpoints (I15), checked at
process start by `gateway_config_from_env`, which refuses rather than degrades. The
client that uses a `GatewayConfig` lands with #50.
"""

from steward_llm.config import (
    APPROVED_ENDPOINTS_ENV,
    CONFIG_PATH_ENV,
    MODE_ENV,
    PRODUCTION_ALIASES,
    DeploymentMode,
    GatewayConfig,
    InvalidGatewayConfig,
    ModelBinding,
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

__all__ = [
    "APPROVED_ENDPOINTS_ENV",
    "CONFIG_PATH_ENV",
    "MODE_ENV",
    "PRODUCTION_ALIASES",
    "DeploymentMode",
    "Endpoint",
    "EndpointAllowlist",
    "GatewayConfig",
    "GatewayConfigError",
    "InvalidGatewayConfig",
    "MalformedEndpoint",
    "ModelBinding",
    "NonApprovedEndpoint",
    "committed_production_config",
    "gateway_config_from_env",
]
