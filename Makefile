# MINOS_ENGINE — Stage 0 Makefile
# Thin wrappers over the local virtualenv toolchain.

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
PYTEST ?= .venv/bin/pytest

.PHONY: help bootstrap lint fmt-check typecheck test test-fast test-full cov \
        protocol-contract doctor gate stage0 clean-test-artifacts qualify-local

help:
	@echo "MINOS_ENGINE Stage 0 targets:"
	@echo "  bootstrap          create venv and install dev deps"
	@echo "  lint               ruff check"
	@echo "  fmt-check          ruff format --check"
	@echo "  typecheck          mypy src"
	@echo "  test               pytest"
	@echo "  cov                pytest with coverage"
	@echo "  protocol-contract  run protocol_contract test suite"
	@echo "  doctor             minos-engine doctor --json"
	@echo "  gate               build reports + protocol-ready gate"
	@echo "  stage0             lint + fmt-check + typecheck + cov"

bootstrap:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

lint:
	$(RUFF) check .

fmt-check:
	$(RUFF) format --check .

typecheck:
	$(MYPY) src

test:
	$(PYTEST)

cov:
	$(PYTEST) --cov=src/minos_engine --cov-report=term-missing

protocol-contract:
	$(PYTEST) tests/protocol_contract

doctor:
	$(PY) -m minos_engine.cli.main doctor --json

gate:
	$(PY) scripts/build_protocol_ready_gate.py

layer1-qualify:
	$(PY) -m minos_engine.cli.main layer1 qualify --json

stage0: lint fmt-check typecheck cov
	@echo "Stage 0 checks complete."

# --- test execution (see docs/testing/TEST_STRATEGY.md) -----------------------
# GitHub runs the fast tier only. Full qualification is this MANUAL local command.
#
# It refuses to start if the caller supplied ANY database configuration -- MINOS_DATABASE_URL or
# a libpq routing variable (PGHOST, PGPORT, PGDATABASE, PGSERVICE, ...) -- whatever it points at.
# PostgreSQL is always provisioned by the repository's isolated test fixtures. Unset those
# variables before running; there is no override. Every tool is resolved through $(PY), so
# .venv/bin does not need to be on PATH.
qualify-local:
	$(PY) scripts/local_qualification.py


test-fast:
	$(PY) -m pytest tests/unit tests/leakage tests/determinism tests/protocol_contract

test-full:
	$(PY) -m pytest \
	  --junitxml=reports/ci-junit.xml \
	  --cov=src/minos_engine \
	  --cov-fail-under=90 \
	  --cov-report=term-missing \
	  --cov-report=xml:reports/ci-coverage.xml

# Removes ONLY known tool caches and transient CI XML, each named explicitly.
# It never touches reports/, gates/, manifests/, evidence or runtime state, and it
# never recursively removes a directory it was not given by name.
clean-test-artifacts:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -f .coverage .coverage.*
	rm -f reports/ci-junit.xml reports/ci-coverage.xml
	rm -f reports/ci-db-junit.xml reports/ci-ingest-junit.xml
	rm -f reports/ci-split-junit.xml reports/ci-split-v2-junit.xml
	find src tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "Removed tool caches and transient CI XML only."
