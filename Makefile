.PHONY: help setup install uninstall test lint fmt check clean docker release


UV ?= uv
IMAGE ?= ghcr.io/xeroc/x2llm
BUMP ?= patch
NV := $(shell date +%Y.%m.%d%H%M)

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

docker: ## Build ghcr.io/xeroc/x2llm:dev from the working tree
	docker build -t $(IMAGE):dev .

release: ## Bump version (BUMP=patch|minor|major), commit, tag, build+push image, push git
	@test -z "$$(git status --porcelain)" || { echo '!! clean working tree required'; exit 1; }; \
	sed -i 's/version = .*$$/version = "$(NV)"/' pyproject.toml
	echo "== releasing v$(NV)"; \
	$(UV) lock && \
	git add pyproject.toml uv.lock && \
	git commit -m "chore: release v$(NV)" && \
	git tag v$(NV) && \
	git push && git push origin v$(NV) || { echo '!! release incomplete - finish the remaining steps manually'; exit 1; }

docker_build:
	docker build --build-arg VERSION=$(NV) -t $(IMAGE):$(NV) -t $(IMAGE):latest .

docker_push:
	docker push $(IMAGE):$(NV)
	docker push $(IMAGE):latest

docker: docker_build docker_push

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache __pycache__ src/x2llm/__pycache__
