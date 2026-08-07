"""Secret references resolve; secrets do not leak (N7)."""

from __future__ import annotations

import pytest
from steward_catalog import EnvSecretResolver, MalformedSecretRef, Secret, SecretNotFound

A_DSN = "postgresql://steward_reader@db.internal:5432/analytics"


def test_a_reference_resolves_to_the_environment_value() -> None:
    resolver = EnvSecretResolver(environ={"STEWARD_SOURCE_DSN": A_DSN})

    assert resolver.resolve("env:STEWARD_SOURCE_DSN").reveal() == A_DSN


def test_a_secret_redacts_itself_everywhere_it_could_be_printed() -> None:
    """The leak paths are `repr` (tracebacks, logs, pytest output) and `str`
    (f-strings). A secret has to be inert in both or the seam is decorative."""
    secret = Secret(A_DSN)

    assert A_DSN not in repr(secret)
    assert A_DSN not in str(secret)
    assert A_DSN not in f"{secret}"
    assert A_DSN not in repr([secret])  # containers print their items with repr
    assert secret.reveal() == A_DSN


def test_a_missing_secret_is_a_typed_failure_naming_only_the_reference() -> None:
    resolver = EnvSecretResolver(environ={})

    with pytest.raises(SecretNotFound) as raised:
        resolver.resolve("env:STEWARD_SOURCE_DSN")

    assert raised.value.ref == "env:STEWARD_SOURCE_DSN"


def test_an_empty_environment_variable_is_not_a_secret() -> None:
    # A blank value is a misconfiguration, and treating it as a credential
    # would hand libpq an empty DSN and produce a confusing connection error.
    resolver = EnvSecretResolver(environ={"STEWARD_SOURCE_DSN": "   "})

    with pytest.raises(SecretNotFound):
        resolver.resolve("env:STEWARD_SOURCE_DSN")


@pytest.mark.parametrize(
    "ref",
    [A_DSN, "STEWARD_SOURCE_DSN", "env:", "ENV:NAME", "env:has spaces"],
    ids=["a-dsn", "no-scheme", "no-name", "uppercase-scheme", "illegal-name"],
)
def test_a_reference_that_is_not_scheme_name_is_rejected(ref: str) -> None:
    """The same grammar the `sources.dsn_secret_ref` CHECK enforces, so a
    reference that survived a write is one this resolver can parse."""
    with pytest.raises(MalformedSecretRef):
        EnvSecretResolver(environ={}).resolve(ref)


def test_an_unsupported_scheme_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(MalformedSecretRef, match="unsupported secret scheme"):
        EnvSecretResolver(environ={}).resolve("vault:sources-warehouse")


def test_the_default_resolver_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_SOURCE_DSN", A_DSN)

    assert EnvSecretResolver().resolve("env:STEWARD_SOURCE_DSN").reveal() == A_DSN
