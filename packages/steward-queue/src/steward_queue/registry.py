"""The task-handler registry — and the contract every handler signs.

Workers do not import handlers; they look them up here by task type. The H1
idempotency harness (GUARDRAILS.md Tier H) does the same: it iterates
`REGISTRY` and re-derives its subjects on every run, so a handler added in a
later milestone is on the leash the moment it is registered, with no test file
to remember to edit.

Registry contract
-----------------
Registering a handler is a promise with four clauses:

1. **Signature.** `async def handler(ctx: TaskContext) -> TaskResult`. All
   database work goes through `ctx.connection`, and the handler never commits
   or rolls back: the worker owns the transaction so that the handler's writes,
   the task's terminal state, and the audit row commit together (I7, I8).
   `ctx.connection` belongs to the thread the handler is called on and to
   nothing else -- the worker opens it there, closes it there, and never
   touches it from the event loop (SPEC.md §13, D7). A handler may block on it
   freely; blocking costs it its own budget and no one else's responsiveness.
   What a handler must not do is hand the connection to a thread of its own,
   which would recreate on the inside the sharing this contract removes.
2. **Idempotence.** Executing the handler twice with the same `TaskSpec` must
   leave the same end state. Writes are upserts keyed on natural keys; a
   handler must not read its own side effects to decide what to do next
   (GUARDRAILS.md §4 smell list). Claiming is exactly-once, execution is
   at-least-once (SPEC.md §3.1) -- this clause is what makes that safe.
3. **`sample_payload`.** A payload the handler accepts, valid on its own, with
   no dependency on rows another task created. H1 uses it as the twice-run
   subject; it is the handler author's job to keep it representative.
4. **`state_probe`.** Returns the state this handler owns for a given task, as
   a JSON-comparable value, excluding anything inherently non-repeatable
   (timestamps, generated ids). H1 compares the probe's output across the two
   runs byte for byte, so whatever the probe omits is, by definition, outside
   the handler's idempotency claim. The default probe -- the task's recorded
   result plus its checkpoints -- covers handlers whose only writes are those.
   A handler that writes elsewhere must supply a probe that reads it back, or
   its idempotency is unproven.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from steward_schemas import TaskResult, TaskSpec

from steward_queue import _sql
from steward_queue.db import QueueConnection


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Everything a handler is given: the caller's open transaction and the
    typed spec it must execute. `attempts` is this execution's attempt number
    (1 on the first), so a handler can shed optional work on a retry."""

    connection: QueueConnection
    spec: TaskSpec
    attempts: int


type TaskHandler = Callable[[TaskContext], Awaitable[TaskResult]]
type StateProbe = Callable[[QueueConnection, TaskSpec], object]


def default_state_probe(conn: QueueConnection, spec: TaskSpec) -> object:
    """The recorded result and checkpoints of a task, without timestamps.

    Deliberately timestamp-free: `created_at` differs between two executions by
    construction, and a probe that included it would make H1 unfalsifiable in
    the wrong direction -- always failing, and so eventually always ignored.
    """
    result_row = conn.execute(_sql.SELECT_TASK_RESULT, {"id": spec.task_id}).fetchone()
    checkpoints = conn.execute(_sql.SELECT_CHECKPOINTS, {"task_id": spec.task_id}).fetchall()
    return {
        "result": result_row[0] if result_row is not None else None,
        "checkpoints": [[row[0], row[1]] for row in checkpoints],
    }


@dataclass(frozen=True, slots=True)
class HandlerRegistration:
    """One registry entry: the handler plus what a generic harness needs to
    exercise and verify it without knowing anything about the task type."""

    task_type: str
    fn: TaskHandler
    sample_payload: Mapping[str, Any]
    state_probe: StateProbe


REGISTRY: dict[str, HandlerRegistration] = {}


def task_handler(
    task_type: str,
    *,
    sample_payload: Mapping[str, Any],
    state_probe: StateProbe = default_state_probe,
) -> Callable[[TaskHandler], TaskHandler]:
    """Register `task_type`'s handler. See this module's registry contract."""

    def register(fn: TaskHandler) -> TaskHandler:
        if task_type in REGISTRY:
            raise ValueError(f"task type already registered: {task_type}")
        REGISTRY[task_type] = HandlerRegistration(
            task_type=task_type,
            fn=fn,
            sample_payload=dict(sample_payload),
            state_probe=state_probe,
        )
        return fn

    return register


class UnknownTaskType(LookupError):
    """No handler is registered for a claimed task's type."""


def get_handler(task_type: str) -> HandlerRegistration:
    """The registration for `task_type`, or `UnknownTaskType`."""
    try:
        return REGISTRY[task_type]
    except KeyError as exc:
        raise UnknownTaskType(task_type) from exc


def registered_types() -> tuple[str, ...]:
    """Task types a worker built from this registry can claim."""
    return tuple(sorted(REGISTRY))
