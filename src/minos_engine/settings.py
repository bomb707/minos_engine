"""The single typed settings layer.

All configuration loads through here (assignment §13). Domain modules never do
their own environment lookups, argument parsing, or ad-hoc YAML reading. Code
defaults live on the models and must equal the values in ``configs/`` — a parity
test enforces this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.errors import PolicyViolationError

__all__ = ["RuntimePolicy", "EngineConfig", "Settings", "config_dir"]

ACTIVE_CALLER = "gatk"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return _repo_root() / "configs"


class RuntimePolicy(BaseModel):
    """GATK-only caller policy (configs/runtime/gatk_only.yaml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "runtime-policy-v1"
    active: str = ACTIVE_CALLER
    allowed: tuple[str, ...] = (ACTIVE_CALLER,)
    disabled: tuple[str, ...] = ("deepvariant", "bcftools", "freebayes")

    @field_validator("active")
    @classmethod
    def _active_is_gatk(cls, v: str) -> str:
        if v != ACTIVE_CALLER:
            raise PolicyViolationError(f"active caller must be '{ACTIVE_CALLER}', got {v!r}")
        return v

    @field_validator("allowed")
    @classmethod
    def _allowed_only_gatk(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(v) != (ACTIVE_CALLER,):
            raise PolicyViolationError(
                f"GATK-only policy allows only ('{ACTIVE_CALLER}',), got {v!r}"
            )
        return v

    def is_selectable(self, caller: str) -> bool:
        return caller in self.allowed


class EngineConfig(BaseModel):
    """Engine timing + truth-isolation config (configs/engine/default.yaml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "engine-config-v1"
    round_duration_seconds: int = 4320
    prediction_target_seconds: int = 300
    final_safety_reserve_seconds: int = 300
    truth_isolation_enabled: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)
    engine: EngineConfig = Field(default_factory=EngineConfig)

    @classmethod
    def load(cls, base_dir: Path | None = None) -> Settings:
        base = base_dir or config_dir()
        runtime_raw = _read_yaml(base / "runtime" / "gatk_only.yaml")
        engine_raw = _read_yaml(base / "engine" / "default.yaml")
        return cls(
            runtime_policy=_runtime_from_yaml(runtime_raw),
            engine=_engine_from_yaml(engine_raw),
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} did not parse to a mapping")
    return data


def _runtime_from_yaml(raw: dict[str, Any]) -> RuntimePolicy:
    caller = raw.get("caller", {})
    return RuntimePolicy(
        schema_version=raw.get("schema_version", "runtime-policy-v1"),
        active=caller.get("active", ACTIVE_CALLER),
        allowed=tuple(caller.get("allowed", (ACTIVE_CALLER,))),
        disabled=tuple(caller.get("disabled", ())),
    )


def _engine_from_yaml(raw: dict[str, Any]) -> EngineConfig:
    round_cfg = raw.get("round", {})
    pred = raw.get("prediction", {})
    truth = raw.get("truth_isolation", {})
    return EngineConfig(
        schema_version=raw.get("schema_version", "engine-config-v1"),
        round_duration_seconds=round_cfg.get("duration_seconds", 4320),
        prediction_target_seconds=pred.get("target_seconds", 300),
        final_safety_reserve_seconds=pred.get("final_safety_reserve_seconds", 300),
        truth_isolation_enabled=truth.get("enabled", True),
    )
