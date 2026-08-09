"""Every statement the queue runs, as a module-level constant.

I5: SQL is never assembled from strings. Nothing here is an f-string, a
`.format()`, a `%`-format, or a concatenation — the only variability is
server-side parameter binding (`%(name)s` placeholders, bound by psycopg).
That is also what keeps ruff S608 (S3) quiet without a single pragma.

Private module on purpose: SQL text is an implementation detail of the modules
that run these statements, not part of this package's public surface.
"""

# The `ON CONFLICT` clause is the idempotency-key contract (SPEC.md §8): a
# replayed POST converges on the run the first one created instead of starting a
# second. The index predicate is restated because the index is partial -- runs
# created without a key must not collide with each other on NULL.
INSERT_RUN = """
INSERT INTO runs (id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd,
                  budget_wall_clock, trace_id, idempotency_key)
VALUES (%(id)s, %(goal)s, %(payload)s, %(status)s, %(budget_steps)s, %(budget_tokens)s,
        %(budget_cost_usd)s, %(budget_wall_clock)s, %(trace_id)s, %(idempotency_key)s)
ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
RETURNING id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd,
          budget_wall_clock, used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id,
          idempotency_key, created_at, updated_at
"""

SELECT_RUN = """
SELECT id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
       used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, idempotency_key,
       created_at, updated_at
FROM runs
WHERE id = %(id)s
"""

# Single-flight admission (SPEC.md §8: "a scan already in flight returns that
# run"). Two statements, and both halves are needed.
#
# The lock is transaction-scoped and taken on a hash of (goal, payload), so two
# concurrent requests for the same work serialise: the second waits, then sees
# the run the first committed instead of both finding nothing and both creating.
# It is an advisory lock rather than a unique index because "non-terminal" is a
# moving predicate -- a partial unique index over `status IN (...)` would have to
# name the goal and dig into the payload, which is the queue learning what a
# goal means (I4).
LOCK_RUN_ADMISSION = """
SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))
"""

SELECT_IN_FLIGHT_RUN = """
SELECT id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
       used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, idempotency_key,
       created_at, updated_at
FROM runs
WHERE goal = %(goal)s AND payload = %(payload)s AND status IN ('pending', 'running')
ORDER BY created_at
LIMIT 1
"""

SELECT_RUN_BY_IDEMPOTENCY_KEY = """
SELECT id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
       used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, idempotency_key,
       created_at, updated_at
FROM runs
WHERE idempotency_key = %(idempotency_key)s
"""

# Every writer of `idempotency_key` takes this first -- `create_run`'s INSERT
# and `bind_idempotency_key`'s UPDATE alike -- so two requests racing to claim
# the *same key*, whichever pair of functions they call, serialise on the key
# rather than reaching the unique index concurrently. `INSERT ... ON CONFLICT`
# is race-free against another INSERT on its own, but an UPDATE has no `ON
# CONFLICT` to arbitrate with, so without this lock an INSERT-vs-UPDATE race
# on the same key surfaces a raw constraint violation instead of the typed
# conflict every caller here expects. A different advisory lock domain from
# admission's (salt `1` rather than `0`, `hashtextextended`'s namespacing
# argument), keyed on the idempotency key itself rather than (goal, payload).
LOCK_IDEMPOTENCY_KEY = """
SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 1))
"""

# `idempotency_key IS NULL` in the predicate makes this a no-op -- no row, no
# audit -- when the run already carries this exact key (a second retry while
# still in flight). The `NOT EXISTS` guards the case the first predicate
# alone would not: the *target* run is unbound but some other run already
# holds this key, which would otherwise reach the unique index as a raw
# `UniqueViolation` instead of the typed conflict the caller expects. Called
# only after `LOCK_IDEMPOTENCY_KEY`, which is what makes the guard's read
# race-free -- every other binder of this exact key is serialised behind the
# same lock, so nothing can insert or update the key out from under it.
BIND_IDEMPOTENCY_KEY = """
UPDATE runs
SET idempotency_key = %(idempotency_key)s, updated_at = now()
WHERE id = %(id)s
  AND idempotency_key IS NULL
  AND NOT EXISTS (SELECT 1 FROM runs WHERE idempotency_key = %(idempotency_key)s)
RETURNING id, goal, payload, status, budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock,
          used_steps, used_tokens, used_cost_usd, used_wall_clock, trace_id, idempotency_key,
          created_at, updated_at
"""

# `previous` CTEs throughout: `UPDATE ... RETURNING` yields post-update values,
# and an audit row that reports the state it just wrote as the state it replaced
# is worse than no audit row. Reading the old value in the same statement also
# avoids a read-modify-write window that a concurrent writer could slip into.
UPDATE_RUN_STATUS = """
WITH previous AS (
    SELECT id, status FROM runs WHERE id = %(id)s FOR UPDATE
)
UPDATE runs AS r
SET status = %(status)s, updated_at = now()
FROM previous AS p
WHERE r.id = p.id
RETURNING p.status
"""

# Run status follows task outcomes; nothing else moves it (except an operator
# cancelling).
#
# START_RUN fires when a task first starts. `status = 'pending'` in the predicate
# makes it a no-op for every task after the first, so no serialisation is needed
# beyond the row lock the UPDATE itself takes.
#
# LOCK_RUN + ROLLUP_RUN are the run's terminal state machine, and they are two
# statements for a reason that is easy to get wrong. Under READ COMMITTED a
# statement evaluates against the snapshot it started with; when `FOR UPDATE`
# inside a statement blocks on a concurrent writer, only the *locked* row is
# re-read once the lock is granted (EvalPlanQual) -- an aggregate over `tasks`
# in the same statement keeps the stale snapshot. Two workers settling the last
# two tasks of a run would then both count the other's task as outstanding and
# both decline, leaving the run non-terminal forever with nothing to recover it.
# Taking the lock in its own statement means ROLLUP_RUN begins after the wait
# and therefore reads a snapshot that includes the other worker's committed
# task. It fires only when nothing is outstanding, and `failed` wins over
# `succeeded` because a run that lost a task did not do what it was asked to.
START_RUN = """
UPDATE runs
SET status = 'running', updated_at = now()
WHERE id = %(id)s AND status = 'pending'
RETURNING id
"""

LOCK_RUN = """
SELECT id FROM runs WHERE id = %(id)s FOR UPDATE
"""

ROLLUP_RUN = """
WITH outcome AS (
    SELECT count(*) AS total,
           count(*) FILTER (WHERE state NOT IN ('succeeded', 'failed', 'dead')) AS outstanding,
           count(*) FILTER (WHERE state IN ('failed', 'dead')) AS unsuccessful
    FROM tasks
    WHERE run_id = %(id)s
), previous AS (
    SELECT id, status FROM runs WHERE id = %(id)s
)
UPDATE runs AS r
SET status = CASE WHEN o.unsuccessful > 0 THEN 'failed' ELSE 'succeeded' END,
    updated_at = now()
FROM previous AS p, outcome AS o
WHERE r.id = p.id
  AND p.status IN ('pending', 'running')
  AND o.total > 0
  AND o.outstanding = 0
RETURNING p.status, r.status
"""

ADD_RUN_USAGE = """
WITH previous AS (
    SELECT id, used_steps, used_tokens, used_cost_usd, used_wall_clock
    FROM runs WHERE id = %(id)s FOR UPDATE
)
UPDATE runs AS r
SET used_steps = r.used_steps + %(steps)s,
    used_tokens = r.used_tokens + %(tokens)s,
    used_cost_usd = r.used_cost_usd + %(cost_usd)s,
    used_wall_clock = r.used_wall_clock + %(wall_clock)s,
    updated_at = now()
FROM previous AS p
WHERE r.id = p.id
RETURNING p.used_steps, p.used_tokens, p.used_cost_usd, p.used_wall_clock,
          r.used_steps, r.used_tokens, r.used_cost_usd, r.used_wall_clock
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
SELECT state, attempts, max_attempts, claimed_by, run_id,
       budget_steps, budget_tokens, budget_cost_usd, budget_wall_clock
FROM tasks WHERE id = %(id)s FOR UPDATE
"""
"""The run and the caps come back with the attempt counters because `fail` has
to decide whether the run can still afford a retry before it schedules one, and
reading them in a second statement would be a second chance for the row to move."""

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
FROM claimable AS c, runs AS r
WHERE t.id = c.id AND r.id = t.run_id
RETURNING t.id, t.run_id, t.task_type, t.payload, t.attempts, t.max_attempts,
          t.budget_steps, t.budget_tokens, t.budget_cost_usd, t.budget_wall_clock,
          t.claimed_by, t.lease_expires_at, r.trace_id
"""

# The `claimed_by` predicate is a fencing token: a worker whose lease expired and
# whose task a reaper handed to someone else must not be able to move it. Without
# it, a stalled worker's late `mark_running`/`complete`/`fail` would silently
# stomp the claim of the worker now executing the task.
MARK_RUNNING = """
UPDATE tasks
SET state = 'running',
    started_at = COALESCE(started_at, now()),
    lease_expires_at = now() + %(lease)s,
    updated_at = now()
WHERE id = %(id)s
  AND state = 'claimed'
  AND (%(claimed_by)s::text IS NULL OR claimed_by = %(claimed_by)s::text)
RETURNING id, run_id
"""

COMPLETE_TASK = """
WITH previous AS (
    SELECT id, state FROM tasks WHERE id = %(id)s FOR UPDATE
)
UPDATE tasks AS t
SET state = 'succeeded',
    result = %(result)s,
    last_error = NULL,
    finished_at = now(),
    lease_expires_at = NULL,
    updated_at = now()
FROM previous AS p
WHERE t.id = p.id
  AND t.state IN ('claimed', 'running')
  AND (%(claimed_by)s::text IS NULL OR t.claimed_by = %(claimed_by)s::text)
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
RETURNING id, run_id, state
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

# `set_config` rather than `SET`: the value is a bound derived at runtime, and
# `SET` cannot take a bound parameter -- which would leave string assembly as
# the only way to write it (I5/S3). `is_local = false` so the setting survives
# the transaction it is issued in, which is what lets the worker widen the cap
# back out after rolling a failed attempt back.
SET_STATEMENT_TIMEOUT = """
SELECT set_config('statement_timeout', %(milliseconds)s, false)
"""

# Sent over a *different* connection from the one being ended -- that is the
# point of addressing a backend by pid rather than calling a method on a
# connection object (worker.py, SPEC.md D7). Terminate rather than cancel:
# cancelling only reaches a backend that is running a statement, and the
# session this ends is typically idle inside a transaction it will never
# commit, still holding the locks the worker needs to record the outcome.
# Returns false when the backend has already gone, which is a fine outcome and
# not an error.
TERMINATE_BACKEND = """
SELECT pg_terminate_backend(%(pid)s)
"""

COUNT_AUDIT_FOR_ENTITY = """
SELECT count(*) FROM audit_log WHERE entity_type = %(entity_type)s AND entity_id = %(entity_id)s
"""
