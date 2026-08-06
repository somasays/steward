"""Run the API service: `python -m steward_api` (dev) or the `steward-api`
console script (`[project.scripts]` in pyproject.toml)."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("steward_api.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
