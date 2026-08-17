"""Layer 1 profiler configuration loader and identity.

Loads ``configs/layer1/default.yaml`` and exposes a frozen, typed view plus the
canonical ``config_hash`` bound into every result and the L1-READY gate. The hash
is over the *semantic* config values (YAML comments are not part of the parse), so
any behavioral change to the config changes the fingerprint by design.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import ConfigValidationError
from minos_engine.common.hashing import canonical_hash

__all__ = ["Layer1Config", "load_layer1_config", "default_config_path"]


def default_config_path() -> Path:
    # src/minos_engine/layer1/config.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3] / "configs" / "layer1" / "default.yaml"


class WindowCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    primary_bp: int
    refinement_bp: int


class BudgetCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    soft_seconds: float
    hard_seconds: float
    serialization_reserve_seconds: float


class PileupCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    soft_seconds: float
    max_depth: int
    stepper: str
    min_base_quality: int
    min_mapping_quality: int
    ignore_overlaps: bool
    compute_baq: bool


class CoverageCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    overlap_policy: str
    count_deletions_in_depth: bool
    depth_bins: tuple[int, ...]
    quantiles: tuple[float, ...]


class Layer1Config(BaseModel):
    """Frozen, validated Layer 1 configuration with a canonical identity hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    profiler_config_version: str
    window: WindowCfg
    budget: BudgetCfg
    pileup: PileupCfg
    coverage: CoverageCfg
    thresholds: dict[str, Any]
    reference: dict[str, Any]
    sampling: dict[str, Any]
    confidence: dict[str, Any]
    quantiles: dict[str, Any]
    filters: dict[str, Any]
    cost_model: dict[str, float]
    config_hash: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Layer1Config:
        data = dict(raw)
        data.pop("implemented", None)  # documentation flag only
        config_hash = canonical_hash(_canonical_semantics(raw))
        return cls(
            schema_version=data["schema_version"],
            profiler_config_version=data["profiler_config_version"],
            window=WindowCfg(**data["window"]),
            budget=BudgetCfg(**data["budget"]),
            pileup=PileupCfg(**data["pileup"]),
            coverage=CoverageCfg(**data["coverage"]),
            thresholds=data["thresholds"],
            reference=data["reference"],
            sampling=data["sampling"],
            confidence=data["confidence"],
            quantiles=data["quantiles"],
            filters=data["filters"],
            cost_model={k: float(v) for k, v in data["cost_model"].items()},
            config_hash=config_hash,
        )


def _canonical_semantics(raw: dict[str, Any]) -> dict[str, Any]:
    """The hashable semantic view (drops the documentation-only ``implemented`` flag)."""
    view = {k: v for k, v in raw.items() if k != "implemented"}
    return view


@lru_cache(maxsize=4)
def load_layer1_config(path: str | None = None) -> Layer1Config:
    p = Path(path) if path else default_config_path()
    if not p.is_file():
        raise ConfigValidationError(f"Layer 1 config not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigValidationError("Layer 1 config must be a mapping")
    try:
        return Layer1Config.from_mapping(raw)
    except KeyError as exc:  # missing required key
        raise ConfigValidationError(f"Layer 1 config missing key: {exc}") from exc
