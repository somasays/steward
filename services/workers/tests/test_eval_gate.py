"""The B-tier gate's invocation contract (#50, GUARDRAILS.md Tier B).

This file exists because of a defect it now prevents. `_tool_check` skipped B*
on "no `evals/` yet", so `uv run steward evals run --changed` — the command the
gate has always named — was never executed and its **absence was never noticed**.
Creating `evals/` was therefore enough to turn every fitness run red for a reason
unrelated to eval quality: the pre-commit hook blocked an unrelated commit the
first time a fixture file was written.

The contract these tests pin, and each state exists because collapsing it into
another is how a gate starts lying:

* **nothing affected** -> exit 0. A change that cannot alter a score does not pay
  for three model runs, and the pre-commit hook must stay usable.
* **selected, no endpoint** -> `EXIT_NO_ENDPOINT`, which the runner shows as SKIP
  with its reason. CI has no model and must not be permanently red; a laptop
  without one must not be either. **It is not a pass.**
* **selected, no endpoint, REQUIRED** -> exit 1. `STEWARD_EVALS_REQUIRED=1` is
  what a release job would set, and there "could not run" is a failure. That is
  #50's "INCONCLUSIVE locally and a failure in the designated integration/release
  job", as a flag rather than a convention. **No such job exists yet (#88)** — the
  behaviour is pinned here, and unenforced in CI.
* **selected, endpoint present, no fixture** -> exit 1, whatever REQUIRED says.
  An absent fixture is a fact about this repository, not this machine, and #50
  forbids it reporting PASS.

None of these needs a model, which is the point: the gate's own behaviour is
testable where the thing it gates is not.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from steward_workers.evals import EXIT_FAILED, EXIT_NO_ENDPOINT, EXIT_OK, classification
from steward_workers.evals.__main__ import main
from steward_workers.evals.classification import AFFECTING_PATHS, CLASSIFICATION_SUITE

pytestmark = pytest.mark.invariants

PREFLIGHT_CONFIG = "evals/config/litellm.preflight-ollama.yaml"
PROXY_URL = "http://localhost:4000"
PROXY_KEY = "not-a-secret"
"""A reachable-looking proxy, for tests whose subject is *past* the endpoint
check. A bound model with nothing to carry a request to it is INCONCLUSIVE like
any other missing endpoint, so a test asserting a statement about the repository
— an absent fixture — has to configure one or it asserts the machine instead."""
LOCAL_ENDPOINT = "http://host.docker.internal:11434/v1"
"""Must equal the `api_base` in the preflight config — see the test below.

The proxy runs in a container and reaches Ollama on the host, so the endpoint
the allowlist governs is the one the *proxy* dials, not the one Steward does.
These two drifting apart is a refusal at startup, which is the right failure
and a confusing one to debug from a test that hard-codes the wrong half."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited gateway or REQUIRED flag: these assert on what is set here."""
    for name in (
        "STEWARD_LITELLM_CONFIG",
        "STEWARD_LLM_APPROVED_ENDPOINTS",
        "STEWARD_LLM_PROXY_URL",
        "STEWARD_EVALS_REQUIRED",
        "STEWARD_DEPLOYMENT_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_a_selected_suite_with_no_endpoint_is_inconclusive_not_a_pass() -> None:
    """The state the whole design turns on."""
    assert main(["evals", "run", "classification"]) == EXIT_NO_ENDPOINT


def test_the_same_condition_is_a_failure_where_evidence_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`STEWARD_EVALS_REQUIRED=1` — what a release job would set (#88: none does).

    Paired with the test above deliberately: one call, two environments, two exit
    codes. A runner that always returned 3, or always returned 1, would satisfy
    one of them and fail the other.
    """
    monkeypatch.setenv("STEWARD_EVALS_REQUIRED", "1")

    assert main(["evals", "run", "classification"]) == EXIT_FAILED


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_an_explicit_1_makes_evidence_required(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A flag that turned a gate hard on any truthy-looking string would make a
    stray `STEWARD_EVALS_REQUIRED=0` fail every local commit."""
    monkeypatch.setenv("STEWARD_EVALS_REQUIRED", value)

    assert main(["evals", "run", "classification"]) == EXIT_NO_ENDPOINT


@pytest.mark.parametrize("required", ["0", "1"])
def test_an_absent_fixture_fails_and_is_never_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: str
) -> None:
    """The anti-vacuity rule: an absent fixture cannot report PASS (#50).

    The absence is *created* here rather than borrowed from a repository that
    happens to have no fixture — which is what this test relied on before one was
    written, and would have silently stopped testing anything the moment it was.

    A gateway is configured, so this is past the endpoint check: a statement
    about the repository, not the machine. `REQUIRED` is parametrised because it
    escalates "could not run" and must not soften "there is nothing to run
    against".
    """
    monkeypatch.setenv("STEWARD_LITELLM_CONFIG", PREFLIGHT_CONFIG)
    monkeypatch.setenv("STEWARD_LLM_APPROVED_ENDPOINTS", LOCAL_ENDPOINT)
    monkeypatch.setenv("STEWARD_LLM_PROXY_URL", PROXY_URL)
    monkeypatch.setenv("STEWARD_LLM_PROXY_KEY", PROXY_KEY)
    monkeypatch.setenv("STEWARD_EVALS_REQUIRED", required)
    monkeypatch.setattr(classification, "FIXTURE_DIR", tmp_path / "absent")

    assert main(["evals", "run", "classification"]) == EXIT_FAILED


def test_an_empty_fixture_fails_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file that exists and declares nothing is the more dangerous shape: it
    looks like a fixture and scores nothing."""
    monkeypatch.setenv("STEWARD_LITELLM_CONFIG", PREFLIGHT_CONFIG)
    monkeypatch.setenv("STEWARD_LLM_APPROVED_ENDPOINTS", LOCAL_ENDPOINT)
    monkeypatch.setenv("STEWARD_LLM_PROXY_URL", PROXY_URL)
    monkeypatch.setenv("STEWARD_LLM_PROXY_KEY", PROXY_KEY)
    (tmp_path / "fixture.v1.json").write_text(
        '{"version": "v", "description": "d", "tables": []}'
    )
    monkeypatch.setattr(classification, "FIXTURE_DIR", tmp_path)

    assert main(["evals", "run", "classification"]) == EXIT_FAILED


def test_the_committed_fixture_loads_and_is_not_empty() -> None:
    """The positive case beside them: the fixture this repository ships is
    real, parses, and carries labelled columns."""
    fixture = classification.load_fixture()

    assert fixture.tables
    assert all(table.columns for table in fixture.tables)
    assert any(column.expected for table in fixture.tables for column in table.columns)
    assert fixture.version


def test_a_change_affecting_nothing_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 without touching a model — what keeps the pre-commit hook usable.

    The selection is stubbed rather than depending on what this working tree
    happens to contain: a test whose answer changes with the branch it runs on
    measures the branch.
    """
    monkeypatch.setattr(type(CLASSIFICATION_SUITE), "affected_by_working_tree", lambda self: False)

    assert main(["evals", "run", "--changed"]) == EXIT_OK


def test_a_change_touching_the_prompt_selects_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, so the test above cannot pass against a runner that
    selects nothing at all."""
    monkeypatch.setattr(type(CLASSIFICATION_SUITE), "affected_by_working_tree", lambda self: True)

    assert main(["evals", "run", "--changed"]) == EXIT_NO_ENDPOINT


def test_every_affecting_path_exists() -> None:
    """A path list is a dependency claim, and a stale one silently stops
    selecting the suite: a renamed prompt directory would mean prompt changes no
    longer trigger B2, and nothing would say so."""
    root = Path(__file__).resolve().parents[3]

    missing = [path for path in AFFECTING_PATHS if not (root / path).exists()]

    assert missing == []


def test_the_preflight_config_and_its_allowlist_agree() -> None:
    """The `api_base` in the config must be the endpoint the allowlist approves.

    They are set in two different places — a YAML file and an environment
    variable — and I15 refuses at startup when they disagree. That refusal is
    correct, but it reads as "the gateway is misconfigured" rather than "these
    two strings drifted", so the agreement is asserted where it can be read.
    """
    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / PREFLIGHT_CONFIG).read_text()
    )
    bases = {
        entry["litellm_params"]["api_base"]
        for entry in config["model_list"]
        if entry["model_name"] == "steward-classify"
    }

    assert bases == {LOCAL_ENDPOINT}


class TestTheArtifact:
    """What a run leaves behind, and whether it may be quoted.

    Handover step 2 for #50: *confirm every artifact is explicitly non-release*.
    Ollama scores characterise a model no deployment runs, and an artifact that
    does not say so will eventually be read as if it did — so the marker is
    computed from the pinning and the REQUIRED flag rather than being a field
    someone remembers to set.
    """

    def _payload(self, target: Path) -> dict[str, object]:
        classification._persist(
            [classification.RunOutcome(index=1, scores=())],
            str(target),
            fixture_version="classification-fixture@v9",
        )
        loaded: dict[str, object] = json.loads((target / "classification.json").read_text())
        return loaded

    def test_an_unpinned_run_says_it_is_not_release_evidence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The preflight case — the one this suite is usually run in."""
        for name in (classification.PROXY_IMAGE_ENV, classification.MODEL_REVISION_ENV):
            monkeypatch.delenv(name, raising=False)

        payload = self._payload(tmp_path)

        assert payload["release_evidence"] is False
        assert "NOT release evidence" in str(payload["note"])
        assert payload["proxy_image"] == "unpinned (preflight)"
        assert payload["model_revision"] == "unpinned (preflight)"

    DIGEST = "a" * 64
    PINS = {
        classification.PROXY_IMAGE_ENV: f"ghcr.io/berriai/litellm@sha256:{DIGEST}",
        classification.MODEL_REVISION_ENV: f"sha256:{DIGEST}",
    }

    def test_a_fully_pinned_required_run_is_still_refused_while_89_is_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The claim fails **closed**.

        `litellm.production.yaml` falls `steward-classify` back to
        `steward-fast`, and nothing records which one answered, so a run served
        entirely by the fallback looks identical to one served by the classifier
        model. Quality numbers attributed to a model that never ran are worse
        than none. The artifact says which condition is missing rather than going
        quiet, and this test is what makes #89 land as a behaviour change.
        """
        for name, value in self.PINS.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("STEWARD_EVALS_REQUIRED", "1")

        payload = self._payload(tmp_path)

        assert payload["release_evidence"] is False
        assert "responding model is not recorded (#89)" in str(payload["note"])
        assert payload["responding_model"] is None
        assert payload["model_alias_requested"] == "steward-classify"

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("ghcr.io/berriai/litellm:main-stable", "a moving tag is not a pin"),
            ("ghcr.io/berriai/litellm:latest", "nor is latest"),
            ("sha256:tooshort", "nor is a malformed digest"),
            ("", "nor is nothing"),
        ],
    )
    def test_a_mutable_tag_does_not_count_as_pinned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str, why: str
    ) -> None:
        """Two non-empty strings are not provenance. `main-stable` moves — the
        compose file pins by digest for exactly this reason — and an artifact
        naming a tag names a stack that may no longer exist."""
        monkeypatch.setenv(classification.MODEL_REVISION_ENV, f"sha256:{self.DIGEST}")
        monkeypatch.setenv(classification.PROXY_IMAGE_ENV, value)
        monkeypatch.setenv("STEWARD_EVALS_REQUIRED", "1")

        payload = self._payload(tmp_path)

        assert "not pinned to immutable digests" in str(payload["note"]), why

    def test_an_immutable_digest_does_count_as_pinned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The positive case beside it, so the rejection above cannot be
        satisfied by a rule that refuses everything — the negative-only guard
        this repository has shipped before (#82). `pinned` is no longer visible
        on the payload, so it is read through the blocker it stops producing."""
        for name, value in self.PINS.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("STEWARD_EVALS_REQUIRED", "1")

        payload = self._payload(tmp_path)

        assert "not pinned to immutable digests" not in str(payload["note"])

    @pytest.mark.parametrize("required", ["0", "1"])
    def test_an_unpinned_run_names_every_condition_it_misses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: str
    ) -> None:
        """An artifact that said only "not release evidence" would send a reader
        looking for one missing thing when there may be three."""
        for name in self.PINS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("STEWARD_EVALS_REQUIRED", required)

        note = str(self._payload(tmp_path)["note"])

        assert "not pinned to immutable digests" in note
        assert ("STEWARD_EVALS_REQUIRED is not 1" in note) is (required != "1")

    def test_the_fixture_version_is_the_one_that_was_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It used to be a literal `classification-fixture@v1`, which would have
        gone on naming v1 after the fixture was revised. Evidence naming the
        wrong dataset is worse than evidence naming none."""
        payload = self._payload(tmp_path)

        assert payload["fixture"] == "classification-fixture@v9"

    def test_the_gateway_config_is_identified_by_digest_not_copied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The config names an `api_key` reference; a digest says which config
        ran without publishing it."""
        config = tmp_path / "gateway.yaml"
        config.write_text("model_list: []\n")
        monkeypatch.setenv("STEWARD_LITELLM_CONFIG", str(config))

        payload = self._payload(tmp_path)

        assert payload["gateway_config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()

    def test_the_runner_writes_one_by_default(self) -> None:
        """The help text named `evals/artifacts` while the default was `None`, so
        no invocation of the gate ever wrote a result."""
        from steward_workers.evals.__main__ import DEFAULT_ARTIFACTS, _parser

        parsed = _parser().parse_args(["evals", "run", "classification"])

        assert parsed.artifacts == DEFAULT_ARTIFACTS == "evals/artifacts"

    def test_the_default_artifact_directory_is_not_committed(self) -> None:
        """A preflight score committed by accident is a number in the repository
        that reads like a release result."""
        root = Path(__file__).resolve().parents[3]

        assert "evals/artifacts/" in (root / ".gitignore").read_text()


class TestSelectionFailsClosed:
    """A baseline that cannot be resolved must over-select, never under-select.

    `_changed_paths` promises in prose that "a failure to ask git is not an
    answer: it returns everything-changed rather than nothing-changed, so a
    broken invocation over-selects instead of quietly running no evals at all."
    It tested `diff.returncode != 0 and working.returncode != 0` — **both** — and
    the common case is one of the two: no `origin/main` fails the diff with rc
    128 while `git status` returns rc 0 and, on a clean worktree, nothing. Empty
    paths, suite not selected, "no eval suite is affected by this change", exit
    0. A green B* that measured nothing, which is this repository's signature
    defect.
    """

    def _run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rcs: dict[str, int]) -> bool:
        """Select against stubbed git commands with the given return codes."""

        class _Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode
                self.stdout = ""

        def fake_run(cmd: list[str], **kwargs: object) -> _Result:
            return _Result(rcs["diff" if cmd[1] == "diff" else "status"])

        monkeypatch.setattr(classification.subprocess, "run", fake_run)
        return CLASSIFICATION_SUITE.affected_by_working_tree()

    @pytest.mark.parametrize(
        ("rcs", "why"),
        [
            ({"diff": 128, "status": 0}, "no origin/main, clean worktree — the real case"),
            ({"diff": 0, "status": 128}, "the mirror image"),
            ({"diff": 128, "status": 128}, "both unavailable"),
        ],
    )
    def test_a_git_failure_selects_the_suite(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rcs: dict[str, int], why: str
    ) -> None:
        assert self._run(monkeypatch, tmp_path, rcs) is True, why

    def test_both_succeeding_on_a_clean_tree_still_selects_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The positive case beside them. Without it the three above are
        satisfied by a function that always selects, which would mean the
        pre-commit hook paying for three model runs on every commit."""
        assert self._run(monkeypatch, tmp_path, {"diff": 0, "status": 0}) is False

    def test_the_selector_covers_what_decides_a_b2_run(self) -> None:
        """A path list is a dependency claim. These four are not speculative:
        the model binding table and prices, the transport, the agent loop, and
        the ledger seam whose rounding defect failed *every* B2 run on this
        branch without touching a prompt, a fixture or a classifier."""
        required = (
            "packages/steward-llm/src/steward_llm/",
            "packages/steward-agents/src/steward_agents/",
            "packages/steward-queue/src/steward_queue/runs.py",
            "packages/steward-queue/src/steward_queue/usage.py",
        )

        assert [path for path in required if path not in AFFECTING_PATHS] == []

    def test_a_production_binding_change_selects_the_suite(self) -> None:
        """The concrete case: changing the model `steward-classify` resolves to
        is the most B2-relevant change there is, and none of the original paths
        would have caught it."""
        bindings = "packages/steward-llm/src/steward_llm/defaults/litellm.production.yaml"

        assert bindings.startswith(AFFECTING_PATHS)


def test_a_gateway_with_no_proxy_is_inconclusive_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound model and nothing to carry a request to it is still "this machine
    cannot", not "this code is wrong".

    `classify_once` refuses a missing proxy with `ClassifierFailed`, which
    `_one_run` does not catch — it handles `EvaluationInfrastructureError` and
    `EvaluationResult` only — so this escaped as a traceback and exit 1 where the
    contract promises `EXIT_NO_ENDPOINT`. The line carried `# pragma: no cover --
    the caller checks first`, and no caller did.
    """
    monkeypatch.setenv("STEWARD_LITELLM_CONFIG", PREFLIGHT_CONFIG)
    monkeypatch.setenv("STEWARD_LLM_APPROVED_ENDPOINTS", LOCAL_ENDPOINT)
    monkeypatch.delenv("STEWARD_LLM_PROXY_URL", raising=False)
    monkeypatch.delenv("STEWARD_LLM_PROXY_KEY", raising=False)

    assert main(["evals", "run", "classification"]) == EXIT_NO_ENDPOINT


def test_the_declared_entry_point_is_the_one_the_gate_calls() -> None:
    """GUARDRAILS' B* row runs `steward evals run --changed`. If the console
    script is renamed, this fails here rather than as a red gate on someone
    else's unrelated commit — which is exactly how the original defect surfaced."""
    import tomllib

    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(manifest.read_text())["project"]["scripts"]

    assert scripts["steward"] == "steward_workers.evals.__main__:main"
