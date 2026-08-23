"""Recompute every accepted identity a HARNESS-READY qualification binds, from real bytes.

Nothing here is supplied by a caller and nothing is a shape check. Each value is derived from the
committed repository bytes, the committed gate artifacts or the accepted generators, and is then
required to EQUAL the accepted constant. ``bool(policy_hash)`` or "is 64 hex characters" is not
accepted anywhere as proof of a scientific identity.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from minos_engine.common.errors import MinosEngineError
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_ALEMBIC_HEAD,
    ACCEPTED_CANDIDATE_COUNT,
    ACCEPTED_CANDIDATE_SET_HASH,
    ACCEPTED_E5_GATE_HASHES,
    ACCEPTED_F5_CONTRACT_HASH,
    ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256,
    ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256,
    ACCEPTED_LOGICAL_JOB_COUNT,
    ACCEPTED_MIGRATION_SHAS,
    ACCEPTED_PARAMETER_SPACE_HASH,
    ACCEPTED_PLAN_HASH,
    ACCEPTED_POLICY_HASH,
    AcceptedIdentities,
)

__all__ = [
    "AcceptedIdentityError",
    "repository_root",
    "recompute_accepted_identities",
    "verify_accepted_identities",
    "recompute_e5_gate_hashes",
    "recompute_migration_sha256",
    "recompute_live_gatk_artifact_sha256",
    "recompute_alembic_head",
]


class AcceptedIdentityError(MinosEngineError):
    """A recomputed accepted identity did not equal its accepted value."""


def repository_root() -> Path:
    """The repository root that owns the committed manifests, migrations and gates."""
    return Path(__file__).resolve().parents[3]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def recompute_migration_sha256(root: Path | None = None) -> dict[str, str]:
    """Byte SHA-256 of every accepted L2-F migration, read from the committed files."""
    base = root or repository_root()
    out: dict[str, str] = {}
    for relative in ACCEPTED_MIGRATION_SHAS:
        path = base / relative
        if not path.is_file():
            raise AcceptedIdentityError(f"accepted migration is missing: {relative}")
        out[relative] = _sha256_file(path)
    return out


def recompute_live_gatk_artifact_sha256(root: Path | None = None) -> tuple[str, str]:
    """``(source_artifact_sha256, parameter_space_artifact_sha256)`` from the committed bytes."""
    from minos_engine.experiments.gatk_live_space import (
        LIVE_MANIFEST_PATH,
        LIVE_SOURCE_ARTIFACT_PATH,
    )

    base = root or repository_root()
    for relative in (LIVE_SOURCE_ARTIFACT_PATH, LIVE_MANIFEST_PATH):
        if not (base / relative).is_file():
            raise AcceptedIdentityError(f"committed live-GATK artifact is missing: {relative}")
    return (
        _sha256_file(base / LIVE_SOURCE_ARTIFACT_PATH),
        _sha256_file(base / LIVE_MANIFEST_PATH),
    )


def recompute_e5_gate_hashes(root: Path | None = None) -> dict[str, str]:
    """Load both accepted L2-E gates and recompute their real gate hashes + integrity closure."""
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import load_gate, verify_gate_integrity

    base = root or repository_root()
    out: dict[str, str] = {}
    for gate_name, relative in (
        ("FEATURE-VIEW-READY", "gates/feature-view-ready.json"),
        ("FEATURE-MATRIX-FROZEN-1", "gates/feature-matrix-frozen-1.json"),
    ):
        path = base / relative
        if not path.is_file():
            raise AcceptedIdentityError(f"accepted E5 gate is missing: {relative}")
        gate = load_gate(path)
        if gate.gate_name != gate_name:
            raise AcceptedIdentityError(
                f"{relative} declares gate_name {gate.gate_name!r}, expected {gate_name!r}"
            )
        if gate.status is not GateStatus.PASS:
            raise AcceptedIdentityError(f"accepted E5 gate {gate_name} is not PASS")
        integrity = verify_gate_integrity(gate, base_dir=base)
        if not integrity.ok:
            raise AcceptedIdentityError(
                f"accepted E5 gate {gate_name} failed integrity: {integrity.reasons}"
            )
        out[gate_name] = gate.gate_hash
    return out


def recompute_alembic_head(root: Path | None = None) -> str:
    """The single Alembic head, read from the committed migration lineage (never from a DB)."""
    base = root or repository_root()
    versions = base / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in sorted(versions.glob("[0-9]*.py")):
        revision: str | None = None
        down: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("revision:") or stripped.startswith("revision ="):
                revision = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            elif stripped.startswith("down_revision"):
                raw = stripped.split("=", 1)[1].strip()
                down = None if raw.startswith("None") else raw.strip('"').strip("'")
            if revision is not None and "down_revision" in stripped:
                break
        if revision:
            revisions[revision] = down
    heads = sorted(set(revisions) - {d for d in revisions.values() if d})
    if len(heads) != 1:
        raise AcceptedIdentityError(f"expected exactly one Alembic head, found {heads}")
    return heads[0]


def recompute_accepted_identities(root: Path | None = None) -> AcceptedIdentities:
    """Derive EVERY accepted identity from real committed bytes and accepted generators."""
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.experiments.candidates import (
        generate_accepted_candidate_set,
        verify_accepted_candidate_set,
    )
    from minos_engine.experiments.gatk_live_space import (
        load_committed_live_gatk_parameter_space,
    )
    from minos_engine.storage.l2f_execution_contract import compute_execution_contract_hash

    base = root or repository_root()
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)
    plan = build_accepted_experiment_plan()
    source_sha, space_sha = recompute_live_gatk_artifact_sha256(base)
    return AcceptedIdentities(
        e5_gate_hashes=recompute_e5_gate_hashes(base),
        migration_sha256=recompute_migration_sha256(base),
        f5_contract_hash=compute_execution_contract_hash(),
        live_gatk_source_artifact_sha256=source_sha,
        live_gatk_parameter_space_artifact_sha256=space_sha,
        parameter_space_hash=load_committed_live_gatk_parameter_space().parameter_space_hash,
        policy_hash=candidate_set.policy.experiment_parameter_policy_hash,
        candidate_set_hash=candidate_set.candidate_set_hash,
        candidate_count=candidate_set.candidate_count,
        plan_hash=plan.plan_hash,
        logical_job_count=plan.logical_job_count,
        alembic_head=recompute_alembic_head(base),
    )


def verify_accepted_identities(accepted: AcceptedIdentities) -> None:
    """Require every recomputed identity to EQUAL its accepted value. Fails closed."""
    expected: tuple[tuple[str, object, object], ...] = (
        ("e5_gate_hashes", accepted.e5_gate_hashes, ACCEPTED_E5_GATE_HASHES),
        ("migration_sha256", accepted.migration_sha256, ACCEPTED_MIGRATION_SHAS),
        ("f5_contract_hash", accepted.f5_contract_hash, ACCEPTED_F5_CONTRACT_HASH),
        (
            "live_gatk_source_artifact_sha256",
            accepted.live_gatk_source_artifact_sha256,
            ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256,
        ),
        (
            "live_gatk_parameter_space_artifact_sha256",
            accepted.live_gatk_parameter_space_artifact_sha256,
            ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256,
        ),
        ("parameter_space_hash", accepted.parameter_space_hash, ACCEPTED_PARAMETER_SPACE_HASH),
        ("policy_hash", accepted.policy_hash, ACCEPTED_POLICY_HASH),
        ("candidate_set_hash", accepted.candidate_set_hash, ACCEPTED_CANDIDATE_SET_HASH),
        ("candidate_count", accepted.candidate_count, ACCEPTED_CANDIDATE_COUNT),
        ("plan_hash", accepted.plan_hash, ACCEPTED_PLAN_HASH),
        ("logical_job_count", accepted.logical_job_count, ACCEPTED_LOGICAL_JOB_COUNT),
        ("alembic_head", accepted.alembic_head, ACCEPTED_ALEMBIC_HEAD),
    )
    for name, actual, wanted in expected:
        if actual != wanted:
            raise AcceptedIdentityError(
                f"accepted identity {name} is {actual!r}, expected {wanted!r}"
            )
