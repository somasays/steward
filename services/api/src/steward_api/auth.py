"""API-key authentication (SPEC.md §2: "AuthN via API keys (v1), OIDC (v2)").

Why this landed with the review endpoints and not before
--------------------------------------------------------
Every earlier mutation attributed itself to `API_ACTOR` — a constant, `human`,
id `"api"` — and that was defensible while the mutations were registering a
source and starting a scan: "the API did it, because a person asked it to" is a
true if uninformative statement, and no product claim rested on *which* person.

A review decision is different in kind. `ReviewCommand` deliberately carries no
actor, because a caller must not be able to name one; the repository takes the
actor it is *given* and writes it to both the review row and the audit row, so
that "who approved this classification" has an answer. That guarantee is only
worth as much as the identity handed in at the top. An unauthenticated endpoint
recording `human:api` makes the whole chain — the refusal to accept a
caller-supplied actor, the audit row, the policy-attribution rules — terminate in
a fiction, and "nothing publishes without human review" becomes "nothing
publishes without an HTTP request".

So a decision requires a credential, and the principal it proves becomes the
actor. Reads and the older mutations are unchanged and still unauthenticated;
that is a real gap, stated in SPEC.md §8 and tracked, not one this module
pretends to close.

Three rules worth stating, because each is a way this could be wrong
--------------------------------------------------------------------
* **A key names a human, and only a human.** There is no configuration that
  produces a `policy` principal. SPEC.md §3.3 allows automatic approval only
  through an explicit configured policy, and the repository refuses a policy
  attribution whose id is not the policy actor's own — so a policy key here
  would be a way to record "a policy approved this" from anything holding a
  secret. Auto-approval is a configured policy calling the repository directly.
* **A rejected credential is never described.** "No key" and "not a key we
  accept" are one status and one sentence, because telling them apart tells an
  unauthenticated caller whether a guessed secret exists.
* **Comparison is constant-time and exhaustive.** Every configured secret is
  compared with `hmac.compare_digest` and the loop does not stop early, so
  neither the answer nor the time taken depends on which key matched, or on how
  much of a wrong key was right.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fastapi import Security
from fastapi.security import APIKeyHeader
from steward_queue import Actor, ActorKind

from steward_api.problem_details import API_KEY_HEADER, unauthenticated

__all__ = [
    "API_KEYS_ENV",
    "API_KEY_HEADER",
    "API_KEY_SCHEME_NAME",
    "ApiKeyRegistry",
    "MalformedApiKeys",
    "Principal",
    "authenticator",
]

_logger = logging.getLogger(__name__)

API_KEYS_ENV = "STEWARD_API_KEYS"
"""Where a deployment's credentials come from: `id:secret` pairs, comma-separated.

The composition root reads it, as it reads every other piece of environment. The
value is a secret and is never logged, echoed into a response, or included in an
error — only the *ids* it maps to are, and those are meant to be seen: they are
what an audit row says.
"""


class MalformedApiKeys(ValueError):
    """The configured credentials could not be read as credentials.

    Raised at startup rather than at the first request, and never carrying the
    offending text: a deployment whose key configuration is wrong must fail
    where an operator is looking, and the error must not put a secret in a log
    while doing it.
    """


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, as proven by the credential they presented.

    Deliberately not a `steward_queue.Actor`: an actor is what a mutation is
    *attributed to*, and building one is a decision this type should not make
    silently. `actor` below makes it, once, and says why it is always `human`.
    """

    id: str

    @property
    def actor(self) -> Actor:
        """The actor a decision by this principal is recorded as.

        Always `human`. A credential proves someone holds a secret, and the only
        honest reading of that is "a person, or something a person set up and is
        answerable for". The kinds this deliberately cannot produce are `policy`
        — which would let anything holding a key record an automatic approval,
        the one attribution SPEC.md §3.3 requires to resolve to a configured
        policy — and `agent`, which would let a caller launder a model's decision
        as a reviewed one.
        """
        return Actor(kind=ActorKind.HUMAN, id=self.id)


class ApiKeyRegistry:
    """The credentials this deployment accepts, and nothing else.

    An empty registry is a valid, *fail-closed* configuration: it accepts no
    credential, so every endpoint behind it answers 401. That is the state an API
    started without `STEWARD_API_KEYS` is in, and it is deliberate — a
    deployment that serves reads should not be prevented from starting, and a
    deployment that cannot authenticate anyone must not record decisions on
    anyone's behalf. `configured` exists so a composition root can say so out
    loud at startup instead of leaving it to be discovered by a 401.
    """

    def __init__(self, principals: Mapping[str, str]) -> None:
        """`principals` maps a principal id to that principal's secret."""
        self._secrets: tuple[tuple[str, str], ...] = tuple(principals.items())

    @classmethod
    def from_env(cls, raw: str | None) -> ApiKeyRegistry:
        """Parse `id:secret,id:secret`. An unset or empty value accepts nobody.

        Every failure is refused rather than skipped: a malformed entry that was
        silently dropped would leave a deployment quietly missing one reviewer's
        credential, and a duplicate id or secret would make an audit row
        ambiguous about who acted.
        """
        if raw is None or not raw.strip():
            return cls({})
        principals: dict[str, str] = {}
        seen_secrets: set[str] = set()
        for entry in raw.split(","):
            identifier, separator, secret = entry.strip().partition(":")
            if not separator or not identifier.strip() or not secret.strip():
                raise MalformedApiKeys(
                    f"{API_KEYS_ENV} entries must be 'id:secret'; one entry is not "
                    "(the value is not repeated here because it contains a secret)"
                )
            identifier = identifier.strip()
            secret = secret.strip()
            if identifier in principals:
                raise MalformedApiKeys(f"{API_KEYS_ENV} names principal {identifier!r} twice")
            if secret in seen_secrets:
                raise MalformedApiKeys(
                    f"{API_KEYS_ENV} gives two principals the same secret, so a decision "
                    "by either could not be attributed to one of them"
                )
            seen_secrets.add(secret)
            principals[identifier] = secret
        return cls(principals)

    @property
    def configured(self) -> bool:
        """Whether this registry can authenticate anyone at all."""
        return bool(self._secrets)

    def principal(self, presented: str | None) -> Principal:
        """The principal `presented` proves, or a 401 that describes neither.

        Every configured secret is compared, with `hmac.compare_digest`, and the
        loop does not stop early — so neither the answer nor the work done
        depends on which principal matched or on how much of a wrong secret was
        right. A dict lookup keyed on the secret would be the obvious
        implementation and leaks both.

        **Every rejection takes the same path.** No credential, an empty one, one
        that is not encodable, and an ordinary wrong one all reach the same loop
        and perform the same number of comparisons against the same fixed
        sentinel. An early return for the missing case would have made "you sent
        nothing" measurably cheaper than "you sent something wrong".

        **The conversion to bytes is strict, and that is the point.**
        `compare_digest` raises `TypeError` on a `str` holding any character
        above U+007F, so the comparison has to happen on bytes; but a *lossy*
        conversion is worse than the crash it replaces. Encoding with
        `errors="replace"` mapped every non-Latin-1 character to `?`, so `"ключ"`
        became `b"????"` and authenticated a principal whose configured secret
        was literally `"????"` — distinct credentials collapsing onto one is the
        single thing a credential verifier must never do. Anything that does not
        encode is not a credential this deployment holds, so it is compared
        against the sentinel and refused.

        The encodings on the two sides are deliberately different because the two
        strings did not arrive the same way: an ASGI server decodes header bytes
        as latin-1, so `latin-1` recovers exactly what the client sent, while a
        configured secret came from the environment as UTF-8. For an all-ASCII
        credential — every realistic one — both produce identical bytes.
        """
        candidate, usable = _as_wire_bytes(presented)
        matched: str | None = None
        for identifier, secret in self._secrets:
            if hmac.compare_digest(candidate, secret.encode("utf-8")):
                matched = identifier
        if matched is None or not usable:
            raise unauthenticated(
                f"a valid {API_KEY_HEADER} is required to record a review decision"
            )
        return Principal(id=matched)


NO_CREDENTIAL = b"\x00" * 64
"""What is compared when there is nothing usable to compare.

A fixed value, so the absent, empty and unencodable cases do the same work as a
wrong one rather than skipping the loop. `usable` is tracked separately rather
than trusting this never to collide with a configured secret: a registry built
directly (rather than from the environment, which refuses empty secrets) could
hold anything, and "no credential authenticated somebody" must not be reachable
through a coincidence.
"""


def _as_wire_bytes(presented: str | None) -> tuple[bytes, bool]:
    """The bytes a credential was actually sent as, and whether it is one at all.

    Strict: a string that does not encode is not the credential anyone
    configured, and guessing at what it meant is how two different credentials
    become one.
    """
    if not presented:
        return NO_CREDENTIAL, False
    try:
        return presented.encode("latin-1"), True
    except UnicodeEncodeError:
        return NO_CREDENTIAL, False


API_KEY_SCHEME_NAME = "StewardApiKey"
"""What the published contract calls this credential.

The name a generated client sees, so it is stable and descriptive rather than
FastAPI's default (the class name, `APIKeyHeader`).
"""

API_KEY_SCHEME = APIKeyHeader(
    name=API_KEY_HEADER,
    scheme_name=API_KEY_SCHEME_NAME,
    auto_error=False,
    description=(
        "A credential naming a principal configured in STEWARD_API_KEYS. Required by "
        "the review decision endpoints, which record the principal it proves as the "
        "actor on the decision and its audit row."
    ),
)
"""The credential as a *security scheme*, not as a header parameter.

The distinction is the whole reason this exists rather than a plain `Header`.
Both read the same request header and both produce the same 401 at runtime, but
only this one is published as `securitySchemes` with an operation-level
`security` requirement — and SPEC.md §8 generates the SDK's types from that
document. A credential published as an ordinary optional header describes an
*unsecured* operation with a spare parameter: generated clients offer no place
to configure a key and send none, and every caller discovers the requirement by
receiving a 401.

`auto_error=False` because FastAPI's own error for a missing key is a bare 403
with a `detail` string, which would bypass both the RFC 9457 shape this API
serves and the deliberate indistinguishability of "no key" from "wrong key".
Registering the scheme and owning the failure are separable, and this takes the
first without the second.
"""


def authenticator(registry: ApiKeyRegistry) -> Callable[[str | None], Principal]:
    """A FastAPI dependency that resolves the request's credential.

    A closure over the registry rather than a lookup out of app state, for the
    reason every other seam in this service is a closure: the dependency's own
    dependency is explicit and typed, with no `Any` escape hatch to read
    configuration back out of a request.

    The scheme is a `Security` sub-dependency rather than a `Header` parameter so
    the requirement reaches the published contract; FastAPI propagates a
    sub-dependency's security requirement to every operation that depends on it.
    """

    def authenticate(presented: str | None = Security(API_KEY_SCHEME)) -> Principal:
        return registry.principal(presented)

    return authenticate
