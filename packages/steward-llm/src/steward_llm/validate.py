"""`python -m steward_llm.validate` — the startup refusal, run over the committed config.

S9 uses this: the same code path a process runs at boot, applied to the LiteLLM config
and the allowlist in git, so a commit that points a production alias somewhere else
fails the fitness gate instead of failing in a cluster.
"""

from __future__ import annotations

import sys

from steward_llm.config import committed_production_config
from steward_llm.endpoints import GatewayConfigError


def main() -> int:
    try:
        config = committed_production_config()
    except GatewayConfigError as exc:
        print(f"gateway config REFUSED: {exc}")
        return 1
    print(
        f"gateway config OK: {len(config.bindings)} models on "
        f"{len(config.allowlist.approved)} approved endpoints ({config.mode.value})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
