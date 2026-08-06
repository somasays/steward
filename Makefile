# Steward — developer entrypoints. See CLAUDE.md for the workflow, GUARDRAILS.md for what gates mean.

.PHONY: fitness hooks lint type test evals

fitness:            ## Run the fitness suite: S/H/B tiers + hygiene (GUARDRAILS.md §1)
	python3 scripts/fitness/run.py

hooks:              ## Install git hooks (run once after clone)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/commit-msg
	@echo "hooks installed (core.hooksPath -> .githooks)"

lint:               ## G1
	uv run ruff check . && uv run ruff format --check .

type:               ## G2
	uv run mypy --strict packages

test:               ## G3
	uv run pytest -q -m "not acceptance" --cov=packages --cov-branch --cov-fail-under=85

evals:              ## B tier (from M2)
	uv run steward evals run
