"""Tier H invariant harnesses for the queue (GUARDRAILS.md §1).

* **H1 — idempotency (I8).** Iterates `steward_queue.REGISTRY` and runs every
  registered handler twice with the same payload against a real database,
  asserting the handler's own state and its returned result are byte-identical
  across the two runs. It binds to the registry, not to a list in this file:
  a handler added in M1 is on the leash the moment it is registered.
* **H3 — no lost or ghost tasks (I8, N1).** Injects crashes around enqueue and
  around claim -- a transaction that dies mid-flight -- and asserts the
  resulting task set matches the state machine exactly: nothing enqueued by a
  transaction that never committed, nothing lost by a worker that died holding
  a claim, and nothing duplicated when the orchestrator replays.

Run: `pytest -m invariants` (this is what the fitness suite's H* check runs).
"""

import json
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from steward_queue import (
    REGISTRY,
    HandlerRegistration,
    TaskState,
    claim,
    complete,
    create_run,
    enqueue,
    get_task,
    mark_running,
    requeue_stale,
    write_checkpoint,
)
from steward_queue.db import QueueConnection
from steward_queue.registry import TaskContext
from steward_schemas import RunBudget, TaskResult, TaskSpec, TaskStatus

pytestmark = pytest.mark.invariants

EXPIRED_LEASE = timedelta(seconds=-1)

COUNT_RUNS = "SELECT count(*) FROM runs"
COUNT_TASKS = "SELECT count(*) FROM tasks"
COUNT_AUDIT = "SELECT count(*) FROM audit_log"
COUNT_CHECKPOINTS = "SELECT count(*) FROM checkpoints WHERE task_id = %s"
TASK_STATES = "SELECT state, count(*) FROM tasks GROUP BY state"
AUDIT_ACTIONS = "SELECT action FROM audit_log WHERE entity_id = %s ORDER BY id"


def count(conn: QueueConnection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params if params else None).fetchone()
    assert row is not None
    return int(row[0])


def canonical(value: object) -> str:
    """Byte-comparable rendering of a state probe's output."""
    return json.dumps(value, sort_keys=True, default=str)


def task_states(conn: QueueConnection) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute(TASK_STATES).fetchall()}


def audit_actions(conn: QueueConnection, task_id: UUID) -> list[str]:
    return [row[0] for row in conn.execute(AUDIT_ACTIONS, (str(task_id),)).fetchall()]


async def observe_twice(
    conn: QueueConnection, registration: HandlerRegistration, spec: TaskSpec
) -> list[tuple[str, str]]:
    """Execute a handler twice with identical input; return both observations.

    `attempts` is 1 on both runs on purpose: H1 asks what happens when the same
    input is executed twice, so nothing about the input may differ.
    """
    observations: list[tuple[str, str]] = []
    for _ in range(2):
        result = await registration.fn(TaskContext(connection=conn, spec=spec, attempts=1))
        conn.commit()
        observations.append((result.model_dump_json(), canonical(registration.state_probe(conn, spec))))
        conn.commit()
    return observations


class TestH1Idempotency:
    def test_the_registry_has_subjects(self) -> None:
        # A harness bound to a registry must fail loudly when the registry is
        # empty rather than pass on zero subjects.
        assert REGISTRY, "H1 has no registered handlers to exercise"

    async def test_every_registered_handler_is_idempotent(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        for task_type, registration in sorted(REGISTRY.items()):
            spec = spec_factory(run_id, task_type=task_type, payload=dict(registration.sample_payload))
            enqueue(conn, spec)
            conn.commit()

            (result_1, state_1), (result_2, state_2) = await observe_twice(conn, registration, spec)

            assert result_1 == result_2, f"{task_type}: second run returned a different result"
            assert state_1 == state_2, f"{task_type}: second run left different state"

    def test_the_comparison_would_catch_an_accumulating_handler(
        self, conn: QueueConnection, run_id: UUID, spec_factory: Callable[..., TaskSpec]
    ) -> None:
        """H1 must be falsifiable: state that accumulates has to change the probe.

        A handler that appends a checkpoint per attempt instead of upserting one
        is the classic non-idempotent shape; this asserts the probe used above
        actually distinguishes it, so a green H1 means something.
        """
        spec = spec_factory(run_id, payload={"probe": "falsifiability"})
        enqueue(conn, spec)
        conn.commit()
        probe = REGISTRY["noop"].state_probe

        write_checkpoint(conn, spec.task_id, step=0, state={"n": 0})
        conn.commit()
        after_first = canonical(probe(conn, spec))
        conn.commit()

        write_checkpoint(conn, spec.task_id, step=1, state={"n": 1})  # appended, not upserted
        conn.commit()
        after_second = canonical(probe(conn, spec))
        conn.commit()

        assert after_first != after_second
        assert count(conn, COUNT_CHECKPOINTS, spec.task_id) == 2


class TestH3NoGhostTasks:
    def test_a_transaction_that_dies_before_commit_enqueues_nothing(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        budget: RunBudget,
        spec_factory: Callable[..., TaskSpec],
    ) -> None:
        crashing = open_conn()
        run = create_run(crashing, goal="crash", budget=budget)
        enqueue(crashing, spec_factory(run.id, payload={"ghost": True}))
        crashing.close()  # the crash: no commit, ever

        assert count(conn, COUNT_RUNS) == 0
        assert count(conn, COUNT_TASKS) == 0
        assert count(conn, COUNT_AUDIT) == 0

    def test_the_same_transaction_committing_enqueues_exactly_once(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        budget: RunBudget,
        spec_factory: Callable[..., TaskSpec],
    ) -> None:
        # The other half of the proof: the identical sequence, committed, does
        # produce the run, the task and their audit rows -- so the test above
        # measures the crash, not a broken enqueue.
        writer = open_conn()
        run = create_run(writer, goal="survives", budget=budget)
        task_id = enqueue(writer, spec_factory(run.id, payload={"ghost": False}))
        writer.commit()

        assert count(conn, COUNT_RUNS) == 1
        assert count(conn, COUNT_TASKS) == 1
        assert audit_actions(conn, task_id) == ["task.enqueued"]

    def test_replaying_a_crashed_enqueue_converges_on_one_task(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        run_id: UUID,
        spec_factory: Callable[..., TaskSpec],
    ) -> None:
        """An orchestrator whose transaction died replays it. One task results."""
        payload = {"table": "public.orders"}
        crashing = open_conn()
        enqueue(crashing, spec_factory(run_id, payload=payload))
        crashing.close()

        replay = open_conn()
        enqueue(replay, spec_factory(run_id, payload=payload))
        enqueue(replay, spec_factory(run_id, payload=payload))  # and again, for good measure
        replay.commit()

        assert count(conn, COUNT_TASKS) == 1
        assert task_states(conn) == {TaskState.PENDING.value: 1}


class TestH3NoLostTasks:
    def test_a_worker_dying_between_claim_and_completion_loses_nothing(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        budget: RunBudget,
        queued: Callable[..., TaskSpec],
    ) -> None:
        spec = queued(payload={"n": 1})

        crashing = open_conn()
        claim(crashing, worker_id="crashed", lease=EXPIRED_LEASE)
        mark_running(crashing, spec.task_id, lease=EXPIRED_LEASE)
        crashing.commit()
        crashing.close()  # the crash: claimed and running, never finished

        assert requeue_stale(conn) == [(spec.task_id, TaskState.PENDING)]
        conn.commit()

        [reclaimed] = claim(conn, worker_id="healthy")
        mark_running(conn, spec.task_id)
        complete(
            conn,
            TaskResult(task_id=spec.task_id, status=TaskStatus.SUCCEEDED, usage=budget),
        )
        conn.commit()

        assert reclaimed.attempts == 2  # the crashed attempt was counted, not erased
        assert count(conn, COUNT_TASKS) == 1  # and never duplicated
        assert task_states(conn) == {TaskState.SUCCEEDED.value: 1}
        assert audit_actions(conn, spec.task_id) == [
            "task.enqueued",
            "task.claimed",
            "task.started",
            "task.lease_expired",
            "task.claimed",
            "task.started",
            "task.succeeded",
        ]

    def test_a_worker_dying_mid_execution_discards_its_partial_writes(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        queued: Callable[..., TaskSpec],
    ) -> None:
        spec = queued(payload={"n": 2})

        crashing = open_conn()
        claim(crashing, worker_id="crashed", lease=EXPIRED_LEASE)
        mark_running(crashing, spec.task_id, lease=EXPIRED_LEASE)
        crashing.commit()
        write_checkpoint(crashing, spec.task_id, step=0, state={"half": "written"})
        crashing.close()  # the crash: mid-handler, nothing committed since

        assert count(conn, COUNT_CHECKPOINTS, spec.task_id) == 0
        assert task_states(conn) == {TaskState.RUNNING.value: 1}

        assert requeue_stale(conn) == [(spec.task_id, TaskState.PENDING)]
        conn.commit()
        task = get_task(conn, spec.task_id)
        assert task is not None
        assert task.state is TaskState.PENDING
        assert task.attempts == 1

    def test_a_crashed_worker_holding_its_last_attempt_dead_letters(
        self,
        conn: QueueConnection,
        open_conn: Callable[[], QueueConnection],
        queued: Callable[..., TaskSpec],
    ) -> None:
        # A lost task must not become an immortal one: attempts still bound it.
        spec = queued(payload={"n": 3}, max_attempts=1)
        crashing = open_conn()
        claim(crashing, worker_id="crashed", lease=EXPIRED_LEASE)
        crashing.commit()
        crashing.close()

        assert requeue_stale(conn) == [(spec.task_id, TaskState.DEAD)]
        conn.commit()
        assert task_states(conn) == {TaskState.DEAD.value: 1}
