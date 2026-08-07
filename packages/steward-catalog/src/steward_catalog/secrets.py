"""Secret references, and the seam that turns one into a credential.

A source row stores a **reference** — `env:STEWARD_SOURCE_DSN_WAREHOUSE` — and
never a DSN. Resolution happens once, at connection time, through a resolver the
caller injects. Three properties fall out of that and each is deliberate:

* **The database cannot hold a credential.** `sources.dsn_secret_ref` carries a
  CHECK that admits `scheme:name` and nothing else, so a DSN cannot be written
  there even by a future caller who forgets (migration `0003_catalog`, N7).
* **A reference cannot be mistaken for a credential.** `resolve` returns
  `Secret`, and the connector takes `Secret`, not `str` — so handing psycopg a
  reference (or a raw DSN read off a row) does not type-check (I3). The
  conversion has exactly one implementation and it is this module.
* **A credential cannot be logged by accident.** `Secret` redacts itself in
  `repr` and `str`, which is what a traceback, a log line and an f-string all
  reach for. `reveal()` is the only way to the characters, and it is called in
  exactly one place.

`EnvSecretResolver` is M1's implementation: the deployment puts the DSN in an
environment variable and the reference names it. A Vault/KMS resolver is another
implementation of the same Protocol and no caller changes (N9).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from steward_schemas import SECRET_REF_PATTERN

__all__ = [
    "ENV_SCHEME",
    "SECRET_REF",
    "EnvSecretResolver",
    "MalformedSecretRef",
    "Secret",
    "SecretNotFound",
    "SecretResolver",
]

SECRET_REF = re.compile(SECRET_REF_PATTERN)
"""`scheme:name`, compiled from the contract's own pattern -- not restated.

`steward_schemas.SECRET_REF_PATTERN` is the single authority: the
`POST /v1/sources` contract validates against it, the `sources.dsn_secret_ref`
CHECK enforces it in the database, and this module parses with it. Three
enforcement points, one rule -- so a reference that survived a write is always
one this module can take apart, and a DSN is rejected at the outermost of the
three, before it can reach a log (N7).
"""

ENV_SCHEME = "env"
"""The only scheme M1 resolves: the name is an environment variable."""

REDACTED = "Secret(***)"


@dataclass(frozen=True, slots=True, repr=False)
class Secret:
    """A resolved credential. Prints as `Secret(***)` wherever it is printed.

    Not a `str` subclass on purpose: subclassing would make it substitutable
    for the reference it replaces and would inherit every `str` formatting path
    that leaks. This is opaque, and `reveal()` is the deliberate way out.
    """

    _value: str

    def reveal(self) -> str:
        """The credential itself. Called only by the code opening a connection."""
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED


class SecretNotFound(LookupError):
    """A well-formed reference names a secret the store does not hold.

    Carries the reference, never the surrounding state: the reference is safe
    to log by construction, which is the point of storing one.
    """

    def __init__(self, ref: str) -> None:
        super().__init__(f"no secret for reference {ref!r}")
        self.ref = ref


class MalformedSecretRef(ValueError):
    """A reference is not `scheme:name`, or names a scheme nothing resolves."""

    def __init__(self, ref: str, reason: str) -> None:
        super().__init__(f"{reason}: {ref!r}")
        self.ref = ref


class SecretResolver(Protocol):
    """Reference -> credential. The only path from a source row to a connection."""

    def resolve(self, ref: str) -> Secret:
        """The secret `ref` names.

        Raises `MalformedSecretRef` if `ref` is not a reference this resolver
        understands, and `SecretNotFound` if it is one but the store has no
        value for it.
        """
        ...


@dataclass(frozen=True, slots=True)
class EnvSecretResolver:
    """Environment-backed resolution: `env:NAME` -> `os.environ["NAME"]`.

    Indirection through the environment is enough for M1 and is what a
    Kubernetes secret mount looks like from inside a process anyway; the value
    of the seam is that swapping in Vault is a wiring change (N9), not that
    this implementation is clever.

    `environ` is injectable so a test can exercise resolution without mutating
    process state, which is otherwise shared between tests running in one
    session.
    """

    environ: Mapping[str, str] | None = None

    def resolve(self, ref: str) -> Secret:
        if SECRET_REF.match(ref) is None:
            raise MalformedSecretRef(ref, "not a scheme:name secret reference")
        scheme, _, name = ref.partition(":")
        if scheme != ENV_SCHEME:
            raise MalformedSecretRef(ref, f"unsupported secret scheme {scheme!r}")
        source = os.environ if self.environ is None else self.environ
        value = source.get(name, "").strip()
        if not value:
            raise SecretNotFound(ref)
        return Secret(value)
