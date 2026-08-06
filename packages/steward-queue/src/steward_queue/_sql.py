"""Every statement the queue runs, as a module-level constant.

I5: SQL is never assembled from strings. Nothing here is an f-string, a
`.format()`, a `%`-format, or a concatenation — the only variability is
server-side parameter binding (`%(name)s` placeholders, bound by psycopg).
That is also what keeps ruff S608 (S3) quiet without a single pragma.

Private module on purpose: SQL text is an implementation detail of
`steward_queue.queue`, not part of this package's public surface.
"""

INSERT_RUN = """
INSERT INTO runs (id, goal, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
                  trace_id)
VALUES (%(id)s, %(goal)s, %(status)s, %(budget_steps)s, %(budget_tokens)s, %(budget_cost_usd)s,
        %(budget_wall_clock)s, %(trace_id)s)
RETURNING id, goal, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
          used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, created_at, updated_at
"""

SELECT_RUN = """
SELECT id, goal, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
       used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, created_at, updated_at
FROM runs
WHERE id = %(id)s
"""

UPDATE_RUN_STATUS = """
UPDATE runs SET status = %(status)s, updated_at = now() WHERE id = %(id)s RETURNING status
"""

ADD_RUN_USAGE = """
UPDATE runs
SET used_steps = used_steps + %(steps)s,
    used_tokens = used_tokens + %(tokens)s,
    used_cost_usd = used_cost_usd + %(cost_usd)s,
    used_wall_clock = used_wall_clock + %(wall_clock)s,
    updated_at = now()
WHERE id = %(id)s
"""

INSERT_TASK = """
INSERT INTO tasks (id, run_id, task_type, payload, state, max_attempts, dedup_key,
                   budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock, available_at)
VALUES (%(id)s, %(run_id)s, %(task_type)s, %(payload)s, 'pending', %(max_attempts)s, %(dedup_key)s,
        %(budget_steps)s, %(budget_tokens)s, %(budget_cost_usd)s, %(budget_wall_clock)s,
        COALESCE(%(available_at)s::timestamptz, now()))
ON CONFLICT (run_id, dedup_key) DO NOTHING
RETURNING id
"""

SELECT_TASK_ID_BY_DEDUP = """
SELECT id FROM tasks WHERE run_id = %(run_id)s AND dedup_key = %(dedup_key)s
"""

SELECT_TASK = """
SELECT id, run_id, task_type, state, attempts, max_attempts, dedup_key, claimed_by, claimed_at,
       lease_expires_at, started_at, finished_at, available_at
FROM tasks
WHERE id = %(id)s
"""

SELECT_TASK_ATTEMPTS_FOR_UPDATE = """
SELECT state, attempts, max_attempts FROM tasks WHERE id = %(id)s FOR UPDATE
"""

SELECT_TASK_RESULT = """
SELECT result FROM tasks WHERE id = %(id)s
"""

SELECT_CHECKPOINTS = """
SELECT step, state FROM checkpoints WHERE task_id = %(task_id)s ORDER BY step
"""

# The claim. `FOR UPDATE SKIP LOCKED` inside the CTE is what makes concurrent
# workers disjoint: a row another transaction already locked is skipped rather
# than waited on, so two workers never hand the same task to two handlers
# (SPEC.md §3.1, decision D2).
CLAIM_TASKS = """
WITH claimable AS (
    SELECT id
    FROM tasks
    WHERE state = 'pending'
      AND available_at <= now()
      AND (%(task_types)s::text[] IS NULL OR task_type = ANY (%(task_types)s::text[]))
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT %(limit)s
)
UPDATE tasks AS t
SET state = 'claimed',
    attempts = t.attempts + 1,
    claimed_by = %(worker_id)s,
    claimed_at = now(),
    lease_expires_at = now() + %(lease)s,
    updated_at = now()
FROM claimable AS c
WHERE t.id = c.id
RETURNING t.id, t.run_id, t.task_type, t.payload, t.attempts, t.max_attempts,
          t.budget_steps, t.budget_tokens, t.budget_cost_usd, t.budget_wall_clock,
          t.claimed_by, t.lease_expires_at
"""

MARK_RUNNING = """
UPDATE tasks
SET state = 'running',
    started_at = COALESCE(started_at, now()),
    lease_expires_at = now() + %(lease)s,
    updated_at = now()
WHERE id = %(id)s AND state = 'claimed'
RETURNING id, run_id
"""

# The `previous` CTE is how the audit row gets a truthful `before` state:
# `UPDATE ... RETURNING` yields post-update values, and an audit trail that
# reports the state it just wrote as the state it replaced is worse than none.
COMPLETE_TASK = """
WITH previous AS (
    SELECT id, state FROM tasks WHERE id = %(id)s
)
UPDATE tasks AS t
SET state = 'succeeded',
    result = %(result)s,
    last_error = NULL,
    finished_at = now(),
    lease_expires_at = NULL,
    updated_at = now()
FROM previous AS p
WHERE t.id = p.id AND t.state IN ('claimed', 'running')
RETURNING t.run_id, p.state
"""

RETRY_TASK = """
UPDATE tasks
SET state = 'pending',
    available_at = now() + %(delay)s,
    claimed_by = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    last_error = %(error)s,
    updated_at = now()
WHERE id = %(id)s AND state IN ('claimed', 'running')
RETURNING run_id
"""

TERMINATE_TASK = """
UPDATE tasks
SET state = %(state)s,
    claimed_by = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    finished_at = now(),
    last_error = %(error)s,
    updated_at = now()
WHERE id = %(id)s AND state IN ('claimed', 'running')
RETURNING run_id
"""

# Crash recovery (N1, H3): a worker that dies between claiming and finishing
# leaves a row holding an expired lease. Returning it to `pending` -- or to
# `dead` when its attempts are spent -- is what makes "no task is ever lost"
# true without a distributed lock.
REQUEUE_STALE = """
UPDATE tasks
SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
    claimed_by = NULL,
    claimed_at = NULL,
    lease_expires_at = NULL,
    available_at = CASE WHEN attempts >= max_attempts THEN available_at ELSE now() END,
    finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE finished_at END,
    updated_at = now()
WHERE state IN ('claimed', 'running')
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < now()
RETURNING id, state
"""

UPSERT_CHECKPOINT = """
INSERT INTO checkpoints (task_id, step, state)
VALUES (%(task_id)s, %(step)s, %(state)s)
ON CONFLICT (task_id, step) DO UPDATE SET state = EXCLUDED.state, updated_at = now()
"""

INSERT_AUDIT = """
INSERT INTO audit_log (actor_kind, actor_id, action, entity_type, entity_id, before, after)
VALUES (%(actor_kind)s, %(actor_id)s, %(action)s, %(entity_type)s, %(entity_id)s, %(before)s, %(after)s)
"""

COUNT_AUDIT_FOR_ENTITY = """
SELECT count(*) FROM audit_log WHERE entity_type = %(entity_type)s AND entity_id = %(entity_id)s
"""
