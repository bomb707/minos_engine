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
    "recompute_harness_alembic_head",
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
    """Recompute both accepted E5 gate hashes and run their ESTABLISHED ancestry closure.

    Generic ``verify_gate_integrity`` alone is not enough: each accepted L2-E gate has its own
    verifier that additionally proves qualified source/tree, ancestry and evidence closure, and
    those are what F7 requires. Both are run here, and neither accepted gate is modified.
    """
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import load_gate, verify_gate_integrity
    from minos_engine.qualification.layer2_feature_view_runner import (
        verify_feature_matrix_frozen_1_gate,
        verify_feature_view_ready_gate,
    )

    base = root or repository_root()
    closures = {
        "FEATURE-VIEW-READY": ("gates/feature-view-ready.json", verify_feature_view_ready_gate),
        "FEATURE-MATRIX-FROZEN-1": (
            "gates/feature-matrix-frozen-1.json",
            verify_feature_matrix_frozen_1_gate,
        ),
    }
    out: dict[str, str] = {}
    for gate_name, (relative, closure) in closures.items():
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
        # the ESTABLISHED gate-specific closure (source/tree/ancestry/evidence), not just integrity
        established = closure(base, path)
        if not established.ok:
            raise AcceptedIdentityError(
                f"accepted E5 gate {gate_name} failed its established ancestry closure: "
                f"{tuple(established.reasons)}"
            )
        out[gate_name] = gate.gate_hash
    return out


def _parse_migration_revision(path: Path) -> tuple[str | None, str | None]:
    """Read the real ``revision`` / ``down_revision`` a migration file declares."""
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
    return revision, down


def _single_head(revisions: dict[str, str | None], *, label: str) -> str:
    """The one head of a revision map, or a typed failure."""
    heads = sorted(set(revisions) - {d for d in revisions.values() if d})
    if len(heads) != 1:
        raise AcceptedIdentityError(f"expected exactly one {label} Alembic head, found {heads}")
    return heads[0]


def recompute_alembic_head(root: Path | None = None) -> str:
    """The CURRENT repository-wide Alembic head, from committed bytes (never from a DB).

    This scans every migration file, so it legitimately advances as later stages add additive
    migrations. It is deliberately NOT the authority for historical HARNESS evidence — see
    :func:`recompute_harness_alembic_head`.
    """
    base = root or repository_root()
    revisions: dict[str, str | None] = {}
    for path in sorted((base / "migrations" / "versions").glob("[0-9]*.py")):
        revision, down = _parse_migration_revision(path)
        if revision:
            revisions[revision] = down
    return _single_head(revisions, label="repository")


def recompute_harness_alembic_head(root: Path | None = None) -> str:
    """The head of the ACCEPTED HARNESS migration subgraph — stage-scoped, not repository-wide.

    HARNESS-READY proves the migration state of the L2-F1 source it qualified: that the accepted
    migrations exist, are byte-identical and form one coherent lineage ending at
    ``0008_l2f_execution_results``. It must NOT assert that the repository head stays 0008
    forever, or the first legitimate additive migration (0009) would retroactively invalidate
    frozen evidence.

    Membership is ``ACCEPTED_MIGRATION_SHAS`` — never filename numbering — so a later additive
    migration is ignored here while remaining visible to :func:`recompute_alembic_head`.
    """
    base = root or repository_root()
    revisions: dict[str, str | None] = {}
    for relative in sorted(ACCEPTED_MIGRATION_SHAS):
        path = base / relative
        if not path.is_file():
            raise AcceptedIdentityError(f"accepted migration {relative} is missing")
        revision, down = _parse_migration_revision(path)
        if not revision:
            raise AcceptedIdentityError(f"accepted migration {relative} declares no revision")
        if revision in revisions:
            raise AcceptedIdentityError(f"duplicate accepted migration revision {revision}")
        revisions[revision] = down
    # the earliest accepted migration legitimately points back at the pre-HARNESS boundary, so a
    # down_revision outside the accepted set is only allowed for exactly one entry point.
    external = sorted(r for r, d in revisions.items() if d is not None and d not in revisions)
    roots = sorted(r for r, d in revisions.items() if d is None) + external
    if len(roots) != 1:
        raise AcceptedIdentityError(
            f"accepted migrations must form ONE lineage; found entry points {roots}"
        )
    return _single_head(revisions, label="accepted HARNESS")


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
        # stage-scoped: the ACCEPTED subgraph head, not the repository's current head.
        alembic_head=recompute_harness_alembic_head(base),
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
