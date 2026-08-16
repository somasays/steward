"""The eval claims its task the way the queue publishes (#50, I7).

This exists because of the defect it now pins. `_claimed_task` used to run its
own statement:

    UPDATE tasks SET state = 'running', claimed_by = %(who)s, attempts = 1
    WHERE id = %(id)s

which jumped `pending -> running` straight past `claimed`, wrote no audit rows,
and duplicated the queue's schema knowledge inside a service. **H5 could not see
it**: the audit-completeness sweep runs over the repository registry, so a
service issuing raw SQL is structurally outside it — the same reason the
violation survived to a guardian pass. That is precisely why the fix needs a test
at this seam rather than a green suite: without one, restoring the `UPDATE` is
still green.

Asserted on durable state and on the audit trail, never on the return value
alone: the fencing pair exists so the classifier's checkpoint writes can be
rejected when they are stale, and a `ClassificationRun` carrying figures the
`tasks` row disagrees with would fence against nothing.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator

import pgserver
import pytest
from steward_queue import QueueConnection, connect, upgrade_to_head
from steward_workers.evals.harness import EVAL_WORKER, _claimed_task

pytestmark = pytest.mark.invariants

SELECT_TASK = "SELECT state, claimed_by, attempts, run_id FROM tasks WHERE id = %(id)s"
SELECT_ACTIONS = "SELECT action FROM audit_log ORDER BY id"


@pytest.fixture(scope="module")
def claim_dsn() -> Iterator[str]:
    """Its own database, so the audit trail this asserts over is only this
    scenario's. A shared one would make "the trail contains task.claimed" true
    for a row some other test claimed."""
    with tempfile.TemporaryDirectory(prefix="steward-claim") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")  # type: ignore[attr-defined]
        try:
            uri: str = server.get_uri()
            upgrade_to_head(uri)
            yield uri
        finally:
            server.cleanup()


@pytest.fixture
def conn(claim_dsn: str) -> Iterator[QueueConnection]:
    connection = connect(claim_dsn)
    try:
        yield connection
    finally:
        connection.close()


def test_the_task_is_claimed_running_and_fenced(claim_dsn: str, conn: QueueConnection) -> None:
    """The observable result: a `running` task whose stored fencing pair is the
    one handed to the classifier."""
    run = _claimed_task(claim_dsn)

    row = conn.execute(SELECT_TASK, {"id": run.task_id}).fetchone()
    conn.rollback()

    assert row is not None, "the eval returned a task id no row matches"
    state, claimed_by, attempts, run_id = row
    assert state == "running"
    assert claimed_by == EVAL_WORKER
    # The pair the checkpoint store fences with. Read back from the row rather
    # than compared to a literal: a `ClassificationRun` that disagrees with the
    # stored task fences against nothing, which is #85's shape.
    assert (claimed_by, attempts) == (run.claimed_by, run.attempts)
    assert run_id == run.run_id


def test_the_state_machine_is_not_skipped(claim_dsn: str, conn: QueueConnection) -> None:
    """`task.claimed` **and** `task.started`, in that order.

    The raw UPDATE produced neither — it set `running` directly, so the audit
    trail had no record that the task was ever claimed and none that it started.
    Asserting only "some audit row exists" would pass against the defect, since
    `run.created` and `task.enqueued` were written either way.
    """
    _claimed_task(claim_dsn)

    actions = [row[0] for row in conn.execute(SELECT_ACTIONS).fetchall()]
    conn.rollback()

    assert "task.claimed" in actions, actions
    assert "task.started" in actions, actions
    assert actions.index("task.claimed") < actions.index("task.started")


def test_the_run_carries_a_real_trace(claim_dsn: str) -> None:
    """Provenance the proposal is followed back by.

    The old code passed `"0" * 32` — a well-formed trace id naming nothing, so
    every eval run was untraceable and every one of them looked identical.
    """
    run = _claimed_task(claim_dsn)

    assert len(run.trace_id) == 32
    assert set(run.trace_id) != {"0"}, "a placeholder trace id follows back to nothing"


def test_two_evals_get_two_distinct_tasks(claim_dsn: str) -> None:
    """Each call claims its own. Sharing one would make the three runs of a
    single suite fence against each other's checkpoints."""
    first, second = _claimed_task(claim_dsn), _claimed_task(claim_dsn)

    assert first.task_id != second.task_id
    assert first.run_id != second.run_id
    assert first.trace_id != second.trace_id
