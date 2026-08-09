"""H12 — every service entry point refuses a gateway config that routes off the
allowlist (I15).

S9 proves the committed routing table and the refusal logic agree; G3 proves the
refusal fires. Neither can see *wiring*: until now the call lived in the worker's
composition root, and a new entry point that forgot it would have started
happily against a config pointed at a hosted API. GUARDRAILS.md §5 named this as
I15's promotion path, and this is it — the refusal is asserted at every process a
service ships, not at the ones someone remembered.

Three things keep the result honest:

* **The entry points are enumerated from the repo**, not listed here: every
  `[project.scripts]` target in every `services/*/pyproject.toml`. A service
  added next month is on the leash without a test edit — which is the same rule
  GUARDRAILS.md §3 states for registry-bound harnesses.
* **Zero is a failure, not a pass.** A harness that enumerates nothing and boots
  nothing reports green while measuring nothing, which is this project's
  signature defect. Three guards, and it is worth being exact about which one
  does what: the enumeration is asserted non-empty, every service directory is
  asserted to have a manifest declaring at least one script (so a service added
  without one FAILs rather than shrinking the set), and the processes actually
  spawned are asserted to be the ones enumerated — counted inside `Boots`, at the
  point a subprocess is started, because a tally appended next to the assertions
  is equal by construction and cannot fail.
* **A control run says the refusal is specific.** Each entry point is booted a
  second time against the config this repo ships, and must *not* mention the
  refusal — otherwise "exits non-zero" would be satisfied by a process that
  refuses every config it is given. What the control does **not** prove is that
  the process got past the check and did its work: it asserts the refusal is
  absent from output that exists, not that the boot succeeded. The rogue run is
  what carries the weight. The environment is built from scratch for both runs, so
  a developer's exported `STEWARD_DEPLOYMENT_MODE` cannot quietly turn the
  refusal off.

One edge is worth writing down because no tool can see it: this harness boots
`steward_api`'s entry points from `steward-workers`' test tree, and
`steward-workers` declares no dependency on `steward-api`. It resolves because
the uv workspace installs every member. That is a repo-wide harness living in one
service for want of a repo-wide test tree; it fails loudly rather than silently if
that stops being true.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from steward_llm.config import COMMITTED_CONFIG, PRODUCTION_ALIASES
from steward_llm.endpoints import GatewayConfigError, NonApprovedEndpoint

pytestmark = pytest.mark.invariants

ROGUE_ENDPOINT = "http://rogue-inference.example.internal:8000/v1"
"""A base URL that is not on any allowlist. Not a hosted provider host on
purpose: the deny layer would refuse one of those whatever the allowlist said, so
using one would prove the backstop rather than the allowlist."""

APPROVED_ENDPOINT = "http://vllm-fast-a.steward-inference.svc.cluster.local:8000/v1"

BOOT_TIMEOUT_SECONDS = 120


def repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "GUARDRAILS.md").exists():
            return candidate
    raise RuntimeError("could not locate the repo root")


def service_entry_points() -> tuple[tuple[str, str, str], ...]:
    """Every console script every service declares: (service, script, "module:attr")."""
    services_dir = repo_root() / "services"
    service_dirs = sorted(path for path in services_dir.iterdir() if path.is_dir())
    entry_points: list[tuple[str, str, str]] = []
    for service in service_dirs:
        manifest = service / "pyproject.toml"
        assert manifest.exists(), f"{service.name} is a service with no pyproject.toml to enumerate"
        scripts = tomllib.loads(manifest.read_text())["project"].get("scripts", {})
        assert scripts, f"{service.name} declares no [project.scripts]; nothing of it would be booted"
        entry_points.extend((service.name, name, target) for name, target in sorted(scripts.items()))
    return tuple(entry_points)


def rogue_config(path: Path) -> Path:
    """A routing table that binds every production alias to a non-approved endpoint.

    Every alias, because `validate_routing` checks for unbound aliases first: a
    config missing one would refuse for *that* reason and this harness would be
    proving that an incomplete config is rejected, not that an off-allowlist one is.
    """
    entries = "\n".join(
        f"  - model_name: {alias}\n"
        f"    litellm_params:\n"
        f"      model: hosted_vllm/qwen3-8b-instruct\n"
        f"      api_base: {ROGUE_ENDPOINT}"
        for alias in sorted(PRODUCTION_ALIASES)
    )
    path.write_text(f"model_list:\n{entries}\n")
    return path


class Boots:
    """Starts entry points, and counts the ones it actually started.

    The counter lives here rather than in the loop that calls it on purpose. A
    tally appended next to the assertions is equal to the enumeration by
    construction — it cannot fail, so a test that offers it as evidence is
    offering nothing. Counted at the point a process is spawned, it disagrees
    with the enumeration the moment a loop skips, filters or short-circuits past
    an entry point, which is the failure this number is supposed to catch.
    """

    def __init__(self) -> None:
        self.started: list[str] = []

    def __call__(self, target: str, env: dict[str, str], argument: Path) -> subprocess.CompletedProcess[str]:
        """Start one entry point the way its console script does, in a clean environment.

        `sys.executable -c` rather than the installed script: the same interpreter
        the tests run under, with no PATH resolution in between. The argument is
        passed to every entry point uniformly — the ones that ignore argv ignore
        it, and the exporter writes to a temporary file instead of the working tree.
        """
        module, _, attribute = target.partition(":")
        code = f"import sys; from {module} import {attribute}; sys.exit({attribute}())"
        self.started.append(target)
        return subprocess.run(
            [sys.executable, "-c", code, str(argument)],
            env=env,
            capture_output=True,
            text=True,
            timeout=BOOT_TIMEOUT_SECONDS,
            check=False,
        )


def environment(**steward: str) -> dict[str, str]:
    """A hermetic environment: PATH, and exactly the STEWARD_ variables named."""
    return {"PATH": os.environ.get("PATH", ""), **steward}


def test_every_service_entry_point_refuses_an_off_allowlist_gateway(tmp_path: Path) -> None:
    entry_points = service_entry_points()
    assert entry_points, "enumerated no service entry points — this harness would prove nothing"

    rogue = environment(
        STEWARD_LITELLM_CONFIG=str(rogue_config(tmp_path / "rogue.yaml")),
        STEWARD_LLM_APPROVED_ENDPOINTS=APPROVED_ENDPOINT,
    )
    boots = Boots()
    refused: list[str] = []
    for service, script, target in entry_points:
        booted = boots(target, rogue, tmp_path / f"{script}.out")
        output = booted.stdout + booted.stderr
        where = f"{service}:{script} ({target})"
        assert booted.returncode != 0, f"{where} started against {ROGUE_ENDPOINT}\n{output}"
        assert NonApprovedEndpoint.__name__ in output, f"{where} exited for another reason\n{output}"
        assert ROGUE_ENDPOINT in output, f"{where} did not name the endpoint it refused\n{output}"
        refused.append(where)

    assert boots.started == [target for _, _, target in entry_points]
    print(f"refused by {len(refused)} entry points: {', '.join(refused)}")


def test_the_committed_config_is_not_refused_by_any_entry_point(tmp_path: Path) -> None:
    """The control: without it, an entry point that refused every config it was
    given — or one whose refusal was hard-coded rather than read — would satisfy
    the test above."""
    entry_points = service_entry_points()
    assert entry_points, "enumerated no service entry points — this harness would prove nothing"

    approved = environment(STEWARD_LITELLM_CONFIG=str(COMMITTED_CONFIG))
    boots = Boots()
    checked: list[str] = []
    for service, script, target in entry_points:
        booted = boots(target, approved, tmp_path / f"{script}.out")
        output = booted.stdout + booted.stderr
        where = f"{service}:{script} ({target})"
        assert output.strip(), f"{where} produced no output at all — absence proves nothing here"
        assert GatewayConfigError.__name__ not in output, f"{where} refused the shipped config\n{output}"
        assert NonApprovedEndpoint.__name__ not in output, f"{where} refused the shipped config\n{output}"
        checked.append(where)

    assert boots.started == [target for _, _, target in entry_points]
    print(f"accepted by {len(checked)} entry points: {', '.join(checked)}")
