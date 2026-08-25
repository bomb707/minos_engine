"""L2-F F5 accepted CONFIG-artifact byte validation.

Reads the EXACT persisted CONFIG artifact bytes for one accepted plan-config and proves they are
still the accepted canonical CONFIG before any GATK process is started. A CONFIG the live scoring
API would silently default can never reach execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import ConfigArtifactError, ExecutionConfig
from minos_engine.experiments.gatk_live_space import (
    canonicalize_live_gatk_config,
    live_gatk_parameter_space,
)
from minos_engine.storage.l2f_config_publisher import CONFIG_ARTIFACT_MEDIA_TYPE
from minos_engine.storage.l2f_plan_store import _file_path_from_uri

__all__ = ["load_accepted_execution_config"]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ConfigArtifactError(f"duplicate JSON key {key!r} in a CONFIG artifact")
        seen[key] = value
    return seen


def load_accepted_execution_config(
    conn: Connection, *, plan_id: str, plan_config_id: str
) -> ExecutionConfig:
    """Read + fully validate the persisted CONFIG artifact for one accepted plan-config.

    1. read the EXACT bytes from the persisted content-addressed artifact;
    2. recompute SHA-256 and require ``config_hash`` equality;
    3. reject duplicate JSON keys;
    4. require ``canonical_json_bytes(parsed)`` to equal the stored bytes;
    5. re-canonicalize through ``canonicalize_live_gatk_config``;
    6. require ``effective_config`` and ``config_hash`` to be unchanged;
    7. require the committed live parameter-space identity.
    """
    row = (
        conn.execute(
            text(
                "SELECT pc.config_index, pc.config_hash, pc.parameter_space_hash, "
                "       cp.media_type, a.uri, a.sha256, a.size_bytes "
                "  FROM experiments.l2f_experiment_plan_configs pc "
                "  JOIN experiments.l2f_config_payloads cp ON cp.id = pc.config_payload_id "
                "  JOIN catalog.artifacts a ON a.id = cp.artifact_id "
                " WHERE pc.plan_id = :p AND pc.id = :c"
            ),
            {"p": plan_id, "c": plan_config_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ConfigArtifactError(
            f"plan config {plan_config_id} of plan {plan_id} has no persisted CONFIG payload"
        )
    return validate_execution_config_artifact(dict(row))


def validate_execution_config_artifact(row: Mapping[str, Any]) -> ExecutionConfig:
    """Read and fully validate one persisted CONFIG artifact from its catalog row.

    The SHARED core. ``load_accepted_execution_config`` supplies ``row`` from a direct SELECT;
    the L2-F2 runner supplies the identical column set from a ``SECURITY DEFINER`` function.
    Every byte, hash, canonicality and live-domain check is performed identically on both paths.
    """
    if row["media_type"] != CONFIG_ARTIFACT_MEDIA_TYPE:
        raise ConfigArtifactError(
            f"CONFIG payload media_type {row['media_type']!r} is not the canonical L2-F type"
        )

    config_hash = str(row["config_hash"])
    path: Path = _file_path_from_uri(str(row["uri"]))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigArtifactError(f"CONFIG artifact {path} is unreadable: {exc}") from exc

    actual = hashlib.sha256(raw).hexdigest()
    if actual != config_hash:
        raise ConfigArtifactError(
            f"CONFIG artifact {path} sha256 {actual} != recorded config_hash {config_hash}"
        )
    if str(row["sha256"]) != config_hash:
        raise ConfigArtifactError(
            "catalog.artifacts sha256 does not equal the recorded config_hash"
        )
    if row["size_bytes"] is None or int(row["size_bytes"]) != len(raw):
        raise ConfigArtifactError("catalog.artifacts size_bytes does not match the artifact bytes")

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except ConfigArtifactError:
        raise
    except Exception as exc:
        raise ConfigArtifactError(f"CONFIG artifact {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigArtifactError(f"CONFIG artifact {path} must be a JSON object")
    if canonical_json_bytes(parsed) != raw:
        raise ConfigArtifactError(
            f"CONFIG artifact {path} bytes are not the canonical serialization of their content"
        )

    recanon = canonicalize_live_gatk_config(parsed)
    if recanon.effective_config != parsed:
        raise ConfigArtifactError(
            "the stored CONFIG does not re-canonicalize to itself under the live GATK domain"
        )
    if recanon.config_hash != config_hash:
        raise ConfigArtifactError(
            f"re-canonicalized config_hash {recanon.config_hash} != stored {config_hash}"
        )
    live_space_hash = live_gatk_parameter_space().parameter_space_hash
    for label, value in (
        ("plan-config", str(row["parameter_space_hash"])),
        ("re-canonicalized", str(recanon.parameter_space_hash)),
    ):
        if value != live_space_hash:
            raise ConfigArtifactError(
                f"{label} parameter_space_hash {value} != the committed live-GATK identity "
                f"{live_space_hash}"
            )
    return ExecutionConfig(
        config_hash=config_hash,
        parameter_space_hash=live_space_hash,
        config_index=int(row["config_index"]),
        effective_config=dict(recanon.effective_config),
    )
