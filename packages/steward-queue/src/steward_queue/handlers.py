"""Built-in handlers.

`noop` exists so the registry, the worker loop and the H1 harness all have a
real subject from day one: M0's exit criterion is a no-op run flowing
API -> queue -> worker -> done (SPEC.md §12), and a harness with an empty
registry proves nothing.
"""

from datetime import timedelta
from decimal import Decimal

from steward_schemas import RunBudget, TaskResult, TaskStatus

from steward_queue.checkpoints import write_checkpoint
from steward_queue.registry import TaskContext, task_handler

NOOP_TASK_TYPE = "noop"


@task_handler(NOOP_TASK_TYPE, sample_payload={"echo": "noop"})
async def noop(ctx: TaskContext) -> TaskResult:
    """Echo the payload back, checkpointing once.

    Idempotent by construction (registry contract clause 2): the only write is
    a checkpoint upserted at a fixed step, and the result is a pure function of
    the payload -- nothing here reads what a previous attempt wrote.
    """
    write_checkpoint(ctx.connection, ctx.spec.task_id, step=0, state=dict(ctx.spec.payload))
    return TaskResult(
        task_id=ctx.spec.task_id,
        status=TaskStatus.SUCCEEDED,
        usage=RunBudget(steps=1, tokens=0, cost_usd=Decimal("0"), wall_clock=timedelta(0)),
        output=dict(ctx.spec.payload),
    )
