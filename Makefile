.PHONY: setup dev api ui build test lint eval seed clean docker help
.DEFAULT_GOAL := help

VENV := .venv
PY   := $(VENV)/bin/python

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:  ## Install everything (Python + UI) and build the front end
	@command -v uv >/dev/null || { echo "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	uv venv -p 3.13 $(VENV)
	VIRTUAL_ENV=$(PWD)/$(VENV) uv pip install -e ".[dev]"
	cd ui && npm install && npm run build
	@test -f .env || cp .env.example .env
	@echo
	@echo "Setup complete. Put your key in .env, then:  make dev"

dev:  ## Run the server (API + built UI) on :8000
	$(PY) -m uvicorn app.api.main:app --reload --port 8000

api:  ## Run the API only, no reload
	$(PY) -m uvicorn app.api.main:app --port 8000

ui:  ## Run the Vite dev server on :5173 with hot reload
	cd ui && npm run dev

build:  ## Rebuild the front end
	cd ui && npm run build

test:  ## Run the test suite (no model calls)
	$(PY) -m pytest -q

lint:  ## Lint and format-check
	$(PY) -m ruff check app tests eval
	$(PY) -m ruff format --check app tests eval

eval:  ## Run the evaluation set against a running server
	$(PY) eval/run_eval.py

seed:  ## Load the sample datasets into a running server
	$(PY) scripts/seed.py

clean:  ## Remove data, caches and build output
	rm -rf data/datasets data/*.sqlite* ui/dist .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

docker:  ## Build and run in Docker
	docker compose up --build
