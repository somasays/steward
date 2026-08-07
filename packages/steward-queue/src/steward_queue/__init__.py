"""steward-queue: the Postgres task queue (SPEC.md §3.1, decision D2).

Postgres is the queue because the property that matters is not throughput but
**transactional enqueue**: a task and the state change that caused it commit
atomically, which removes ghost tasks and lost tasks as a category (I8). Every
function here takes the caller's connection and never commits it, so that
guarantee is structural rather than a convention reviewers have to police.

The three moving parts:

* `queue` -- enqueue, claim (`FOR UPDATE SKIP LOCKED`), complete, fail with
  exponential backoff and dead-lettering, and lease recovery. Each state
  mutation writes its audit row on the same connection (I7).
* `registry` -- task type -> handler, with the contract handlers sign
  (idempotence, sample payload, state probe). The H1 harness iterates it, so
  new handlers are leashed on registration.
* `worker` -- a minimal asyncio loop that claims and dispatches, opening a task
  span on the run's trace around every execution (I7). No LLM.

A run's status follows its tasks: `pending` until one starts, `running` while
any is in flight, and `succeeded`/`failed` the moment the last one settles --
decided in the same transaction as the task transition that caused it, so
there is no window where a finished run still reads as running.

Schema lives in `migrations`; `migrate.upgrade_to_head` applies it.
"""

from steward_queue.backoff import retry_delay
from steward_queue.db import DSN_ENV, QueueConnection, connect
from steward_queue.handlers import NOOP_TASK_TYPE
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
from steward_queue.queue import (
    DEFAULT_LEASE,
    TaskNotClaimable,
    claim,
    complete,
    create_run,
    dedup_key_for,
    enqueue,
    fail,
    get_run,
    get_task,
    mark_running,
    requeue_stale,
    rollup_run_status,
    set_run_status,
    start_run,
    write_checkpoint,
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
    "TaskContext",
    "TaskHandler",
    "TaskNotClaimable",
    "TaskRecord",
    "TaskState",
    "UnknownTaskType",
    "Worker",
    "claim",
    "complete",
    "connect",
    "create_run",
    "dedup_key_for",
    "default_state_probe",
    "downgrade_to_base",
    "enqueue",
    "fail",
    "get_handler",
    "get_run",
    "get_task",
    "mark_running",
    "registered_types",
    "requeue_stale",
    "retry_delay",
    "rollup_run_status",
    "set_run_status",
    "start_run",
    "task_handler",
    "upgrade_to_head",
    "write_checkpoint",
]
