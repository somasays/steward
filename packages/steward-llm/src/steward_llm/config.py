"""The gateway configuration, and the startup refusal that guards it.

I15 says production model aliases resolve only to approved self-hosted endpoints.
The failure it guards against does not look like a failure: a base URL pointed at a
hosted API answers, streams, and costs money exactly like the right one, and S1's
import boundaries cannot see it because the call goes through LiteLLM either way.
Documentation cannot catch that and neither can a lint. So the check runs where the
mistake becomes real — at process start, before any work is claimed — and its only
outcome is a refusal to boot.

Three decisions worth stating:

* **Production is the default mode.** `STEWARD_DEPLOYMENT_MODE` unset means
  production, and an unrecognised value is a refusal. Development is opted into,
  never fallen into.
* **Every entry in `model_list` is validated, not only the four aliases.** LiteLLM
  routes across fallbacks and same-named groups, so a hosted entry sitting anywhere
  in a production config is reachable; validating only the named aliases would leave
  exactly the hole that makes fallback chains dangerous.
* **A missing `api_base` is a refusal.** It is the quietest form of the mistake: with
  no base URL, `model: claude-sonnet-5` resolves to the provider's own hosted API and
  the config never mentions a URL at all.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from steward_llm.endpoints import (
    EndpointAllowlist,
    GatewayConfigError,
    NonApprovedEndpoint,
)

__all__ = [
    "APPROVED_ENDPOINTS_ENV",
    "CONFIG_PATH_ENV",
    "MODE_ENV",
    "PRODUCTION_ALIASES",
    "SELF_HOSTED_MODEL_PREFIXES",
    "DeploymentMode",
    "GatewayConfig",
    "InvalidGatewayConfig",
    "ModelBinding",
    "committed_production_config",
    "gateway_config_from_env",
]

CONFIG_PATH_ENV = "STEWARD_LITELLM_CONFIG"
"""Path to the LiteLLM proxy config this deployment runs. Unset means no gateway."""

APPROVED_ENDPOINTS_ENV = "STEWARD_LLM_APPROVED_ENDPOINTS"
"""Comma-separated approved base URLs. Unset falls back to the committed allowlist."""

MODE_ENV = "STEWARD_DEPLOYMENT_MODE"

CONFIG_DIR = Path(__file__).parent / "config"
COMMITTED_CONFIG = CONFIG_DIR / "litellm.production.yaml"
COMMITTED_ALLOWLIST = CONFIG_DIR / "approved_endpoints.yaml"

PRODUCTION_ALIASES = frozenset(
    {
        "steward-reasoning",
        "steward-fast",
        "steward-classify",
        "steward-embed",
    }
)
"""The aliases SPEC §6 routes on. A production config missing one fails at boot rather
than at the first call that needs it."""

SELF_HOSTED_MODEL_PREFIXES = ("hosted_vllm/", "openai/")
"""LiteLLM provider prefixes that address an OpenAI-compatible server by URL. Any other
prefix names a provider whose SDK picks the destination, which the allowlist cannot see."""


class DeploymentMode(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"


class InvalidGatewayConfig(GatewayConfigError):
    """A gateway config that cannot be read, parsed, or trusted to route production."""


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """One `model_list` entry: the alias callers use, the model LiteLLM routes to, and
    the base URL it is sent to (absent when the provider decides)."""

    alias: str
    model: str
    api_base: str | None


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """A gateway configuration that has passed the startup check.

    Validation happens in `__post_init__`, so an instance of this type is evidence the
    check ran: there is no construction path that skips it, and callers that need a
    gateway (the client lands with #50) can take this type instead of a file path.
    """

    mode: DeploymentMode
    source: str
    bindings: tuple[ModelBinding, ...]
    allowlist: EndpointAllowlist

    def __post_init__(self) -> None:
        validate_routing(self.bindings, self.allowlist, self.mode)


def validate_routing(
    bindings: Sequence[ModelBinding],
    allowlist: EndpointAllowlist,
    mode: DeploymentMode,
) -> None:
    """Refuse a production routing table that can reach a non-approved endpoint (I15).

    Development mode skips the endpoint rules — that is what it is for — and is
    reachable only by setting `STEWARD_DEPLOYMENT_MODE=development` explicitly.
    """
    if mode is DeploymentMode.DEVELOPMENT:
        return
    missing = sorted(PRODUCTION_ALIASES - {binding.alias for binding in bindings})
    if missing:
        raise InvalidGatewayConfig(f"production config binds no model for: {', '.join(missing)}")
    if not allowlist.approved:
        raise NonApprovedEndpoint(f"no approved endpoints configured; set {APPROVED_ENDPOINTS_ENV} (I15)")
    for binding in bindings:
        _validate_binding(binding, allowlist)


def _validate_binding(binding: ModelBinding, allowlist: EndpointAllowlist) -> None:
    if binding.api_base is None:
        raise NonApprovedEndpoint(
            f"{binding.alias!r} declares no api_base, so {binding.model!r} resolves to its "
            "provider's hosted API (I15)"
        )
    if not binding.model.startswith(SELF_HOSTED_MODEL_PREFIXES):
        raise NonApprovedEndpoint(
            f"{binding.alias!r} binds {binding.model!r}, whose provider chooses the destination; "
            f"production models are addressed by URL ({', '.join(SELF_HOSTED_MODEL_PREFIXES)})"
        )
    if not allowlist.admits(binding.api_base):
        raise NonApprovedEndpoint(
            f"{binding.alias!r} resolves to {binding.api_base}, which is not an approved "
            "self-hosted endpoint (I15)"
        )


def parse_litellm_config(document: object, source: str) -> tuple[ModelBinding, ...]:
    """`model_list` -> bindings. Every shape this does not recognise is a refusal.

    The parser is deliberately shallow: it reads the two fields that decide where a
    prompt goes and does not reimplement LiteLLM's config semantics. But it refuses
    what it cannot read, because a config it skipped past is a config it did not check.
    """
    if not isinstance(document, dict):
        raise InvalidGatewayConfig(f"{source}: expected a YAML mapping at the top level")
    entries = document.get("model_list")
    if not isinstance(entries, list) or not entries:
        raise InvalidGatewayConfig(f"{source}: model_list must be a non-empty list")
    return tuple(_binding(entry, source, index) for index, entry in enumerate(entries))


def _binding(entry: object, source: str, index: int) -> ModelBinding:
    where = f"{source}: model_list[{index}]"
    if not isinstance(entry, dict):
        raise InvalidGatewayConfig(f"{where} is not a mapping")
    alias = entry.get("model_name")
    params = entry.get("litellm_params")
    if not isinstance(alias, str) or not alias:
        raise InvalidGatewayConfig(f"{where} has no model_name")
    if not isinstance(params, dict):
        raise InvalidGatewayConfig(f"{where} has no litellm_params")
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise InvalidGatewayConfig(f"{where} has no litellm_params.model")
    api_base = params.get("api_base")
    if api_base is not None and not isinstance(api_base, str):
        raise InvalidGatewayConfig(f"{where} has a non-string api_base")
    return ModelBinding(alias=alias, model=model, api_base=api_base)


def load_litellm_config(path: Path) -> tuple[ModelBinding, ...]:
    """Read and parse a LiteLLM config file. Unreadable or unparseable is a refusal."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidGatewayConfig(f"{path}: cannot be read ({exc.strerror})") from exc
    try:
        document: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InvalidGatewayConfig(f"{path}: is not valid YAML ({exc})") from exc
    return parse_litellm_config(document, str(path))


def load_approved_endpoints(path: Path) -> tuple[str, ...]:
    """Read an allowlist file: `approved_endpoints:` holding a list of base URLs."""
    try:
        document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InvalidGatewayConfig(f"{path}: cannot be read ({exc.strerror})") from exc
    except yaml.YAMLError as exc:
        raise InvalidGatewayConfig(f"{path}: is not valid YAML ({exc})") from exc
    urls = document.get("approved_endpoints") if isinstance(document, dict) else None
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise InvalidGatewayConfig(f"{path}: approved_endpoints must be a list of URLs")
    return tuple(str(url) for url in urls)


def mode_from_env(env: Mapping[str, str]) -> DeploymentMode:
    """Production unless development is asked for by name. An unknown value refuses."""
    raw = env.get(MODE_ENV, "").strip().lower()
    if not raw:
        return DeploymentMode.PRODUCTION
    try:
        return DeploymentMode(raw)
    except ValueError:
        raise InvalidGatewayConfig(
            f"{MODE_ENV}={raw!r} is not a deployment mode "
            f"({', '.join(mode.value for mode in DeploymentMode)})"
        ) from None


def allowlist_from_env(env: Mapping[str, str]) -> EndpointAllowlist:
    """The deployment's approved endpoints, or the committed default when it names none."""
    raw = env.get(APPROVED_ENDPOINTS_ENV, "").strip()
    urls = (
        [part.strip() for part in raw.split(",") if part.strip()]
        if raw
        else list(load_approved_endpoints(COMMITTED_ALLOWLIST))
    )
    return EndpointAllowlist.from_urls(urls)


def gateway_config_from_env(env: Mapping[str, str] | None = None) -> GatewayConfig | None:
    """The validated gateway config for this process, or `None` when none is configured.

    `None` is not a degraded gateway: it means this process has no gateway at all and
    cannot call a model (M0/M1 run credential-free). Whenever a config *is* named, it is
    validated here and a failure propagates — the caller is a composition root and the
    intended outcome is that the process does not start.
    """
    source = os.environ if env is None else env
    path = source.get(CONFIG_PATH_ENV, "").strip()
    if not path:
        return None
    return GatewayConfig(
        mode=mode_from_env(source),
        source=path,
        bindings=load_litellm_config(Path(path)),
        allowlist=allowlist_from_env(source),
    )


def committed_production_config() -> GatewayConfig:
    """The config this repo ships, validated in production mode against the committed
    allowlist — the same check a process runs at startup, over the files in git (S9)."""
    return GatewayConfig(
        mode=DeploymentMode.PRODUCTION,
        source=str(COMMITTED_CONFIG),
        bindings=load_litellm_config(COMMITTED_CONFIG),
        allowlist=EndpointAllowlist.from_urls(load_approved_endpoints(COMMITTED_ALLOWLIST)),
    )
