"""Every statement the classification repository runs, as a module constant.

I5: SQL is never assembled from strings. Nothing here is an f-string, a
`.format()`, a `%`-format or a concatenation — the only variability is
server-side parameter binding, which is also what keeps ruff S608 quiet without
a pragma.

Private module on purpose: SQL text is an implementation detail of the module
that runs it, not part of this package's public surface.
"""

# The namespace lock. Approval has to serialise *per asset*, and locking the
# currently approved row cannot do it: on a first approval there is no such row
# to lock, so two first approvals would both find nothing and both promote. The
# lock is therefore taken on the asset id itself, is transaction-scoped, and is
# released when the caller commits or rolls back.
#
# Salt 2 keeps this in its own domain, distinct from run admission (0) and
# idempotency keys (1) in `steward_queue._sql`.
LOCK_ASSET_CLASSIFICATION = """
SELECT pg_advisory_xact_lock(hashtextextended(%(asset_id)s::text, 2))
"""

SELECT_PROPOSAL_FOR_UPDATE = """
SELECT id, asset_id, version, profile_version, prompt_version, model_alias, status,
       proposal, run_id, task_id, trace_id, created_at
FROM classification_proposals
WHERE id = %(id)s
FOR UPDATE
"""

SELECT_APPROVED_PROPOSAL = """
SELECT id, asset_id, version, profile_version, prompt_version, model_alias, status,
       proposal, run_id, task_id, trace_id, created_at
FROM classification_proposals
WHERE asset_id = %(asset_id)s AND status = 'approved'
"""

SELECT_LATEST_PROPOSAL_VERSION = """
SELECT coalesce(max(version), 0) FROM classification_proposals WHERE asset_id = %(asset_id)s
"""

SELECT_PROPOSAL_BY_REQUEST = """
SELECT id, asset_id, version, profile_version, prompt_version, model_alias, status,
       proposal, run_id, task_id, trace_id, created_at
FROM classification_proposals
WHERE asset_id = %(asset_id)s
  AND profile_version = %(profile_version)s
  AND prompt_version = %(prompt_version)s
  AND model_alias = %(model_alias)s
"""

SELECT_PROPOSALS_FOR_ASSET = """
SELECT id, asset_id, version, profile_version, prompt_version, model_alias, status,
       proposal, run_id, task_id, trace_id, created_at
FROM classification_proposals
WHERE asset_id = %(asset_id)s
ORDER BY version DESC
"""

INSERT_PROPOSAL = """
INSERT INTO classification_proposals
    (id, workspace_id, asset_id, version, profile_version, prompt_version, model_alias,
     status, proposal, run_id, task_id, trace_id)
VALUES (%(id)s, %(workspace_id)s, %(asset_id)s, %(version)s, %(profile_version)s,
        %(prompt_version)s, %(model_alias)s, 'pending_review', %(proposal)s, %(run_id)s,
        %(task_id)s, %(trace_id)s)
ON CONFLICT (asset_id, profile_version, prompt_version, model_alias) DO NOTHING
RETURNING id, asset_id, version, profile_version, prompt_version, model_alias, status,
          proposal, run_id, task_id, trace_id, created_at
"""

SET_PROPOSAL_STATUS = """
UPDATE classification_proposals
SET status = %(status)s, updated_at = now()
WHERE id = %(id)s
RETURNING id, asset_id, version, profile_version, prompt_version, model_alias, status,
          proposal, run_id, task_id, trace_id, created_at
"""

INSERT_REVIEW = """
INSERT INTO classification_reviews
    (id, proposal_id, outcome, actor, reason, policy_id, idempotency_key)
VALUES (%(id)s, %(proposal_id)s, %(outcome)s, %(actor)s, %(reason)s, %(policy_id)s,
        %(idempotency_key)s)
ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
RETURNING id, proposal_id, outcome, actor, reason, policy_id, decided_at
"""

SELECT_REVIEW_BY_KEY = """
SELECT id, proposal_id, outcome, actor, reason, policy_id, decided_at
FROM classification_reviews
WHERE idempotency_key = %(idempotency_key)s
"""

SELECT_REVIEWS_FOR_PROPOSAL = """
SELECT id, proposal_id, outcome, actor, reason, policy_id, decided_at
FROM classification_reviews
WHERE proposal_id = %(proposal_id)s
ORDER BY decided_at, id
"""

# The asset must still be active and the cited profile must still be its latest:
# a proposal read from a profile that has since been superseded describes data
# that has changed, and approving it would publish a classification of the past.
SELECT_ASSET_STATE = """
SELECT a.lifecycle, coalesce(max(p.version), 0) AS latest_profile
FROM assets AS a
LEFT JOIN profiles AS p ON p.asset_id = a.id
WHERE a.id = %(asset_id)s
GROUP BY a.lifecycle
"""

# The profile a proposal claims to have read. Evidence is checked against this
# rather than against the proposal's own text: the type can verify a citation is
# self-consistent, but only the stored profile knows whether the column existed.
SELECT_PROFILE_VERSION = """
SELECT profile FROM profiles WHERE asset_id = %(asset_id)s AND version = %(version)s
"""
