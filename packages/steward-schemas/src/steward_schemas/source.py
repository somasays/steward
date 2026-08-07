"""Source — a registered, read-only connection to a data store (SPEC.md §7)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from steward_schemas._base import SchemaModel

# The shape of a secret reference: `scheme:name`, and nothing else.
#
# Published here, on the contract, because this is the outermost place the rule
# can be enforced. A DSN fails it — `postgresql://u:p@h/db` carries `/` and `@`
# — so a client that posts a credential is rejected at the boundary with a 422
# naming only the field.
#
# That matters more than it looks. The same rule is a CHECK constraint on
# `sources.dsn_secret_ref` (migration `0003_catalog`), and if the boundary let a
# DSN through, the database would be the thing to reject it — raising a
# `CheckViolation` whose message quotes the failing row, credential included,
# into the API's error log (N7). Defence in depth only works when the outer
# layer holds; this constant is that layer, and `steward_catalog.secrets`
# compiles this exact string rather than a second copy of it.
SECRET_REF_PATTERN = r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.-]+$"

DEFAULT_EXCLUDED_SCHEMAS: tuple[str, ...] = ("information_schema", "pg_catalog")
"""Schemas a scan skips unless the registration says otherwise.

The engine's own metadata, which is Steward's *mechanism* for discovery rather
than part of the estate being cataloged. Published here, on the contract, so
the default a client gets is documented rather than buried in a scanner.
"""


class SourceEngine(StrEnum):
    """Database engines Steward can connect to (SPEC.md §2 system diagram)."""

    POSTGRES = "postgres"
    MYSQL = "mysql"
    SNOWFLAKE = "snowflake"


class SourceCreate(SchemaModel):
    """`POST /v1/sources` request body (SPEC.md §8, issue #20).

    Carries a **secret reference**, never a DSN: the credential lives in the
    deployment's secret store and is resolved at connection time, so it is
    readable from neither the database nor any API response (I5, N7).

    `host`, `database` and the schema filter are not decoration -- together with
    `engine` they are the source's natural key, which is what makes
    registration idempotent. Two registrations of the same database under
    different filters are two sources, because they describe two different
    subsets of it.

    An empty `include_schemas` means "every schema except the excluded ones",
    so schemas created upstream *after* registration are picked up by the next
    scan. A non-empty `include_schemas` is a closed allowlist: new schemas are
    never scanned until the allowlist is changed, which is the point of asking
    for one.
    """

    name: str
    engine: SourceEngine
    host: str
    database: str
    dsn_secret_ref: str = Field(pattern=SECRET_REF_PATTERN)
    """A `scheme:name` reference. Constrained here so that posting a DSN is a
    422 at the boundary rather than a database CHECK violation whose message
    would carry the credential into the API's log (N7)."""

    include_schemas: tuple[str, ...] = ()
    exclude_schemas: tuple[str, ...] = Field(default=DEFAULT_EXCLUDED_SCHEMAS)
    scan_schedule: str | None = None


class Source(SchemaModel):
    """A connection registration. Never carries a raw DSN or credential
    (I5, N7, I4's neighbor "no credentials in git") — only a reference into
    the secret store the deployment configures.
    """

    id: UUID
    workspace_id: UUID
    name: str
    engine: SourceEngine
    dsn_secret_ref: str
    """Opaque reference into the secret store; resolved at connection time,
    never embedded here or logged."""

    scan_schedule: str | None = None
    """Cron expression for periodic scans; None means scan-on-demand only."""

    created_at: datetime
    updated_at: datetime
