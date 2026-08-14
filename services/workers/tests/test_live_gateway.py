"""The live gateway smoke test (#50's amendment): the real route, once.

B2 and the deterministic suites all run against a stub. Nothing in them exercises
the actual inference path, and #69's HTTP transport rests on `MockTransport` plus
review — so one test has to call the thing itself.

Two deployment profiles, and the distinction is the whole point
---------------------------------------------------------------
* **Local preflight** — pinned LiteLLM → Ollama
  (`evals/config/docker-compose.preflight.yml`). Proves *plumbing*: that our
  transport's streaming tool-call assembly works against an OpenAI-compatible
  server at all. It is allowed to prove nothing else, and a run against it is
  **not** this test's evidence.
* **Required release smoke** — pinned LiteLLM → pinned vLLM at a pinned model
  revision. This is the evidence of record, because vLLM is not an
  interchangeable backend: its chat template, tool parser, streamed frame shapes,
  usage reporting and model revision are precisely what this test exists to
  validate. A green run against a different server says nothing about the one
  that is deployed.

What this covers, and the one link it does not
----------------------------------------------
    plan_run → queue → worker → AgentColumnClassifier → LiteLLM → vLLM
             → streamed tool call → checkpoint → accounting → pending_review

The run is admitted the way `steward_api.store` admits one internally — the same
`plan_run`, `create_run` and `enqueue` in one transaction — rather than over
HTTP, because a test tree may not import both services (I4: "services do not
import each other", and S1 cannot see an import in a `tests/` directory, which is
how #83 shipped one). The HTTP handler above that seam is covered by H11's
classification acceptance scenario, which drives `POST /v1/runs` through the same
queue with a stub classifier. Between them the whole path is covered; neither
covers it alone, and saying so is better than a test that quietly means less than
its name.

Assertions are on **durable state after the run settles**, never on the terminal
frame. A usage figure read from the response proves the model reported something;
what an operator is billed for, and what a budget bounds, is what reached the
database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pgserver
import pytest
from steward_catalog.classification import evidence_problems, proposal_history
from steward_llm import gateway_config_from_env, proxy_config_from_env
from steward_llm.proxy import LiteLLMProxyTransport
from steward_orchestration import CLASSIFY_ASSET_GOAL, plan_run
from steward_queue import (
    SYSTEM_ACTOR,
    QueueConnection,
    Worker,
    connect,
    create_run,
    enqueue,
    upgrade_to_head,
)
from steward_schemas import (
    AssetType,
    ProposalStatus,
    SemanticType,
    SourceCreate,
    SourceEngine,
    TableProfile,
)
from steward_telemetry import NoopTracer
from steward_workers.classifier import (
    CLASSIFY_MODEL_ALIAS,
    CLASSIFY_PROMPT_VERSION,
    AgentColumnClassifier,
)
from steward_workers.evals import REQUIRED_ENV

pytestmark = pytest.mark.live_gateway

PROXY_IMAGE_ENV = "STEWARD_SMOKE_PROXY_IMAGE"
MODEL_REVISION_ENV = "STEWARD_SMOKE_MODEL_REVISION"
ARTIFACT_ENV = "STEWARD_SMOKE_ARTIFACT"

SELECT_TASK_USAGE = """
SELECT used_tokens, used_cost_usd, state FROM tasks WHERE run_id = %(run_id)s
"""
SELECT_RUN_USAGE = """
SELECT used_tokens, used_cost_usd, status, budget_tokens, budget_cost_usd FROM runs
WHERE id = %(run_id)s
"""
SELECT_CHECKPOINT_USAGE = """
SELECT state FROM checkpoints WHERE task_id = %(task_id)s ORDER BY updated_at DESC LIMIT 1
"""

def _required() -> bool:
    """Whether this environment must produce evidence rather than skip."""
    return os.environ.get(REQUIRED_ENV, "").strip() == "1"


def _missing_configuration() -> str | None:
    """Why this cannot run, or None. Never guesses that it can."""
    if proxy_config_from_env(os.environ) is None:
        return "no proxy is configured (STEWARD_LLM_PROXY_URL / STEWARD_LLM_PROXY_KEY)"
    if gateway_config_from_env() is None:
        return "no gateway is configured (STEWARD_LITELLM_CONFIG)"
    return None


@pytest.fixture(scope="module", autouse=True)
def configured() -> None:
    """Skip loudly where a gateway is absent; fail where evidence is required.

    #50: missing proxy configuration is an explicit INCONCLUSIVE locally and a
    **failure** in the designated integration/release job. The same switch the
    eval runner uses, so one environment variable decides for both.
    """
    missing = _missing_configuration()
    if missing is None:
        return
    if _required():
        pytest.fail(
            f"{REQUIRED_ENV}=1 and the live gateway smoke could not run: {missing}. "
            "This job exists to produce the evidence #50 requires; a skip here would "
            "be a release with no proof the deployed route works."
        )
    pytest.skip(
        f"live gateway smoke: INCONCLUSIVE — {missing}. Not a pass. "
        f"Set {REQUIRED_ENV}=1 where this must not be skipped."
    )


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="steward-smoke") as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        try:
            uri: str = server.get_uri()
            upgrade_to_head(uri)
            yield uri
        finally:
            server.cleanup()


@pytest.fixture
def conn(dsn: str) -> Iterator[QueueConnection]:
    connection = connect(dsn)
    try:
        yield connection
    finally:
        connection.close()


def _profile() -> TableProfile:
    """A profile with one obvious positive and two negatives.

    Deliberately small: this is a protocol and accounting test, not a quality
    one. Whether the labels are *good* is B2's question, over a fixture built for
    it; what this asserts is that a real model's answer survives the route,
    validates, resolves and is accounted for.
    """
    from steward_schemas import ColumnProfile, MaskedSample, ValueFrequency

    return TableProfile(
        row_count=1000,
        columns=(
            ColumnProfile(
                name="email",
                data_type="text",
                null_count=0,
                null_ratio=Decimal("0.000000"),
                distinct_count=1000,
                distinct_ratio=Decimal("1.000000"),
                semantic_type=SemanticType.EMAIL,
                top_values=(
                    ValueFrequency(
                        value=MaskedSample(
                            masked="a***@e***.***",
                            semantic_type=SemanticType.EMAIL,
                            length=17,
                        ),
                        count=1,
                    ),
                ),
            ),
            ColumnProfile(
                name="row_version",
                data_type="bigint",
                null_count=0,
                null_ratio=Decimal("0.000000"),
                distinct_count=1000,
                distinct_ratio=Decimal("1.000000"),
            ),
        ),
    )


@pytest.fixture
def asset_id(conn: QueueConnection) -> UUID:
    """A profiled asset, created through the catalog's own writes.

    Registered and converged with `register_source` / `plan_convergence` /
    `apply_plan` rather than hand-written SQL: an INSERT here would duplicate
    schema knowledge this package already owns, and the first version of this
    fixture did exactly that and named a column that does not exist.

    The scan and profile *handlers* are covered against a real second database by
    H11. This test's subject is downstream of them, and depending on a connector
    would make a gateway failure and a source failure look alike.
    """
    from steward_catalog import (
        DiscoveredAsset,
        DiscoveredColumn,
        apply_plan,
        list_assets,
        load_state,
        plan_convergence,
        register_source,
    )
    from steward_catalog.profiles import record_profile

    source, _ = register_source(
        conn,
        SourceCreate(
            name="smoke",
            engine=SourceEngine.POSTGRES,
            host="smoke.internal",
            database="smoke",
            dsn_secret_ref="env:STEWARD_SMOKE_UNUSED",
            include_schemas=("sales",),
        ),
        actor=SYSTEM_ACTOR,
    )
    observed = (
        DiscoveredAsset(
            schema_name="sales",
            name="customers",
            asset_type=AssetType.TABLE,
            columns=tuple(
                DiscoveredColumn(
                    name=column.name,
                    data_type=column.data_type,
                    ordinal=ordinal,
                    nullable=True,
                )
                for ordinal, column in enumerate(_profile().columns, start=1)
            ),
        ),
    )
    apply_plan(
        conn,
        source.id,
        plan_convergence(load_state(conn, source.id), observed),
        actor=SYSTEM_ACTOR,
    )
    [asset] = list_assets(conn, source_id=source.id, after=None, limit=10)
    record_profile(conn, asset.id, _profile(), actor=SYSTEM_ACTOR)
    conn.commit()
    return asset.id


def test_the_deployed_route_produces_an_accounted_proposal(
    dsn: str, conn: QueueConnection, asset_id: UUID
) -> None:
    """The whole criterion, asserted on what survived the commit."""
    gateway = gateway_config_from_env()
    proxy = proxy_config_from_env(os.environ)
    assert gateway is not None and proxy is not None  # the fixture guarantees this

    transport = LiteLLMProxyTransport(proxy)
    classifier = AgentColumnClassifier(
        dsn=dsn, gateway=gateway, transport=transport, tracer=NoopTracer()
    )
    payload = {"asset_id": str(asset_id), "profile_version": 1}
    plan = plan_run(CLASSIFY_ASSET_GOAL, payload)
    run = create_run(conn, goal=CLASSIFY_ASSET_GOAL, budget=plan.budget, actor=SYSTEM_ACTOR)
    for task in plan.task_specs(run.id):
        enqueue(conn, task, actor=SYSTEM_ACTOR)
    conn.commit()

    with provide_classifier_for(classifier):
        drained = asyncio.run(_drain(dsn, run.id))
    # The transport is deliberately not closed here. Its HTTP client is first used
    # inside the worker's handler thread, which runs under its own `asyncio.run`
    # (D7), so closing it from *this* loop raises "Event loop is closed" after a
    # run that otherwise worked.
    #
    # That is a property of this test's teardown, **not** a production defect: a
    # single transport shared across two tasks was checked directly, and both
    # runs succeeded. The worker's composition root creates one transport
    # (`__main__.py`) and that is fine; what is not fine is a caller on a
    # different loop closing it.
    assert drained, "the worker claimed nothing; the task type was not claimable"

    # --- the run settled, inside every budget dimension -------------------
    row = conn.execute(SELECT_RUN_USAGE, {"run_id": run.id}).fetchone()
    conn.rollback()
    assert row is not None
    used_tokens, used_cost, status, budget_tokens, budget_cost = row
    assert status == "succeeded", f"the run did not succeed: {status}"
    assert used_tokens <= budget_tokens
    assert used_cost <= budget_cost

    # --- accounting is persisted and non-zero -----------------------------
    tasks = conn.execute(SELECT_TASK_USAGE, {"run_id": run.id}).fetchall()
    conn.rollback()
    assert len(tasks) == 1
    task_tokens, task_cost, task_state = tasks[0]
    assert task_state == "succeeded"
    assert task_tokens > 0, "a real model call recorded no tokens"
    assert task_cost > 0, (
        "a real model call recorded no cost; a zero price makes every dollar bound vacuous"
    )

    # --- exactly one proposal, pending review -----------------------------
    proposals = proposal_history(conn, asset_id)
    conn.rollback()
    assert len(proposals) == 1
    record = proposals[0]
    assert record.status is ProposalStatus.PENDING_REVIEW
    assert record.prompt_version == CLASSIFY_PROMPT_VERSION == "classify_asset@v1"
    assert record.model_alias == CLASSIFY_MODEL_ALIAS == "steward-classify"

    # --- every profiled column classified, every citation resolving -------
    profiled = {column.name for column in _profile().columns}
    assert {column.column_name for column in record.proposal.columns} == profiled
    assert evidence_problems(record.proposal, _profile()) == ()

    # --- provenance populated ---------------------------------------------
    assert record.run_id == run.id
    assert record.task_id is not None
    assert record.trace_id, "a proposal with no trace id cannot be followed back to its call"

    _write_evidence(record, task_tokens, task_cost)


def test_the_evidence_names_what_produced_it() -> None:
    """Evidence that cannot say which images and revision produced it is not
    evidence — it is a green tick over an unknown stack.

    Only enforced where the result is required: locally a developer may run this
    against the preflight without pinning anything, and the artifact then records
    that it was unpinned rather than pretending otherwise.
    """
    if not _required():
        pytest.skip("provenance pinning is required only where the evidence is")

    missing = [name for name in (PROXY_IMAGE_ENV, MODEL_REVISION_ENV) if not os.environ.get(name)]

    assert missing == [], (
        f"{', '.join(missing)} unset: the release smoke must record the pinned LiteLLM "
        "image and the vLLM image/model revision it ran against"
    )


def provide_classifier_for(classifier: object) -> Iterator[None]:
    """Bind the capability for one scenario, restoring what was there."""
    from steward_catalog import CLASSIFIER

    return CLASSIFIER.overridden(classifier)  # type: ignore[arg-type,return-value]


async def _drain(dsn: str, run_id: UUID) -> bool:
    """Run the worker until the run leaves `pending`/`running`."""
    worker = Worker(dsn, "smoke-worker", task_types=("classify_asset",))
    for _ in range(40):
        claimed = await worker.run_once()
        with connect(dsn) as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE id = %(id)s", {"id": run_id}
            ).fetchone()
            connection.rollback()
        if row is not None and row[0] in {"succeeded", "failed", "cancelled"}:
            return True
        if not claimed:
            await asyncio.sleep(0.2)
    return False


def _write_evidence(record: object, tokens: int, cost: Decimal) -> None:
    """Record what ran, so the proof names its own stack.

    The config is hashed rather than copied: it may carry an `api_key` reference
    and a digest identifies it without publishing it.
    """
    target = os.environ.get(ARTIFACT_ENV)
    if not target:
        return
    config_path = os.environ.get("STEWARD_LITELLM_CONFIG", "")
    digest = (
        hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        if config_path and Path(config_path).exists()
        else None
    )
    evidence = {
        "proxy_image": os.environ.get(PROXY_IMAGE_ENV) or "unpinned (preflight)",
        "model_revision": os.environ.get(MODEL_REVISION_ENV) or "unpinned (preflight)",
        "gateway_config_sha256": digest,
        "prompt_version": CLASSIFY_PROMPT_VERSION,
        "model_alias": CLASSIFY_MODEL_ALIAS,
        "persisted_tokens": tokens,
        "persisted_cost_usd": str(cost),
        "required": _required(),
    }
    Path(target).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
