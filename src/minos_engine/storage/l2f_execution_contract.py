"""Frozen contract for the L2-F F5 migration ``0008_l2f_execution_results``.

Freezes the migration's byte identity, its revision lineage, the accepted prior-migration byte
hashes, the two owned tables, the bounded failure vocabulary, the published media types and the
stable SQLSTATEs. Nothing here is derived from caller input.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "L2F_EXECUTION_REVISION",
    "L2F_EXECUTION_DOWN_REVISION",
    "L2F_EXECUTION_MIGRATION_FILE",
    "L2F_EXECUTION_OWNED_TABLE_NAMES",
    "L2F_EXECUTION_FAILURE_CODES",
    "L2F_VCF_MEDIA_TYPE",
    "L2F_RESULT_MANIFEST_MEDIA_TYPE",
    "L2F_EXECUTION_FUNCTIONS",
    "L2F_EXECUTION_SQLSTATES",
    "ACCEPTED_PRIOR_MIGRATION_SHAS",
    "compute_execution_migration_sha256",
    "compute_execution_contract_hash",
]

L2F_EXECUTION_REVISION = "0008_l2f_execution_results"
L2F_EXECUTION_DOWN_REVISION = "0007_l2f_job_claiming"
L2F_EXECUTION_MIGRATION_FILE = "migrations/versions/0008_l2f_execution_results.py"

L2F_EXECUTION_OWNED_TABLE_NAMES: tuple[str, ...] = (
    "l2f_execution_results",
    "l2f_execution_failures",
)

#: the complete bounded failure vocabulary — no free-text reason ever reaches the database.
L2F_EXECUTION_FAILURE_CODES: tuple[str, ...] = (
    "PREPARATION_FAILED",
    "GATK_NONZERO_EXIT",
    "GATK_TIMEOUT",
    "GATK_OUTPUT_INVALID",
    "GATK_OUTPUT_MISSING",
    "EXECUTION_ERROR",
)

L2F_VCF_MEDIA_TYPE = "application/vnd.ga4gh.vcf"
L2F_RESULT_MANIFEST_MEDIA_TYPE = "application/vnd.minos.l2f-execution-result+json"

#: the three narrowly scoped SECURITY DEFINER functions 0008 installs (identity arguments).
L2F_EXECUTION_FUNCTIONS: tuple[str, ...] = (
    "experiments.minos_l2f_resolve_running_job(text, uuid, text)",
    (
        "experiments.minos_l2f_complete_job_success(text, uuid, text, text, text, text, text, "
        "text, text, text, uuid, text, uuid, text, text, bigint)"
    ),
    "experiments.minos_l2f_fail_job(text, uuid, text, text, integer, text)",
)

#: stable SQLSTATEs raised by the 0008 functions/triggers.
L2F_EXECUTION_SQLSTATES: dict[str, str] = {
    "invalid_worker": "MN001",
    "plan_absent": "MN002",
    "not_owned": "MN003",
    "claim_invariant": "MN010",
    "claim_metadata": "MN011",
    "transition": "MN012",
    "missing_record": "MN020",
    "dual_outcome": "MN021",
    "result_conflict": "MN022",
}

#: byte SHA-256 of every accepted prior migration — proves 0001-0007 are byte-identical.
ACCEPTED_PRIOR_MIGRATION_SHAS: dict[str, str] = {
    "migrations/versions/0006_l2f_experiment_plan.py": (
        "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
    ),
    "migrations/versions/0007_l2f_job_claiming.py": (
        "bc247e0a68f82ad6e52868e115db3f1e237b637def98567c596e3cc0a4e42625"
    ),
}

_REPO_ROOT = Path(__file__).resolve().parents[3]


def compute_execution_migration_sha256() -> str:
    """Recompute the byte SHA-256 of the authoritative 0008 migration file."""
    return hashlib.sha256((_REPO_ROOT / L2F_EXECUTION_MIGRATION_FILE).read_bytes()).hexdigest()


def compute_execution_contract_hash() -> str:
    """Domain-separated canonical hash over the F5 migration identity + frozen inventory."""
    content = {
        "revision": L2F_EXECUTION_REVISION,
        "down_revision": L2F_EXECUTION_DOWN_REVISION,
        "migration_sha256": compute_execution_migration_sha256(),
        "owned_tables": list(L2F_EXECUTION_OWNED_TABLE_NAMES),
        "failure_codes": list(L2F_EXECUTION_FAILURE_CODES),
        "vcf_media_type": L2F_VCF_MEDIA_TYPE,
        "result_manifest_media_type": L2F_RESULT_MANIFEST_MEDIA_TYPE,
        "functions": list(L2F_EXECUTION_FUNCTIONS),
        "sqlstates": L2F_EXECUTION_SQLSTATES,
        "prior_migration_shas": ACCEPTED_PRIOR_MIGRATION_SHAS,
    }
    return sha256_hex(
        b"minos:l2f-execution-migration-contract:v1\n" + canonical_json_bytes(content)
    )
