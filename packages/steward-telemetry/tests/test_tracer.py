"""The tracing seam, asserted on observable behaviour.

The rule these tests exist to defend: tracing degrades, it never fails. A
missing credential, an unreachable backend, a handler that raised -- none of
them may change what the rest of the system does.
"""

import re
from uuid import UUID, uuid4

import pytest
from langfuse import Langfuse
from steward_telemetry import (
    HOST_ENV,
    PUBLIC_KEY_ENV,
    SECRET_KEY_ENV,
    TRACE_ID_HEX_LENGTH,
    LangfuseTracer,
    NoopTracer,
    SpanOutcome,
    Tracer,
    new_trace_id,
    tracer_from_env,
)
from steward_telemetry._langfuse import RUN_SPAN_NAME, TASK_SPAN_NAME, _LangfuseSpan

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
TASK_ID = UUID("22222222-2222-2222-2222-222222222222")

CREDENTIALS = {PUBLIC_KEY_ENV: "pk-lf-test", SECRET_KEY_ENV: "sk-lf-test"}


class RecordingObservation:
    """Stands in for a Langfuse observation, capturing what was written to it."""

    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def update(self, **kwargs: object) -> "RecordingObservation":
        self.updates.append(kwargs)
        return self


class TestTraceId:
    def test_unseeded_ids_are_w3c_shaped_and_unique(self) -> None:
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100
        assert all(TRACE_ID_RE.match(trace_id) for trace_id in ids)
        assert all(len(trace_id) == TRACE_ID_HEX_LENGTH for trace_id in ids)

    def test_seeded_ids_are_deterministic(self) -> None:
        run_id = str(uuid4())
        assert new_trace_id(seed=run_id) == new_trace_id(seed=run_id)
        assert new_trace_id(seed=run_id) != new_trace_id(seed=str(uuid4()))

    def test_seeded_ids_match_langfuses_own_derivation(self) -> None:
        """The point of deriving rather than delegating: a seeded id must still
        be the id Langfuse would have produced, or it resolves to nothing in
        the UI. This pins the two implementations together, so a Langfuse
        change to the mapping fails here rather than silently in production."""
        seed = "run-11111111-1111-1111-1111-111111111111"
        assert new_trace_id(seed=seed) == Langfuse.create_trace_id(seed=seed)

    def test_generating_an_id_needs_no_credentials_and_no_client(self) -> None:
        # The whole graceful-degradation claim rests on this being pure.
        assert TRACE_ID_RE.match(new_trace_id())


class TestNoopTracer:
    def test_spans_open_and_close_and_record_nothing(self) -> None:
        tracer: Tracer = NoopTracer()
        with tracer.run_span(trace_id=new_trace_id(), run_id=RUN_ID, goal="noop") as span:
            span.record(SpanOutcome.OK)
        with tracer.task_span(
            trace_id=new_trace_id(), run_id=RUN_ID, task_id=TASK_ID, task_type="noop"
        ) as task:
            task.record(SpanOutcome.ERROR, "boom")

    def test_an_exception_inside_a_span_still_propagates(self) -> None:
        """Tracing must never swallow the failure it is observing."""
        tracer: Tracer = NoopTracer()
        with pytest.raises(RuntimeError, match="handler exploded"):
            with tracer.task_span(trace_id=new_trace_id(), run_id=RUN_ID, task_id=TASK_ID, task_type="noop"):
                raise RuntimeError("handler exploded")


class TestLangfuseSpan:
    def test_records_outcome_and_detail(self) -> None:
        observation = RecordingObservation()
        _LangfuseSpan(observation).record(SpanOutcome.ERROR, "budget_exceeded")
        assert observation.updates == [
            {"output": "error", "level": "ERROR", "status_message": "budget_exceeded"}
        ]

    def test_first_outcome_wins(self) -> None:
        """A caller-recorded failure must survive the context manager's exit."""
        observation = RecordingObservation()
        span = _LangfuseSpan(observation)
        span.record(SpanOutcome.ERROR, "declined")
        span.record(SpanOutcome.OK)
        assert len(observation.updates) == 1
        assert observation.updates[0]["level"] == "ERROR"


class TestLangfuseTracer:
    """Exercises the real client. No network assertion is made: Langfuse
    batches on a background thread, so what these prove is that the span calls
    are well-formed and that an unreachable backend costs nothing here."""

    @pytest.fixture
    def tracer(self) -> LangfuseTracer:
        return LangfuseTracer.from_credentials(public_key="pk-lf-test", secret_key="sk-lf-test")

    def test_run_and_task_spans_share_a_trace(self, tracer: LangfuseTracer) -> None:
        trace_id = new_trace_id(seed=str(RUN_ID))
        with tracer.run_span(trace_id=trace_id, run_id=RUN_ID, goal="scan_source") as span:
            span.record(SpanOutcome.OK)
        with tracer.task_span(trace_id=trace_id, run_id=RUN_ID, task_id=TASK_ID, task_type="noop") as span:
            span.record(SpanOutcome.OK)

    def test_an_exception_is_recorded_and_re_raised(self, tracer: LangfuseTracer) -> None:
        with pytest.raises(ValueError, match="bad payload"):
            with tracer.task_span(trace_id=new_trace_id(), run_id=RUN_ID, task_id=TASK_ID, task_type="noop"):
                raise ValueError("bad payload")

    def test_span_names_are_the_two_platform_levels(self) -> None:
        assert (RUN_SPAN_NAME, TASK_SPAN_NAME) == ("run", "task")


class TestWiring:
    def test_no_credentials_yields_the_noop_tracer(self) -> None:
        assert isinstance(tracer_from_env({}), NoopTracer)

    @pytest.mark.parametrize("present", [PUBLIC_KEY_ENV, SECRET_KEY_ENV])
    def test_half_a_credential_pair_is_not_enough(self, present: str) -> None:
        assert isinstance(tracer_from_env({present: "value"}), NoopTracer)

    def test_blank_credentials_are_treated_as_absent(self) -> None:
        blank = {PUBLIC_KEY_ENV: "  ", SECRET_KEY_ENV: ""}
        assert isinstance(tracer_from_env(blank), NoopTracer)

    def test_both_credentials_yield_the_langfuse_tracer(self) -> None:
        assert isinstance(tracer_from_env(dict(CREDENTIALS)), LangfuseTracer)

    def test_host_is_optional(self) -> None:
        with_host = dict(CREDENTIALS) | {HOST_ENV: "https://langfuse.example.internal"}
        assert isinstance(tracer_from_env(with_host), LangfuseTracer)

    def test_the_process_environment_is_the_default_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PUBLIC_KEY_ENV, raising=False)
        monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
        assert isinstance(tracer_from_env(), NoopTracer)
