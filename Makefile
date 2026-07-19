# Dev convenience targets. Assumes the venv at .venv and Homebrew Postgres 16.
# `make dev` runs the reloading server; `make migrate` applies migrations.

PG_BIN := /usr/local/opt/postgresql@16/bin
PY     := .venv/bin/python
PIP    := .venv/bin/pip
export PATH := $(PG_BIN):$(PATH)

.PHONY: help
help:
	@echo "make dev        - run the dev server with autoreload on :8000"
	@echo "make migrate    - alembic upgrade head"
	@echo "make revision   - autogenerate a migration (m=\"message\")"
	@echo "make db-up       - start local Postgres 16"
	@echo "make db-shell   - psql into the nextgen database"
	@echo "make install    - install pinned dependencies"

.PHONY: install
install:
	$(PIP) install -r requirements.txt

.PHONY: db-up
db-up:
	brew services start postgresql@16

.PHONY: db-shell
db-shell:
	$(PG_BIN)/psql -d nextgen

.PHONY: migrate
migrate:
	.venv/bin/alembic upgrade head

.PHONY: revision
revision:
	.venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: dev
dev:
	.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: test
test:
	$(PG_BIN)/createdb nextgen_test 2>/dev/null || true
	.venv/bin/python -m pytest tests/ -q
