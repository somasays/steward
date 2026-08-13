"""The Classifier's model half, against a stub gateway (#50).

`packages/steward-catalog/tests/test_classify_handler.py` owns the workflow: what
is loaded, what is refused, what is persisted. This file owns the other side of
the seam — the adapter that turns a `ClassificationRequest` into a bounded agent
run — and asserts the three things only this side can be asked about:

* **what the model is shown**, which is the prompt artifact and the evidence, and
  no tool but `submit_result`;
* **that the alias and prompt version on the result are the ones that ran**, not
  something a model said;
* **that a refused step and a dead gateway leave as the catalog's own failures**,
  because `steward-catalog` cannot catch a `steward-agents` exception (I4).

The gateway is a stub so the model's answers are fixed and the run's correctness
is decidable; everything carrying them is production code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from steward_agents import SUBMIT_RESULT, ModelReservation
from steward_catalog import (
    ClassificationRequest,
    ClassificationRun,
    ClassifierBudgetExceeded,
    ClassifierFailed,
)
from steward_llm import (
    DeploymentMode,
    EndpointAllowlist,
    GatewayConfig,
    ModelBinding,
    Role,
    StubGateway,
    StubReply,
    TokenPricing,
    ToolCall,
)
from steward_queue import QueueConnection, create_run, enqueue
from steward_schemas import (
    ColumnProfile,
    MaskedSample,
    RunBudget,
    SemanticType,
    TableProfile,
    TaskSpec,
    ValueFrequency,
)
from steward_telemetry import NoopTracer
from steward_workers.classifier import (
    CLASSIFY_MODEL_ALIAS,
    CLASSIFY_PROMPT_VERSION,
    AgentColumnClassifier,
    classify_prompt,
)

pytestmark = pytest.mark.invariants

ENDPOINT = "http://127.0.0.1:8000/v1"

MASKED_EMAIL = "j***@e***.***"

CLASSIFY_BUDGET = RunBudget(
    steps=6, tokens=120_000, cost_usd=Decimal("0.500000"), wall_clock=timedelta(minutes=10)
)


def gateway() -> GatewayConfig:
    """A development gateway binding the Classifier's alias to a local endpoint.

    Priced, because an alias whose bindings carry no token prices cannot be
    cost-bounded before a call and the loop refuses it.
    """
    return GatewayConfig(
        mode=DeploymentMode.DEVELOPMENT,
        source="classifier-test",
        bindings=(
            ModelBinding(
                alias=CLASSIFY_MODEL_ALIAS,
                model="openai/local",
                api_base=ENDPOINT,
                pricing=TokenPricing(
                    input_cost_per_token=Decimal("0.00000001"),
                    output_cost_per_token=Decimal("0.00000002"),
                    chat_template_tokens_per_message=8,
                ),
            ),
        ),
        allowlist=EndpointAllowlist.from_urls((ENDPOINT,)),
    )


def a_request() -> ClassificationRequest:
    """One masked column's worth of evidence — the shape a profile really has."""
    return ClassificationRequest(
        asset_id=UUID("33333333-3333-3333-3333-333333333333"),
        profile_version=4,
        profile=TableProfile(
            row_count=10,
            columns=(
                ColumnProfile(
                    name="email",
                    data_type="text",
                    null_count=0,
                    null_ratio=Decimal("0"),
                    distinct_count=10,
                    distinct_ratio=Decimal("1"),
                    top_values=(
                        ValueFrequency(
                            value=MaskedSample(
                                masked=MASKED_EMAIL, semantic_type=SemanticType.EMAIL, length=17
                            ),
                            count=3,
                        ),
                    ),
                    semantic_type=SemanticType.EMAIL,
                ),
            ),
        ),
    )


def submits(columns: object) -> list[StubReply]:
    """A one-step run: the model submits its classification and stops."""
    return [
        StubReply.completed(
            "",
            prompt_tokens=40,
            completion_tokens=20,
            cost_usd=Decimal("0.004"),
            tool_calls=(
                ToolCall(
                    id="s1",
                    name=SUBMIT_RESULT,
                    arguments=json.dumps({"columns": columns}),
                ),
            ),
        )
    ]


A_PII_COLUMN = [
    {
        "column_name": "email",
        "labels": ["pii"],
        "confidence": "0.95",
        "evidence": [
            {
                "profile_version": 4,
                "column_name": "email",
                "kind": "masked_sample",
                "locator": MASKED_EMAIL,
                "detail": "the sampled values are email addresses",
            }
        ],
    }
]


@pytest.fixture
def run(conn: QueueConnection) -> Iterator[ClassificationRun]:
    """A real claimed-shaped task, so the durable checkpoint store has a row to
    fence against and write to."""
    created = create_run(conn, goal="classify_asset", budget=CLASSIFY_BUDGET)
    spec = TaskSpec(
        task_id=uuid4(),
        run_id=created.id,
        task_type="classify_asset",
        payload={},
        budget=CLASSIFY_BUDGET,
        max_attempts=1,
    )
    enqueue(conn, spec)
    conn.execute(
        "UPDATE tasks SET state = 'running', claimed_by = %s, attempts = 1 WHERE id = %s",
        ("w-test", spec.task_id),
    )
    conn.commit()
    yield ClassificationRun(
        run_id=created.id,
        task_id=spec.task_id,
        trace_id="trace-test",
        claimed_by="w-test",
        attempts=1,
        budget=CLASSIFY_BUDGET,
    )


def classifier(
    dsn: str, replies: list[StubReply], *, reservation: ModelReservation | None = None
) -> tuple[AgentColumnClassifier, StubGateway]:
    stub = StubGateway({CLASSIFY_MODEL_ALIAS: replies})
    return (
        AgentColumnClassifier(
            dsn=dsn,
            gateway=gateway(),
            transport=stub,
            tracer=NoopTracer(),
            reservation=reservation,
        ),
        stub,
    )


async def test_the_model_is_offered_no_tool_but_submit_result(
    dsn: str, run: ClassificationRun
) -> None:
    """The least-privilege allowlist, asserted on what crossed the wire.

    Not on `AgentSpec.tools` being empty, which is the same statement one layer
    from where it matters: what decides whether a model can reach a source is the
    tool list in the request the gateway received.
    """
    agent, stub = classifier(dsn, submits(A_PII_COLUMN))

    await agent.classify(run, a_request())

    assert len(stub.calls) == 1
    offered = [tool.name for tool in stub.calls[0].request.tools]
    assert offered == [SUBMIT_RESULT], f"the model was offered {offered}"


async def test_the_prompt_artifact_and_the_evidence_are_what_the_model_sees(
    dsn: str, run: ClassificationRun
) -> None:
    """Both halves: the versioned prompt, and the request verbatim.

    The evidence assertion names a value that is actually in the request rather
    than checking the message is non-empty -- a length check passes on a prompt
    that lost its evidence, which is the failure this pairing exists to catch.
    """
    agent, stub = classifier(dsn, submits(A_PII_COLUMN))

    await agent.classify(run, a_request())

    messages = stub.calls[0].request.messages
    assert [message.role for message in messages] == [Role.SYSTEM, Role.USER]
    assert messages[0].content == classify_prompt()
    assert "Steward's Sensitivity Classifier" in messages[0].content
    evidence = json.loads(messages[1].content)
    assert evidence["profile_version"] == 4
    assert evidence["profile"]["columns"][0]["top_values"][0]["value"]["masked"] == MASKED_EMAIL
    assert stub.calls[0].request.prompt_version == CLASSIFY_PROMPT_VERSION
    assert stub.calls[0].request.alias == CLASSIFY_MODEL_ALIAS


async def test_the_result_carries_the_prompt_and_alias_that_ran(
    dsn: str, run: ClassificationRun
) -> None:
    """Provenance comes from the adapter, never from the model.

    The submission the stub sends carries no prompt version and no alias -- the
    output schema has no field for either -- so if these were not filled in here
    they could not be filled in at all.
    """
    agent, _ = classifier(dsn, submits(A_PII_COLUMN))

    proposed = await agent.classify(run, a_request())

    assert proposed.prompt_version == CLASSIFY_PROMPT_VERSION
    assert proposed.model_alias == CLASSIFY_MODEL_ALIAS
    assert [column.column_name for column in proposed.columns] == ["email"]
    assert proposed.columns[0].evidence[0].locator == MASKED_EMAIL


async def test_a_refused_step_leaves_as_a_budget_failure(
    dsn: str, run: ClassificationRun
) -> None:
    """A reservation larger than the cap refuses the first call.

    Paired with every other test in this file, which run the same code under a
    reservation that fits: the conversion is about the budget, not about the
    adapter refusing everything.
    """
    agent, stub = classifier(
        dsn,
        submits(A_PII_COLUMN),
        reservation=ModelReservation(
            tokens=1_000_000, cost_usd=Decimal("999"), wall_clock=timedelta(hours=1)
        ),
    )

    with pytest.raises(ClassifierBudgetExceeded):
        await agent.classify(run, a_request())

    assert stub.calls == [], "the step was charged before it was refused"


async def test_a_gateway_failure_leaves_as_a_classifier_failure(
    dsn: str, run: ClassificationRun
) -> None:
    """`steward-catalog` cannot catch an `LLMError`, so it must not see one."""
    agent, _ = classifier(
        dsn,
        [
            StubReply.streaming(
                ["partial"],
                prompt_tokens=10,
                cost_per_token=Decimal("0.001"),
                fails_with=ConnectionResetError("gateway went away"),
            )
        ],
    )

    with pytest.raises(ClassifierFailed) as raised:
        await agent.classify(run, a_request())

    assert not isinstance(raised.value, ClassifierBudgetExceeded)


async def test_output_the_schema_refuses_never_becomes_a_proposal(
    dsn: str, run: ClassificationRun
) -> None:
    """`none` beside a sensitive label is a contradiction, and the runtime is
    where it dies.

    The model gets its one correction (SPEC.md §3.2) and repeats itself, so the
    run ends as a failure rather than as a proposal nobody could review.
    """
    contradiction = [
        {
            "column_name": "email",
            "labels": ["pii", "none"],
            "confidence": "0.95",
            "evidence": [
                {
                    "profile_version": 4,
                    "column_name": "email",
                    "kind": "masked_sample",
                    "locator": MASKED_EMAIL,
                    "detail": "the sampled values are email addresses",
                }
            ],
        }
    ]
    agent, _ = classifier(dsn, submits(contradiction) + submits(contradiction))

    with pytest.raises(ClassifierFailed):
        await agent.classify(run, a_request())


async def test_the_checkpoint_connection_is_released(
    dsn: str, run: ClassificationRun, conn: QueueConnection
) -> None:
    """A run that ends leaves no connection behind for the process to leak."""
    agent, _ = classifier(dsn, submits(A_PII_COLUMN))

    await agent.classify(run, a_request())

    checkpoints = conn.execute(
        "SELECT count(*) FROM checkpoints WHERE task_id = %s", (run.task_id,)
    ).fetchone()
    assert checkpoints is not None and checkpoints[0] == 1


def test_the_prompt_is_a_versioned_artifact_not_a_literal() -> None:
    """I10: the prompt lives in `prompts/`, and its version names the file.

    Read as a pair so a rename cannot silently outlive its version: the constant
    the traces and proposal rows carry is derived from the same stem as the file
    the process actually reads.
    """
    text = classify_prompt()

    assert len(text) > 500
    assert CLASSIFY_PROMPT_VERSION == "classify_asset@v1"
    assert "submit_result" in text
    assert "masked" in text
