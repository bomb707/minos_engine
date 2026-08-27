"""Shared test fixtures and helpers.

Deterministic protocol payloads now live in production
(`minos_engine.qualification.fixtures`); tests import them from there. Tests may
import production; production must never import `tests.*`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from minos_engine.qualification.fixtures import (
    payload_missing_identity as payload_missing_identity,
)
from minos_engine.qualification.fixtures import (
    raw_response as make_raw_response,
)
from minos_engine.qualification.fixtures import (
    valid_raw_payload as make_raw_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
API_FIXTURES = FIXTURES / "api"
GATK_FIXTURES = FIXTURES / "gatk"
SRC = REPO_ROOT / "src" / "minos_engine"

__all__ = [
    "REPO_ROOT",
    "FIXTURES",
    "API_FIXTURES",
    "GATK_FIXTURES",
    "SRC",
    "make_raw_payload",
    "make_raw_response",
    "payload_missing_identity",
]


@pytest.fixture
def valid_round_path() -> Path:
    return API_FIXTURES / "valid_round.json"


@pytest.fixture
def parameter_space_path() -> Path:
    return API_FIXTURES / "gatk_parameter_space.json"


@pytest.fixture
def default_config_path() -> Path:
    return GATK_FIXTURES / "default_config.json"


@pytest.fixture(autouse=True)
def _restore_path_env() -> Any:
    """Restore ``PATH`` after every test.

    Provisioning a ``SubprocessGatkRunner`` puts its stub JVM's ``bin`` on ``PATH``, exactly as a
    real deployment does, because the GATK launcher resolves a bare ``java`` from there. That is a
    process-wide mutation, so it is undone here rather than left to leak into the next test.
    """
    before = os.environ.get("PATH")
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = before
