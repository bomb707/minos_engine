"""Frozen contract for the operational RUNTIME OVERLAY lineage.

Mirrors ``storage/migration_contract.py`` for the second, independently versioned chain in
``migrations_runtime/``: an immutable inventory of exactly what
``r0001_l2h_runtime_decisions`` must produce, and a ``contract_hash`` binding the
committed migration bytes to it.

The Core ``Table`` here mirrors ``runtime.decisions`` AFTER the overlay and lives on a DEDICATED
private ``MetaData`` — exactly like ``storage/l2f_tables.py``, and for the same reason: the
accepted DB-READY gate hashes ``Base.metadata``, so binding these columns there would silently
move an accepted gate. ``create_all``/``drop_all`` are never called on it; the migration is the
authoritative DDL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "ADDED_CHECK_CONSTRAINTS",
    "LIVE_PROFILE_RESOLVER",
    "LIVE_PROFILE_RESOLVER_COLUMNS",
    "LIVE_PROFILE_RESOLVER_SEARCH_PATH",
    "LIVE_PROFILE_RESOLVER_SIGNATURE",
    "LIVE_VERSION_TABLE_GRANTS",
    "R0002_FROZEN_INVENTORY",
    "R0002_MIGRATION_PATH",
    "R0002_REVISION",
    "REVOKED_IDENTITY_TABLE_GRANTS",
    "RUNTIME_OVERLAY_HEAD_REVISION",
    "runtime_overlay_head_contract_hash",
    "FROZEN_INVENTORY",
    "REQUIRED_MAIN_REVISION",
    "RUNTIME_OVERLAY_MIGRATION_PATH",
    "RUNTIME_OVERLAY_REVISION",
    "RUNTIME_OVERLAY_SCHEMA",
    "RUNTIME_OVERLAY_SCRIPT_LOCATION",
    "RUNTIME_OVERLAY_VERSION_TABLE",
    "RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA",
    "decisions_table",
    "runtime_overlay_contract_hash",
    "runtime_overlay_revision",
]

RUNTIME_OVERLAY_SCHEMA: Final = "l2h-operational-runtime-overlay-v1"
RUNTIME_OVERLAY_REVISION: Final = "r0001_l2h_runtime_decisions"
RUNTIME_OVERLAY_SCRIPT_LOCATION: Final = "migrations_runtime"
RUNTIME_OVERLAY_MIGRATION_PATH: Final = "migrations_runtime/versions/r0001_l2h_runtime_decisions.py"

#: The corrective revision. r0001 stays exactly as it was issued, qualified and applied; the
#: privilege defect is closed additively on top of it.
R0002_REVISION: Final = "r0002_l2h_live_profile_lookup"
R0002_MIGRATION_PATH: Final = "migrations_runtime/versions/r0002_l2h_live_profile_lookup.py"

#: The head of the overlay lineage: what an operational store must be at.
RUNTIME_OVERLAY_HEAD_REVISION: Final = R0002_REVISION

#: The narrow live lookup surface that replaces the raw identity-table grants.
LIVE_PROFILE_RESOLVER: Final = "runtime.l2h_resolve_owned_profile"
LIVE_PROFILE_RESOLVER_SIGNATURE: Final = f"{LIVE_PROFILE_RESOLVER}(text, text)"
LIVE_PROFILE_RESOLVER_SEARCH_PATH: Final = "pg_catalog, pg_temp"
LIVE_PROFILE_RESOLVER_COLUMNS: Final[tuple[str, ...]] = (
    "profile_row_id",
    "attestation_hash",
    "bai_sha256",
    "bam_sha256",
    "fai_sha256",
    "identity_tuple_hash",
    "profile_manifest_sha256",
    "profile_sha256",
    "reference_sha256",
    "region_hash",
    "registry_snapshot_hash",
    "integrity_degraded",
    "dataset_id",
    "round_id",
    "chromosome",
)

#: Raw identity tables the live role must NOT be able to read. Between them they carry the
#: profile and dataset identities of every partition, TEST included.
REVOKED_IDENTITY_TABLE_GRANTS: Final[tuple[str, ...]] = (
    "catalog.dataset_registry",
    "profiling.bam_profiles",
)

#: One revision string each: no identity, no partition, nothing sealed.
LIVE_VERSION_TABLE_GRANTS: Final[tuple[str, ...]] = (
    "public.alembic_version",
    "runtime.alembic_version_runtime",
)

R0002_FROZEN_INVENTORY: dict[str, object] = {
    "schema_version": "l2h-operational-runtime-overlay-v2",
    "revision": R0002_REVISION,
    "down_revision": "r0001_l2h_runtime_decisions",
    "requires_main_revision": "0005_l2e_feature_view",
    "revoked_grants": [
        f"SELECT ON {table} FROM minos_live" for table in REVOKED_IDENTITY_TABLE_GRANTS
    ],
    "added_grants": [f"SELECT ON {table} TO minos_live" for table in LIVE_VERSION_TABLE_GRANTS]
    + [f"EXECUTE ON FUNCTION {LIVE_PROFILE_RESOLVER_SIGNATURE} TO minos_live"],
    "added_functions": [LIVE_PROFILE_RESOLVER_SIGNATURE],
    "function_owner": "minos_admin",
    "function_security": "DEFINER",
    "function_search_path": LIVE_PROFILE_RESOLVER_SEARCH_PATH,
    "function_language": "plpgsql",
    "function_uses_dynamic_sql": False,
    "function_returns_at_most_one_row": True,
    "function_public_execute": False,
    "added_columns": [],
    "added_tables": [],
    "added_check_constraints": [],
    "added_indexes": [],
    "counts": {"revoked_grants": 2, "added_grants": 3, "added_functions": 1},
}

#: The overlay's own version tracking, deliberately NOT ``public.alembic_version``.
RUNTIME_OVERLAY_VERSION_TABLE: Final = "alembic_version_runtime"
RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA: Final = "runtime"

#: The overlay applies to the accepted operational schema and to nothing else. This single
#: requirement is what keeps it off the TRAIN (0020) and VALIDATION (0026) campaign stores.
REQUIRED_MAIN_REVISION: Final = "0005_l2e_feature_view"

ADDED_CHECK_CONSTRAINTS: Final[tuple[str, ...]] = (
    "ck_decisions_config_id_retired",
    "ck_decisions_contextual_mode_has_bundle",
    "ck_decisions_controller_version_nonempty",
    "ck_decisions_fallback_matches_mode_change",
    "ck_decisions_fallback_reason_valid",
    "ck_decisions_manifest_actual_mode",
    "ck_decisions_manifest_controller_version",
    "ck_decisions_manifest_fallback_reason",
    "ck_decisions_manifest_is_object",
    "ck_decisions_manifest_parameter_space_hash",
    "ck_decisions_manifest_requested_mode",
    "ck_decisions_manifest_round_id",
    "ck_decisions_manifest_schema",
    "ck_decisions_mode_valid",
    "ck_decisions_parameter_space_hash_hex",
    "ck_decisions_requested_mode_valid",
    "ck_decisions_safe_mode_has_no_bundle",
    "ck_decisions_safe_mode_never_loaded_a_model",
    "ck_decisions_safe_mode_selects_the_baseline",
)

FROZEN_INVENTORY: dict[str, object] = {
    "schema_version": RUNTIME_OVERLAY_SCHEMA,
    "revision": RUNTIME_OVERLAY_REVISION,
    "down_revision": None,
    "requires_main_revision": REQUIRED_MAIN_REVISION,
    "version_table": f"{RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA}.{RUNTIME_OVERLAY_VERSION_TABLE}",
    "table": "runtime.decisions",
    "schema_owner": "minos_admin",
    "requires_empty_table": True,
    "added_columns": [
        "baseline_config_id",
        "controller_version",
        "decided_at",
        "decision_manifest",
        "fallback_reason",
        "mode",
        "parameter_space_hash",
        "requested_mode",
        "selected_config_id",
    ],
    "altered_columns": ["profile_id NULL -> NOT NULL"],
    "retired_columns": ["config_id"],
    "added_foreign_keys": [
        "fk_decisions_baseline_config_id_gatk_configs",
        "fk_decisions_selected_config_id_gatk_configs",
    ],
    # 0001 aimed profile_id at profiling.profiles; 0004 built the real ingestion on
    # profiling.bam_profiles and nothing has ever written the former. Re-aimed, not weakened.
    "retargeted_foreign_keys": [
        {
            "dropped": "fk_decisions_profile_id_profiles",
            "created": "fk_decisions_profile_id_bam_profiles",
            "from": "profiling.profiles.id",
            "to": "profiling.bam_profiles.id",
        }
    ],
    "added_unique_constraints": ["uq_decisions_decision_hash"],
    "added_check_constraints": list(ADDED_CHECK_CONSTRAINTS),
    "added_indexes": ["ix_decisions_round_id_decided_at"],
    "added_grants": [
        "SELECT ON catalog.dataset_registry TO minos_live",
        "SELECT ON profiling.bam_profiles TO minos_live",
    ],
    "added_tables": [],
    "added_functions": [],
    "added_triggers": [],
    "counts": {
        "added_columns": 9,
        "added_foreign_keys": 2,
        "added_unique_constraints": 1,
        "added_check_constraints": 19,
        "added_indexes": 1,
        "retargeted_foreign_keys": 1,
        "added_grants": 2,
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def runtime_overlay_contract_hash(root: Any = None) -> str:
    """Bind the committed migration bytes to the frozen inventory."""
    base = Path(root) if root is not None else _repo_root()
    digest = hashlib.sha256((base / RUNTIME_OVERLAY_MIGRATION_PATH).read_bytes()).hexdigest()
    return canonical_hash({"migration_file_sha256": digest, "frozen_inventory": FROZEN_INVENTORY})


def runtime_overlay_head_contract_hash(root: Any = None) -> str:
    """Bind BOTH committed overlay revisions to their frozen inventories.

    r0001's own hash is unchanged and still verifiable on its own; this is the identity of the
    lineage as a whole, which is what an operational store is actually at.
    """
    base = Path(root) if root is not None else _repo_root()
    return canonical_hash(
        {
            "r0001": {
                "migration_file_sha256": hashlib.sha256(
                    (base / RUNTIME_OVERLAY_MIGRATION_PATH).read_bytes()
                ).hexdigest(),
                "frozen_inventory": FROZEN_INVENTORY,
            },
            "r0002": {
                "migration_file_sha256": hashlib.sha256(
                    (base / R0002_MIGRATION_PATH).read_bytes()
                ).hexdigest(),
                "frozen_inventory": R0002_FROZEN_INVENTORY,
            },
            "head": RUNTIME_OVERLAY_HEAD_REVISION,
        }
    )


def runtime_overlay_revision(conn: Any) -> str | None:
    """The overlay revision this database is at, or ``None`` when the overlay is not applied."""
    present = conn.execute(
        sa.text(
            "SELECT to_regclass(:t)",
        ),
        {"t": f"{RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA}.{RUNTIME_OVERLAY_VERSION_TABLE}"},
    ).scalar()
    if present is None:
        return None
    value = conn.execute(
        sa.text(
            f"SELECT version_num FROM {RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA}."
            f"{RUNTIME_OVERLAY_VERSION_TABLE}"
        )
    ).scalar()
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# private Core mapping of the POST-OVERLAY table (never Base.metadata)
# --------------------------------------------------------------------------- #
runtime_overlay_metadata: Final = sa.MetaData()

decisions_table: Final = sa.Table(
    "decisions",
    runtime_overlay_metadata,
    sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column("round_id", sa.Text(), nullable=False),
    sa.Column("decision_hash", sa.CHAR(64), nullable=False),
    sa.Column("decision_manifest_hash", sa.CHAR(64), nullable=False),
    sa.Column("config_id", postgresql.UUID(as_uuid=False), nullable=True),
    sa.Column("model_bundle_id", postgresql.UUID(as_uuid=False), nullable=True),
    sa.Column("profile_id", postgresql.UUID(as_uuid=False), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.Column("controller_version", sa.Text(), nullable=False),
    sa.Column("mode", sa.Text(), nullable=False),
    sa.Column("requested_mode", sa.Text(), nullable=False),
    sa.Column("fallback_reason", sa.Text(), nullable=False),
    sa.Column("parameter_space_hash", sa.CHAR(64), nullable=False),
    sa.Column("baseline_config_id", postgresql.UUID(as_uuid=False), nullable=False),
    sa.Column("selected_config_id", postgresql.UUID(as_uuid=False), nullable=False),
    sa.Column("decision_manifest", postgresql.JSONB(), nullable=False),
    sa.Column(
        "decided_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    schema="runtime",
)
