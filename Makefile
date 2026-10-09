.PHONY: help setup install uninstall test lint fmt check refresh clean


UV ?= uv

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies into .venv (uv sync)
	$(UV) sync

install: ## Install x2llm system-wide via uv tool
	$(UV) tool install --upgrade .

uninstall: ## Remove system-wide x2llm
	$(UV) tool uninstall x2llm

test: ## Run test suite
	$(UV) run pytest -q

lint: ## Run ruff checks
	$(UV) run ruff check src tests scripts

fmt: ## Ruff autofix
	$(UV) run ruff check --fix src tests scripts

check: lint test ## Everything a change must pass

refresh: ## Harvest HAR: propose registry.new.py + upsert account into local db
	$(UV) run python scripts/har_to_endpoints.py '$(HAR)'

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache __pycache__ src/x2llm/__pycache__
