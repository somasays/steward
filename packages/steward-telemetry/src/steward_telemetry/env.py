"""Wiring: which `Tracer` this process gets, decided from the environment.

One decision, made in one place, so no component has to ask "is tracing
configured?" -- they all take a `Tracer` and use it. Credentials are read from
the environment rather than committed anywhere (N7).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from steward_telemetry._langfuse import LangfuseTracer
from steward_telemetry.tracer import NoopTracer, Tracer

PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"
HOST_ENV = "LANGFUSE_HOST"


def tracer_from_env(env: Mapping[str, str] | None = None) -> Tracer:
    """`LangfuseTracer` when both keys are set, `NoopTracer` otherwise.

    Both keys are required because either alone cannot authenticate, and a
    tracer that fails every export is worse than an honest no-op. The fallback
    is not a degraded mode of the system -- the run, its trace id, and its
    audit trail are identical either way; only span export is missing (I7's
    graceful-degradation half). `env` is injectable so wiring is testable
    without mutating the process environment.
    """
    source = os.environ if env is None else env
    public_key = source.get(PUBLIC_KEY_ENV, "").strip()
    secret_key = source.get(SECRET_KEY_ENV, "").strip()
    if not public_key or not secret_key:
        return NoopTracer()
    return LangfuseTracer.from_credentials(
        public_key=public_key,
        secret_key=secret_key,
        host=source.get(HOST_ENV, "").strip() or None,
    )
