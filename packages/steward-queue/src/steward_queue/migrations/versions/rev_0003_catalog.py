"""catalog: sources, assets, columns

The catalog slice of SPEC.md §7's data model (issue #20). Postgres is the only
system of record (I1), so the three entities a metadata scan produces live
here, in the same Alembic tree as the queue -- one `upgrade_to_head` brings a
deployment fully up, and a catalog write and its audit row are in the same
database and therefore in the same transaction (I7).

Design notes worth keeping:

* **Natural keys are unique indexes, not conventions.** `sources` is unique on
  (workspace, engine, host, database, schema filter), `assets` on
  (source, schema, name), `columns` on (asset, name). Re-registering a source
  or rescanning a database therefore converges on the existing row by
  construction: idempotency is the database's guarantee, not the writer's (I8).
* **The schema filter is part of the source's identity.** Two registrations of
  the same database with different filters are two sources, because they
  describe two different subsets of it. Both arrays are stored sorted and
  deduplicated by the writer so the key is canonical.
* **A source row cannot hold a credential.** `dsn_secret_ref` carries a CHECK
  that admits `scheme:name` references only, which every DSN shape fails --
  `postgresql://user:pw@host/db` contains `/` and `@`. N7 ("no credentials in
  git", and none in the database either) is thereby enforced by the database
  rather than by the discipline of whoever writes the next INSERT.
* **Lifecycle, never deletion.** A table that disappears upstream becomes
  `missing`; the row stays (ARCHITECTURE.md §4, append-only stewardship
  history). Columns carry the same lifecycle for the same reason.
* **No `last_seen_at`.** A timestamp touched by every scan would make "rescan
  with no upstream change leaves byte-identical state" false by construction.
  What was seen when is what the audit log records.

DDL is static SQL executed verbatim (I5); it is not assembled from strings.

Revision ID: 0003_catalog
Revises: 0002_run_identity
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_catalog"
down_revision: str | None = "0002_run_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SOURCES = """
CREATE TABLE sources (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    engine text NOT NULL CHECK (engine IN ('postgres', 'mysql', 'snowflake')),
    host text NOT NULL,
    database_name text NOT NULL,
    include_schemas text[] NOT NULL,
    exclude_schemas text[] NOT NULL,
    dsn_secret_ref text NOT NULL CHECK (dsn_secret_ref ~ '^[a-z][a-z0-9_]*:[A-Za-z0-9_.-]+$'),
    scan_schedule text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_SOURCES_NATURAL_KEY = """
CREATE UNIQUE INDEX sources_natural_key
ON sources (workspace_id, engine, host, database_name, include_schemas, exclude_schemas)
"""

CREATE_ASSETS = """
CREATE TABLE assets (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    schema_name text NOT NULL,
    name text NOT NULL,
    asset_type text NOT NULL CHECK (asset_type IN ('table', 'view')),
    lifecycle text NOT NULL CHECK (lifecycle IN ('active', 'missing', 'deprecated')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_ASSETS_NATURAL_KEY = """
CREATE UNIQUE INDEX assets_natural_key ON assets (source_id, schema_name, name)
"""

# The keyset the assets listing pages on (SPEC.md §8: cursor pagination). The
# order is total -- id breaks ties a duplicate (schema, name) across sources
# would otherwise leave -- so a cursor can never skip or repeat a row.
CREATE_ASSETS_CURSOR = """
CREATE INDEX assets_cursor ON assets (schema_name, name, id)
"""

CREATE_COLUMNS = """
CREATE TABLE columns (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    asset_id uuid NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
    name text NOT NULL,
    data_type text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    nullable boolean NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('active', 'missing', 'deprecated')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_COLUMNS_NATURAL_KEY = """
CREATE UNIQUE INDEX columns_natural_key ON columns (asset_id, name)
"""

UPGRADE: tuple[str, ...] = (
    CREATE_SOURCES,
    CREATE_SOURCES_NATURAL_KEY,
    CREATE_ASSETS,
    CREATE_ASSETS_NATURAL_KEY,
    CREATE_ASSETS_CURSOR,
    CREATE_COLUMNS,
    CREATE_COLUMNS_NATURAL_KEY,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE columns",
    "DROP TABLE assets",
    "DROP TABLE sources",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
