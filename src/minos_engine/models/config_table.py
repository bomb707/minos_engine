"""Verify the 80 frozen config payloads and encode them through the ACCEPTED encoder.

The payloads are content-addressed on disk, so their location is an operational handle and cannot
change the science: each file must hash to the ``config_hash`` that names it, and a payload that
does not is refused rather than relocated. No model may consume a caller-created config vector —
the 28 columns come from ``build_config_encoding()`` and nowhere else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "CONFIG_PAYLOAD_ROOT",
    "ConfigTableError",
    "load_verified_config_vectors",
]

#: where the frozen content-addressed payloads live. An operational handle: every file is
#: verified against the hash that names it, so a wrong root fails rather than substitutes.
CONFIG_PAYLOAD_ROOT: Final = Path("/home/hr/bittensor/minos_l2f2_baseline/config_artifacts")

EXPECTED_CONFIG_COUNT: Final = 80
EXPECTED_PARAMETER_COUNT: Final = 25
EXPECTED_ENCODED_COLUMNS: Final = 28


class ConfigTableError(MinosEngineError):
    """A frozen config payload does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigTableError(message)


def load_verified_config_vectors(
    *,
    config_hashes: tuple[str, ...],
    payload_root: Path | None = None,
) -> tuple[dict[str, tuple[float, ...]], tuple[str, ...]]:
    """Return ``config_hash -> 28 encoded columns`` plus the encoder's column names."""
    from minos_engine.experiments.gatk_live_space import (
        load_committed_live_gatk_parameter_space,
    )
    from minos_engine.models.config_encoder import build_config_encoding

    root = payload_root or CONFIG_PAYLOAD_ROOT
    _require(root.is_dir(), f"the config payload root is missing: {root}")
    _require(
        len(config_hashes) == EXPECTED_CONFIG_COUNT,
        f"{len(config_hashes)} config hashes, expected {EXPECTED_CONFIG_COUNT}",
    )
    _require(len(set(config_hashes)) == len(config_hashes), "a config hash appears twice")

    space = load_committed_live_gatk_parameter_space()
    frozen_names = {p.name for p in space.parameters}
    _require(
        len(frozen_names) == EXPECTED_PARAMETER_COUNT,
        f"the frozen space declares {len(frozen_names)} parameters, expected "
        f"{EXPECTED_PARAMETER_COUNT}",
    )
    encoding = build_config_encoding()

    vectors: dict[str, tuple[float, ...]] = {}
    for config_hash in sorted(config_hashes):
        path = root / f"{config_hash}.json"
        _require(path.is_file(), f"no frozen payload for config {config_hash}")
        _require(not path.is_symlink(), f"{path} is a symlink")
        payload: Any = json.loads(path.read_bytes())
        _require(isinstance(payload, dict), f"{config_hash} payload is not an object")
        # the file must earn the name it is stored under
        recomputed = sha256_hex(canonical_json_bytes(payload))
        _require(
            recomputed == config_hash,
            f"payload {path.name} hashes to {recomputed}; the stored identity does not describe "
            "its own bytes",
        )
        _require(
            set(payload) == frozen_names,
            f"{config_hash} is not a complete frozen configuration: "
            f"{sorted(set(payload) ^ frozen_names)}",
        )
        vector = tuple(float(v) for v in encoding.encode(payload))
        _require(
            len(vector) == EXPECTED_ENCODED_COLUMNS,
            f"{config_hash} encoded to {len(vector)} columns, expected {EXPECTED_ENCODED_COLUMNS}",
        )
        vectors[config_hash] = vector
    return vectors, tuple(encoding.feature_names)
