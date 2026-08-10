"""steward-queue: the Postgres task queue (SPEC.md §3.1, decision D2).

Postgres is the queue because the property that matters is not throughput but
**transactional enqueue**: a task and the state change that caused it commit
atomically, which removes ghost tasks and lost tasks as a category (I8). Every
function here takes the caller's connection and never commits it, so that
guarantee is structural rather than a convention reviewers have to police.

The moving parts, one module per aggregate behind this façade:

* `tasks` -- enqueue, claim (`FOR UPDATE SKIP LOCKED`), complete, fail with
  exponential backoff and dead-lettering, and lease recovery.
* `runs` -- creation with an idempotency key, status, and spend against the
  run's budget.
* `checkpoints` -- agent state persisted between steps.
* `audit` -- the audit row each of those writes on the mutation's own
  connection, inside the mutation's transaction (I7).
* `registry` -- task type -> handler, with the contract handlers sign
  (idempotence, sample payload, state probe). The H1 harness iterates it, so
  new handlers are leashed on registration.
* `worker` -- a minimal asyncio loop that claims and dispatches, opening a task
  span on the run's trace around every execution (I7). No LLM.
* `execution` -- the mechanism the worker dispatches through: the handler's own
  thread and connection, its wall-clock deadline, and the handoff that decides
  which context records the attempt (SPEC.md §13, D7).

A run's status follows its tasks: `pending` until one starts, `running` while
any is in flight, and `succeeded`/`failed` the moment the last one settles --
decided in the same transaction as the task transition that caused it, so
there is no window where a finished run still reads as running.

Schema lives in `migrations`; `migrate.upgrade_to_head` applies it.
"""

from steward_queue.audit import write_audit
from steward_queue.backoff import retry_delay
from steward_queue.checkpoints import StaleClaim, guard_claim, latest_checkpoint, write_checkpoint
from steward_queue.db import DSN_ENV, QueueConnection, connect, statement_timeout_ms
from steward_queue.handlers import NOOP_TASK_TYPE
from steward_queue.keys import canonical_json, digest
from steward_queue.migrate import downgrade_to_base, upgrade_to_head
from steward_queue.models import (
    SYSTEM_ACTOR,
    Actor,
    ActorKind,
    ClaimedTask,
    RunRecord,
    RunStatus,
    TaskRecord,
    TaskState,
)
from steward_queue.registry import (
    REGISTRY,
    HandlerRegistration,
    StateProbe,
    TaskContext,
    TaskHandler,
    UnknownTaskType,
    default_state_probe,
    get_handler,
    registered_types,
    task_handler,
)
from steward_queue.runs import (
    bind_idempotency_key,
    claim_single_flight,
    create_run,
    get_run,
    record_step_usage,
    rollup_run_status,
    set_run_status,
    start_run,
)
from steward_queue.tasks import (
    DEFAULT_LEASE,
    TaskNotClaimable,
    claim,
    complete,
    dedup_key_for,
    enqueue,
    fail,
    get_task,
    mark_running,
    requeue_stale,
)
from steward_queue.usage import NOTHING_SPENT, UsageLedger
from steward_queue.worker import Worker

__all__ = [
    "DEFAULT_LEASE",
    "DSN_ENV",
    "NOOP_TASK_TYPE",
    "REGISTRY",
    "SYSTEM_ACTOR",
    "Actor",
    "ActorKind",
    "ClaimedTask",
    "HandlerRegistration",
    "QueueConnection",
    "RunRecord",
    "RunStatus",
    "StateProbe",
    "NOTHING_SPENT",
    "TaskContext",
    "UsageLedger",
    "TaskHandler",
    "TaskNotClaimable",
    "TaskRecord",
    "TaskState",
    "UnknownTaskType",
    "Worker",
    "bind_idempotency_key",
    "canonical_json",
    "claim",
    "claim_single_flight",
    "complete",
    "connect",
    "create_run",
    "dedup_key_for",
    "digest",
    "default_state_probe",
    "downgrade_to_base",
    "enqueue",
    "fail",
    "get_handler",
    "get_run",
    "record_step_usage",
    "get_task",
    "mark_running",
    "registered_types",
    "requeue_stale",
    "retry_delay",
    "rollup_run_status",
    "set_run_status",
    "start_run",
    "statement_timeout_ms",
    "task_handler",
    "upgrade_to_head",
    "write_audit",
    "StaleClaim",
    "guard_claim",
    "latest_checkpoint",
    "write_checkpoint",
]
