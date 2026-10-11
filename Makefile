.PHONY: help setup install uninstall test lint fmt check clean


UV ?= uv

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies into .venv (uv sync)
	$(UV) sync

install: ## Install/refresh x2llm system-wide via uv tool (force = same-version reinstalls too)
	$(UV) tool install --upgrade --force .

uninstall: ## Remove system-wide x2llm
	$(UV) tool uninstall x2llm

test: ## Run test suite
	$(UV) run pytest -q

lint: ## Run ruff checks
	$(UV) run ruff check src tests

fmt: ## Ruff autofix
	$(UV) run ruff check --fix src tests

check: lint test ## Everything a change must pass

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache __pycache__ src/x2llm/__pycache__
