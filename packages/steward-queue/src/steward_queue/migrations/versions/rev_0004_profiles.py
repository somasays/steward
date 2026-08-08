"""profiles: versioned, append-only column statistics

The profiling slice of SPEC.md §7's data model (issue #49). One row is one
*version* of one asset's profile; nothing here is ever updated in place, which
is what makes "what did this table look like in March" a query rather than a
regret (ARCHITECTURE.md §4, append-only stewardship history).

Design notes worth keeping:

* **Version is the identity, and it is unique per asset.** `(asset_id, version)`
  is a unique index, so two profilers racing on one asset cannot both write
  version 4 -- one loses the INSERT and its transaction fails, rather than the
  history growing a fork nothing can order (I8).
* **The digest is what makes re-profiling converge.** A profile is written only
  when its digest differs from the latest stored one, so profiling unchanged
  data twice leaves byte-identical state -- no new version, no audit row, no
  timestamp moved. The column exists so that comparison is an indexed integer's
  worth of work rather than a JSONB diff, and so the *stored* row states what
  it was compared on.
* **The profile itself is JSONB.** `steward_schemas.TableProfile` is the shape;
  keeping it in one column means a profile can grow a field (#50's
  classification evidence, #51's documentation hooks) without a migration, while
  the row around it -- identity, version, digest -- stays fixed. Every value
  inside it is masked before it gets here (I6); nothing in this table is a raw
  customer value.
* **No `updated_at`.** There is no update. A column that could only ever hold
  `created_at`'s value would invite one.

DDL is static SQL executed verbatim (I5); it is not assembled from strings.

Revision ID: 0004_profiles
Revises: 0003_catalog
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_profiles"
down_revision: str | None = "0003_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_PROFILES = """
CREATE TABLE profiles (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    asset_id uuid NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    digest text NOT NULL,
    profile jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

# The append-only key. Also the read path: the latest version of an asset's
# profile is the first row of this index scanned backwards, which is the
# "latest pointer" SPEC.md §7 asks for without a mutable pointer column that a
# concurrent writer could leave pointing at the wrong version.
CREATE_PROFILES_VERSION = """
CREATE UNIQUE INDEX profiles_asset_version ON profiles (asset_id, version DESC)
"""

UPGRADE: tuple[str, ...] = (CREATE_PROFILES, CREATE_PROFILES_VERSION)

DOWNGRADE: tuple[str, ...] = ("DROP TABLE profiles",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
