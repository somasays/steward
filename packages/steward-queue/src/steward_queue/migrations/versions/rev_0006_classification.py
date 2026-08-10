"""classification proposals and their reviews: append-only, review-gated

The persistence half of #50. Two tables, and the shape of each is an argument.

* **`classification_proposals` is versioned per asset and never updated in
  place** — except for `status`, which is the one mutable column and moves only
  through the transitions below. A proposal is what a specific prompt said
  about a specific profile; re-running against a new profile writes a new
  version and supersedes the old one rather than editing it, so "what did this
  column say in March" stays a query (ARCHITECTURE.md §4, the same append-only
  stewardship history `profiles` has).
* **`classification_reviews` is pure append.** A decision is an event: it
  happened, by whom, under which policy, for what reason. Correcting a review
  means recording another one, because a review table you can edit is an audit
  trail that cannot be trusted (I7).

Two uniqueness constraints carry behaviour rather than tidiness:

* `(asset_id, version)` makes the version sequence per asset real, so two
  concurrent classifiers cannot both write version 4 -- one loses the INSERT
  and its transaction fails, instead of the history forking (I8).
* `(asset_id, profile_version, prompt_version, model_alias)` is the convergence
  key #50 asks for: the same effective request cannot create a second
  publishable proposal. Repeating it finds the existing row.

And one partial index carries the publication invariant: **at most one approved
proposal per asset at a time.** A run that approves a second one while a first
is still approved fails at the index rather than leaving two rows both claiming
to be current -- which is the state an API would have to pick between, and
picking is how a "current classification" becomes a matter of ordering.

DDL is static SQL executed verbatim (I5); it is not assembled from strings.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_classification"
down_revision: str | None = "0005_task_usage"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

CREATE_PROPOSALS = """
CREATE TABLE classification_proposals (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    asset_id uuid NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    profile_version integer NOT NULL CHECK (profile_version > 0),
    prompt_version text NOT NULL,
    model_alias text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('pending_review', 'approved', 'rejected', 'superseded')),
    proposal jsonb NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    trace_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_PROPOSAL_VERSION = """
CREATE UNIQUE INDEX classification_proposals_asset_version
    ON classification_proposals (asset_id, version DESC)
"""

# The convergence key (#50): one proposal per (asset, profile, prompt, model).
CREATE_PROPOSAL_CONVERGENCE = """
CREATE UNIQUE INDEX classification_proposals_effective_request
    ON classification_proposals (asset_id, profile_version, prompt_version, model_alias)
"""

# At most one approved proposal per asset. Partial, because rejected and
# superseded rows accumulate freely -- it is only "currently published" that
# must be singular.
CREATE_ONE_APPROVED = """
CREATE UNIQUE INDEX classification_proposals_one_approved
    ON classification_proposals (asset_id) WHERE status = 'approved'
"""

CREATE_REVIEWS = """
CREATE TABLE classification_reviews (
    id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL REFERENCES classification_proposals (id) ON DELETE CASCADE,
    outcome text NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    actor text NOT NULL,
    reason text NOT NULL,
    policy_id text,
    decided_at timestamptz NOT NULL DEFAULT now()
)
"""

CREATE_REVIEWS_BY_PROPOSAL = """
CREATE INDEX classification_reviews_proposal ON classification_reviews (proposal_id, decided_at)
"""

UPGRADE: tuple[str, ...] = (
    CREATE_PROPOSALS,
    CREATE_PROPOSAL_VERSION,
    CREATE_PROPOSAL_CONVERGENCE,
    CREATE_ONE_APPROVED,
    CREATE_REVIEWS,
    CREATE_REVIEWS_BY_PROPOSAL,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE classification_reviews",
    "DROP TABLE classification_proposals",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
