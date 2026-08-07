"""What counts as an approved inference endpoint, and how a base URL is matched against one.

Production inference resolves only to approved self-hosted endpoints (I15), so
something has to decide whether a given `api_base` is one. That decision is here,
and it is deliberately two-layered:

* **The allowlist is the boundary.** A deployment declares the endpoints it runs
  (`config/approved_endpoints.yaml`, overridable per deployment); anything else is
  refused. An empty allowlist admits nothing — the fail-closed direction, because
  the failure this guards against is a config that quietly reaches further than
  intended, and "no endpoints declared" must not read as "all endpoints allowed".
* **The deny layer is a backstop the allowlist cannot unlock.** Well-known hosted
  provider APIs are refused even when an allowlist names them, so the mistake of
  approving `api.openai.com` is not one config edit away. It is a backstop and not
  the boundary: it can never be complete, and a hosted API it has never heard of is
  still refused — by not being on the allowlist.

Matching is on the normalised (scheme, host, port) triple plus a path prefix, so an
approved `http://host:8000` covers `http://host:8000/v1` and nothing on another
host. Everything this module cannot parse is a refusal, never a pass.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from urllib.parse import urlsplit

__all__ = [
    "HOSTED_PROVIDER_HOSTS",
    "Endpoint",
    "EndpointAllowlist",
    "GatewayConfigError",
    "MalformedEndpoint",
    "NonApprovedEndpoint",
]

DEFAULT_PORTS = {"http": 80, "https": 443}

HOSTED_PROVIDER_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "*.openai.azure.com",
    "api.anthropic.com",
    "*.googleapis.com",
    "bedrock*.amazonaws.com",
    "api.mistral.ai",
    "api.cohere.ai",
    "api.cohere.com",
    "api.groq.com",
    "api.together.xyz",
    "api.together.ai",
    "openrouter.ai",
    "*.openrouter.ai",
    "api.fireworks.ai",
    "api.deepseek.com",
    "api.x.ai",
    "api.perplexity.ai",
    "api.voyageai.com",
)
"""Hosts that are a hosted inference API by definition. fnmatch globs, matched on the
lowercased host. Not a firewall and not the boundary — see the module docstring."""


class GatewayConfigError(Exception):
    """The gateway is misconfigured; a process holding this configuration must not start."""


class MalformedEndpoint(GatewayConfigError):
    """A base URL that cannot be understood. Unparseable is refused, never assumed safe."""


class NonApprovedEndpoint(GatewayConfigError):
    """A base URL that is understood and is not one this deployment may send prompts to."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A normalised base URL: scheme, host, explicit port, path without a trailing slash."""

    scheme: str
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, url: str) -> Endpoint:
        """Normalise a base URL, or refuse it.

        Refusals are all cases where two spellings could otherwise be compared and
        disagree, or where the URL is not a base URL at all: a non-HTTP scheme, an
        embedded credential (which would also put a secret in a config file, N7), a
        query or fragment, an unusable port.
        """
        candidate = url.strip()
        if not candidate:
            raise MalformedEndpoint("empty endpoint URL")
        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        if scheme not in DEFAULT_PORTS:
            raise MalformedEndpoint(f"{url!r}: scheme must be http or https, not {parts.scheme!r}")
        if "@" in parts.netloc:
            raise MalformedEndpoint(f"{url!r}: an endpoint URL must not carry credentials")
        if parts.query or parts.fragment:
            raise MalformedEndpoint(f"{url!r}: an endpoint URL is a base, not a request")
        try:
            port = parts.port
        except ValueError as exc:
            raise MalformedEndpoint(f"{url!r}: {exc}") from exc
        host = (parts.hostname or "").lower()
        if not host:
            raise MalformedEndpoint(f"{url!r}: no host")
        return cls(scheme=scheme, host=host, port=port or DEFAULT_PORTS[scheme], path=parts.path.rstrip("/"))

    @property
    def is_hosted_provider(self) -> bool:
        """True for a host that is a hosted inference API by definition (the deny layer)."""
        return any(fnmatch(self.host, pattern) for pattern in HOSTED_PROVIDER_HOSTS)

    def covers(self, other: Endpoint) -> bool:
        """True when `other` is this endpoint or a path below it, on the same origin."""
        if (self.scheme, self.host, self.port) != (other.scheme, other.host, other.port):
            return False
        return other.path == self.path or other.path.startswith(f"{self.path}/")

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"


@dataclass(frozen=True, slots=True)
class EndpointAllowlist:
    """The endpoints a deployment may send prompts to. Empty admits nothing."""

    approved: tuple[Endpoint, ...]

    @classmethod
    def from_urls(cls, urls: Iterable[str]) -> EndpointAllowlist:
        """Build an allowlist, refusing any entry that is malformed or a hosted API.

        Validating the allowlist itself is the point: the deny layer applies to what a
        deployment declares approved, not only to what a model binds to, so a hosted
        provider cannot be smuggled in by widening the list.
        """
        approved: list[Endpoint] = []
        for url in urls:
            endpoint = Endpoint.parse(url)
            if endpoint.is_hosted_provider:
                raise NonApprovedEndpoint(
                    f"{endpoint} is a hosted provider API and cannot be an approved endpoint (I15)"
                )
            approved.append(endpoint)
        return cls(tuple(approved))

    def admits(self, url: str) -> bool:
        """True when this base URL is covered by an approved endpoint. Malformed raises."""
        candidate = Endpoint.parse(url)
        if candidate.is_hosted_provider:
            return False
        return any(endpoint.covers(candidate) for endpoint in self.approved)
