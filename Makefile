# Common commands. `make demo-local` needs no Docker; `make demo` uses docker compose.
VENV := .venv
BIN := $(VENV)/bin
WEEK ?= 2026-11-02

.PHONY: install lint test test-pg demo-local up down demo demo-llm clean

install:
	uv venv $(VENV) && uv pip install -e ".[dev]"

lint:
	$(BIN)/ruff check .

test:
	$(BIN)/pytest

# Needs a reachable PostgreSQL, e.g. `make up` then:
test-pg:
	TEST_PG_URL=postgresql+psycopg://crm:crm@localhost:5432/crm $(BIN)/pytest -m integration

# SQLite + in-process mock CRM: the whole flow without Docker or an API key.
demo-local:
	$(BIN)/crm-pipeline run --in-process-mock --db-url sqlite:///crm.db
	$(BIN)/crm-pipeline report --db-url sqlite:///crm.db --week $(WEEK) --no-llm

up:
	docker compose up -d --build db mock-crm

down:
	docker compose down -v

demo: up
	docker compose run --rm pipeline run
	docker compose run --rm pipeline report --week $(WEEK) --no-llm

# Same, with the Claude summary (needs ANTHROPIC_API_KEY; makes a paid API call).
demo-llm: up
	docker compose run --rm pipeline run
	docker compose run --rm pipeline report --week $(WEEK)

clean:
	rm -f crm.db reports/weekly-*
