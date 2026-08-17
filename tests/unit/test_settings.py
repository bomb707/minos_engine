"""Configuration loads through one typed layer; code defaults == config defaults."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import PolicyViolationError
from minos_engine.settings import EngineConfig, RuntimePolicy, Settings


def test_code_defaults_equal_config_defaults():
    loaded = Settings.load()
    code = Settings()
    assert loaded.runtime_policy == code.runtime_policy
    assert loaded.engine == code.engine


def test_engine_config_default_values():
    e = EngineConfig()
    assert e.round_duration_seconds == 4320
    assert e.prediction_target_seconds == 300
    assert e.final_safety_reserve_seconds == 300
    assert e.truth_isolation_enabled is True


def test_runtime_policy_gatk_only():
    p = RuntimePolicy()
    assert p.active == "gatk"
    assert p.allowed == ("gatk",)
    assert p.is_selectable("gatk")
    assert not p.is_selectable("deepvariant")


def test_non_gatk_active_rejected():
    with pytest.raises(PolicyViolationError):
        RuntimePolicy(active="deepvariant")


def test_extra_allowed_caller_rejected():
    with pytest.raises(PolicyViolationError):
        RuntimePolicy(allowed=("gatk", "deepvariant"))
