"""Source — a registered, read-only connection to a data store (SPEC.md §7)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from steward_schemas._base import SchemaModel


class SourceEngine(StrEnum):
    """Database engines Steward can connect to (SPEC.md §2 system diagram)."""

    POSTGRES = "postgres"
    MYSQL = "mysql"
    SNOWFLAKE = "snowflake"


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
