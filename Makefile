# Steward — developer entrypoints. See CLAUDE.md for the workflow, GUARDRAILS.md for what gates mean.

.PHONY: fitness hooks lint type test evals

fitness:            ## Run all fitness functions (F1-F9)
	python3 scripts/fitness/run.py

hooks:              ## Install git hooks (run once after clone)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/commit-msg
	@echo "hooks installed (core.hooksPath -> .githooks)"

lint:               ## F6
	uv run ruff check . && uv run ruff format --check .

type:               ## F7
	uv run mypy --strict packages

test:               ## F8
	uv run pytest -q --cov=packages --cov-branch --cov-fail-under=85

evals:              ## F9 (from M2)
	uv run steward evals run
