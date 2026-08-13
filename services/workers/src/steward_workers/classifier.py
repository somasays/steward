"""The Classifier's model half: a `ColumnClassifier` over the #69 runtime.

`steward_catalog` owns the `classify_asset` workflow and cannot call a model —
I4 forbids it the gateway client, and it should, because a package that owns the
privacy boundary must not also own a way past it. What it declares instead is
`ColumnClassifier`: evidence in, proposed columns out. This module is the
implementation of that protocol, and everything the catalog is not allowed to
know lives here — the bounded loop, the `steward-classify` alias, the gateway,
the reservation, the tool allowlist and the prompt (SPEC.md §13 D15).

The allowlist is the shortest one the runtime permits: **nothing**. `AgentSpec.
tools` is empty and the registry handed to the runtime holds no tools, so the
only callable the model is shown is `submit_result`, which the runtime adds and
which ends the run. There is no tool that reads a source, runs SQL, fetches a
value the profile did not publish, or writes to the catalog — not because the
prompt asks the model not to, but because no such tool is in the process's reach
(SPEC.md §3.2, issue #50).

What the model sees is the `ClassificationRequest` verbatim, serialised. That is
a deliberate choice over hand-rendering a subset: the request is *already* the
evidence-only view — every value-carrying field on a `TableProfile` is a
`MaskedSample` by construction (I6) — and a hand-written projection would be a
second allowlist to keep in sync with the first, green on the day it fell behind.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from steward_agents import (
    AgentRuntime,
    AgentRuntimeError,
    BudgetExceeded,
    ModelReservation,
    ToolRegistry,
    TraceContext,
)
from steward_catalog import (
    ClassificationRequest,
    ClassificationRun,
    ClassifierBudgetExceeded,
    ClassifierFailed,
    ProposedClassification,
)
from steward_llm import GatewayConfig, GatewayTransport, LLMClient, LLMError, Message, Role
from steward_queue import RunBudgetBreached
from steward_schemas import AgentSpec, ColumnClassification
from steward_telemetry import Tracer

from steward_workers.agent_tasks import DurableCheckpointStore

__all__ = [
    "CLASSIFY_MODEL_ALIAS",
    "CLASSIFY_PROMPT_VERSION",
    "CLASSIFY_RESERVATION",
    "AgentColumnClassifier",
    "ProposedColumns",
    "classify_prompt",
]

CLASSIFY_MODEL_ALIAS = "steward-classify"
"""The gateway alias this agent runs on (SPEC.md §6).

An alias, never a provider or model name: the client resolves it against the
validated binding table, so this constant cannot express a bypass even by
accident (I2, I15).
"""

CLASSIFY_PROMPT_VERSION = "classify_asset@v1"
"""Recorded on every generation span (I7) and on every proposal row (#50).

Bumped when `prompts/classify_asset.v1.md` changes, and the file is renamed with
it: a version that outlived an edit answers "which prompt produced this" with a
prompt that no longer exists. A new prompt version also makes a new proposal --
`classification.propose` converges on `(asset, profile version, prompt version,
model alias)` -- which is what lets a prompt change be re-run and compared rather
than overwrite what the old one said.
"""

PROMPT_FILE = "classify_asset.v1.md"

CLASSIFY_RESERVATION = ModelReservation(
    tokens=18_000,
    cost_usd=Decimal("0.08"),
    wall_clock=timedelta(seconds=90),
)
"""The worst case reserved before each model call, chosen to divide into the
goal's cap.

`CLASSIFY_ASSET_BUDGET` is six steps, 120k tokens, $0.50 and ten minutes, so six
of these fit inside every dimension of it (108k, $0.48, nine minutes) with the
margin an overestimate is allowed to cost. A reservation that did not divide in
would refuse a step the budget could actually afford; one larger than the cap
would refuse the *first* call and the agent would never run at all.
"""


class ProposedColumns(BaseModel):
    """What the model submits: the columns it classified, and nothing else.

    Deliberately not `ClassificationProposal`. That model carries `asset_id`,
    `profile_version`, `prompt_version` and `model_alias`, and every one of them
    is a fact about *which run this was* rather than a finding — asking a model
    to restate them would make provenance something it could get wrong, or
    forge. The handler holds all four already and fills them in itself.

    `ColumnClassification` is reused as-is, so `none`-exclusivity, evidence per
    sensitive label, no duplicate citations and same-column references are all
    enforced where the model's output is parsed (I3): a submission that breaks
    one is a validation error the runtime hands back for its one correction,
    not a row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    columns: tuple[ColumnClassification, ...] = Field(min_length=1)


@cache
def classify_prompt() -> str:
    """The versioned prompt artifact, read from `prompts/` (I10).

    A file rather than a literal, so S4 can see it is versioned and so a change
    to it is a diff on a prompt rather than a diff on a Python module. Cached
    because it is immutable for the life of the process and a worker would
    otherwise re-read it once per task.
    """
    return (Path(__file__).parent / "prompts" / PROMPT_FILE).read_text(encoding="utf-8")


class AgentColumnClassifier:
    """`ColumnClassifier` over the bounded runtime (#69).

    Satisfies the protocol structurally; `steward_catalog` never learns this
    type exists, and nothing here imports the handler that calls it.

    Built by a composition root, because two of its collaborators are a
    composition root's to choose: the `GatewayConfig` was validated at boot and
    re-reading the environment here could reach a gateway that refusal never saw
    (I15), and the transport decides whether this process talks to a real proxy
    or a fixture.
    """

    def __init__(
        self,
        *,
        dsn: str,
        gateway: GatewayConfig,
        transport: GatewayTransport,
        tracer: Tracer,
        reservation: ModelReservation | None = None,
    ) -> None:
        self._dsn = dsn
        self._client = LLMClient(gateway, transport)
        self._tracer = tracer
        self._reservation = reservation or CLASSIFY_RESERVATION

    async def classify(
        self, run: ClassificationRun, request: ClassificationRequest
    ) -> ProposedClassification:
        """Run the bounded loop over this request's evidence.

        Every failure the runtime can raise is converted to the catalog's own
        vocabulary before it leaves. The handler cannot catch `BudgetExceeded`
        or `AgentRuntimeError` — they are `steward-agents` types and the package
        may not import them (I4) — so an unconverted raise here would reach the
        queue as `handler raised`, and an operator would go looking for a bug
        where there was a cap.
        """
        checkpoints = DurableCheckpointStore(
            dsn=self._dsn,
            task_id=run.task_id,
            run_id=run.run_id,
            claimed_by=run.claimed_by,
            attempts=run.attempts,
        )
        runtime = AgentRuntime(
            client=self._client,
            # Empty, and that is the allowlist: the runtime adds `submit_result`
            # and shows the model nothing else, so there is no tool through
            # which a source, a raw value or a catalog write could be reached.
            tools=ToolRegistry(),
            checkpoints=checkpoints,
            reservation=self._reservation,
            # Not wired to a usage ledger, for the reason `agent.echo` does not
            # wire one: spend is charged inside the checkpoint transaction where
            # it becomes durable, and debiting here as well would bill the run
            # twice for the same tokens (SPEC.md §13 D12).
            tracer=self._tracer,
        )
        try:
            result = await runtime.run(
                key=str(run.task_id),
                spec=AgentSpec(
                    name="classifier",
                    model_alias=CLASSIFY_MODEL_ALIAS,
                    tools=(),
                    # The task's own cap, already reserved out of the run's (D9).
                    # A second figure declared here would be one nobody reserved.
                    limits=run.budget,
                ),
                prompt_version=CLASSIFY_PROMPT_VERSION,
                messages=(
                    Message(role=Role.SYSTEM, content=classify_prompt()),
                    Message(role=Role.USER, content=request.model_dump_json(indent=2)),
                ),
                output_model=ProposedColumns,
                trace=TraceContext(trace_id=run.trace_id, task_id=run.task_id),
            )
        except (BudgetExceeded, RunBudgetBreached) as exc:
            raise ClassifierBudgetExceeded(str(exc)) from exc
        except (AgentRuntimeError, LLMError) as exc:
            raise ClassifierFailed(str(exc)) from exc
        finally:
            # However the run ended, this connection is not the worker's to
            # reclaim -- an abandoned handler thread would otherwise leave it
            # open until the process exits.
            checkpoints.close()

        submitted = result.output
        if not isinstance(submitted, ProposedColumns):  # pragma: no cover -- runtime validates
            raise ClassifierFailed(
                f"the runtime returned {type(submitted).__name__}, not a classification"
            )
        return ProposedClassification(
            columns=submitted.columns,
            prompt_version=CLASSIFY_PROMPT_VERSION,
            model_alias=CLASSIFY_MODEL_ALIAS,
        )
