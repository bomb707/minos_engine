# MINOS_ENGINE — Stage 0 Makefile
# Thin wrappers over the local virtualenv toolchain.

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
PYTEST ?= .venv/bin/pytest

.PHONY: help bootstrap lint fmt-check typecheck test cov protocol-contract doctor gate stage0

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
