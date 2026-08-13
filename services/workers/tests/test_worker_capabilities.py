"""What a worker claims, and what it refuses to start without (#50).

`classify_asset`'s handler is registered by importing `steward_catalog` — that is
what lets it be a goal the shipped registry carries (SPEC.md §13 D15). The model
behind it is not: it is a capability a composition root binds. So registration and
claiming come apart here for the first time, and the two ways that could go wrong
are what this file asserts:

* **a worker without the capability claiming the work anyway**, failing every one
  of those tasks and looking like a broken classifier rather than an unconfigured
  one; and
* **a worker started to run the Classifier coming up without one**, claiming
  everything else, and reporting itself healthy while the capability it exists for
  is absent.

Both are asserted against `main()` and `claimable_types()` themselves rather than
against a description of them, because the failure mode in both directions is
wiring that silently stopped happening.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from steward_catalog import (
    CLASSIFIER,
    CLASSIFY_ASSET_TASK_TYPE,
    ClassificationRequest,
    ClassificationRun,
    ProposedClassification,
    classifier_bound,
)
from steward_llm import (
    DeploymentMode,
    EndpointAllowlist,
    GatewayConfig,
    ModelBinding,
    TokenPricing,
)
from steward_queue import DSN_ENV, REGISTRY, registered_types
from steward_telemetry import NoopTracer
from steward_workers import __main__ as entry
from steward_workers.agent_tasks import AGENT_ECHO

pytestmark = pytest.mark.invariants

ENDPOINT = "http://127.0.0.1:8000/v1"
A_DSN = "postgresql://steward@localhost/steward"


class NeverCalled:
    """A `ColumnClassifier` that stands for "this process has the capability".

    It never runs: every test here is about whether a classifier is *bound*, and
    one that answered would invite a test to drift into asserting what it said.
    """

    async def classify(
        self, run: ClassificationRun, request: ClassificationRequest
    ) -> ProposedClassification:  # pragma: no cover -- binding is the subject
        raise AssertionError("this classifier exists to be bound, not to be called")


def gateway() -> GatewayConfig:
    return GatewayConfig(
        mode=DeploymentMode.DEVELOPMENT,
        source="capability-test",
        bindings=(
            ModelBinding(
                alias="steward-classify",
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


@pytest.fixture
def echo_unregistered() -> Iterator[None]:
    """`agent.echo` out of the registry, so `register_agent_tasks` can add it.

    The registry refuses a duplicate registration, and another module in this
    test session registers the same task type.
    """
    REGISTRY.pop(AGENT_ECHO, None)
    yield
    REGISTRY.pop(AGENT_ECHO, None)


def test_a_worker_without_a_classifier_does_not_claim_classification() -> None:
    """The registry holds the handler; this process still must not take the work."""
    assert classifier_bound() is False
    assert CLASSIFY_ASSET_TASK_TYPE in registered_types()

    assert CLASSIFY_ASSET_TASK_TYPE not in entry.claimable_types()


def test_a_worker_with_a_classifier_claims_classification() -> None:
    """The positive half: the exclusion is about the capability, not the type.

    Without this, a `claimable_types()` that dropped `classify_asset`
    unconditionally -- or returned nothing at all -- would satisfy the test above.
    """
    with CLASSIFIER.overridden(NeverCalled()):
        claims = entry.claimable_types()

    assert CLASSIFY_ASSET_TASK_TYPE in claims
    assert set(claims) == set(registered_types())


def test_the_capability_map_narrows_nothing_else() -> None:
    """Only capability-gated types are ever withheld.

    A `claimable_types()` that quietly dropped `scan_source` would leave a whole
    milestone's work unclaimed, and the assertions above would not notice.
    """
    unconditional = set(registered_types()) - set(entry.CAPABILITIES)

    assert unconditional <= set(entry.claimable_types())
    assert unconditional, "no unconditional task types; the comparison proves nothing"


def test_a_model_consuming_worker_refuses_to_start_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asked for a transport and given no classifier, the process must not boot.

    `register_agent_tasks` is replaced with one that binds nothing, which is
    exactly the regression this guard exists for: a composition root that stops
    binding while still reporting that it registered something.
    """
    monkeypatch.setenv(DSN_ENV, A_DSN)
    monkeypatch.setenv(entry.AGENT_TRANSPORT_ENV, "stub")
    monkeypatch.setattr(entry, "register_agent_tasks", lambda *args, **kwargs: "bound nothing")
    monkeypatch.setattr(entry, "gateway_config_from_env", gateway)

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert entry.AGENT_TRANSPORT_ENV in str(exited.value)
    assert "classifier" in str(exited.value)


def test_a_correctly_configured_worker_gets_past_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half, and the one that says what the worker ends up claiming.

    Without it, a guard that exited for every model-consuming worker would pass
    the test above -- and no worker would ever run the Classifier.
    """
    built: dict[str, Any] = {}

    class RecordingWorker:
        def __init__(self, dsn: str, worker_id: str, **kwargs: Any) -> None:
            built["dsn"] = dsn
            built["task_types"] = kwargs["task_types"]

    async def immediately(worker: object) -> None:
        built["ran"] = True

    monkeypatch.setenv(DSN_ENV, A_DSN)
    monkeypatch.setenv(entry.AGENT_TRANSPORT_ENV, "stub")
    monkeypatch.setattr(entry, "register_agent_tasks", lambda *args, **kwargs: "bound a classifier")
    monkeypatch.setattr(entry, "gateway_config_from_env", gateway)
    monkeypatch.setattr(entry, "Worker", RecordingWorker)
    monkeypatch.setattr(entry, "run", immediately)

    with CLASSIFIER.overridden(NeverCalled()):
        entry.main()

    assert built["ran"] is True
    assert CLASSIFY_ASSET_TASK_TYPE in built["task_types"]


def test_a_worker_with_no_transport_starts_and_claims_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-free worker is a supported deployment, not a broken one.

    It comes up, claims the deterministic task types, and leaves classification
    to a process that can do it -- which is the whole reason the refusal above is
    conditioned on the transport rather than applied to every worker.
    """
    built: dict[str, Any] = {}

    class RecordingWorker:
        def __init__(self, dsn: str, worker_id: str, **kwargs: Any) -> None:
            built["task_types"] = kwargs["task_types"]

    async def immediately(worker: object) -> None:
        built["ran"] = True

    monkeypatch.setenv(DSN_ENV, A_DSN)
    monkeypatch.delenv(entry.AGENT_TRANSPORT_ENV, raising=False)
    monkeypatch.setattr(entry, "gateway_config_from_env", lambda: None)
    monkeypatch.setattr(entry, "Worker", RecordingWorker)
    monkeypatch.setattr(entry, "run", immediately)

    entry.main()

    assert built["ran"] is True
    assert CLASSIFY_ASSET_TASK_TYPE not in built["task_types"]
    assert "scan_source" in built["task_types"]


def test_registering_agent_tasks_binds_the_process_classifier(
    monkeypatch: pytest.MonkeyPatch, echo_unregistered: None
) -> None:
    """The real composition root, not a stand-in for it.

    Every other test here replaces `register_agent_tasks`, so this is the one
    that says the replaced thing actually binds a classifier -- and it names the
    task type in what it reports, so an operator reading the boot log sees the
    capability rather than inferring it.
    """
    monkeypatch.setenv(entry.AGENT_TRANSPORT_ENV, "stub")

    with CLASSIFIER.overridden(None):
        reported = entry.register_agent_tasks(A_DSN, gateway(), NoopTracer())

        assert classifier_bound() is True
        assert CLASSIFY_ASSET_TASK_TYPE in reported
        assert "stub" in reported

    assert classifier_bound() is False


def test_a_transport_without_a_gateway_is_refused(
    monkeypatch: pytest.MonkeyPatch, echo_unregistered: None
) -> None:
    """I15: an agent handler with no validated gateway cannot exist."""
    monkeypatch.setenv(entry.AGENT_TRANSPORT_ENV, "stub")

    with CLASSIFIER.overridden(None), pytest.raises(SystemExit):
        entry.register_agent_tasks(A_DSN, None, NoopTracer())

    assert classifier_bound() is False


def test_the_capability_gate_is_keyed_on_the_pathology() -> None:
    """`CAPABILITIES` names the types that need one, not the ones that are safe.

    An allowlist of known-safe types would let a second agent-backed handler be
    claimed by a worker that cannot run it, silently, on the day it is added.
    """
    assert set(entry.CAPABILITIES) == {CLASSIFY_ASSET_TASK_TYPE}
    assert entry.CAPABILITIES[CLASSIFY_ASSET_TASK_TYPE] is classifier_bound

