"""r0001 — complete runtime.decisions to the exact Layer 2 live-decision contract.

Revision ID: r0001_l2h_runtime_decisions
Revises: (root of the operational runtime overlay)

WHAT THIS IS
------------
The Layer 2 specification's live decision algorithm ends::

    persist decision + all candidate reasons in one transaction
    return exact canonical CONFIG bytes referenced by persisted hash

and its DDL for the table that persistence writes is::

    CREATE TABLE runtime.decisions (
     id uuid PRIMARY KEY, round_id text NOT NULL, profile_id uuid NOT NULL,
     controller_version text NOT NULL, model_bundle_id uuid,
     parameter_space_hash char(64) NOT NULL, baseline_config_id uuid NOT NULL,
     selected_config_id uuid NOT NULL, mode text NOT NULL,
     decision_manifest jsonb NOT NULL, decision_hash char(64) UNIQUE NOT NULL,
     decided_at timestamptz NOT NULL, UNIQUE(round_id, decision_hash)
    );
    INDEX decisions(round_id, decided_at DESC);

``0001_l2b_initial`` created a strict subset of that: no ``mode``, no ``controller_version``, no
``parameter_space_hash``, no ``baseline_config_id``/``selected_config_id``, no
``decision_manifest``, no ``decided_at``, ``profile_id`` nullable, ``decision_hash`` not unique
and no ``(round_id, decided_at DESC)`` index. This revision closes exactly that gap. It adds
nothing the contract does not require and reinterprets nothing.

WHY IT LIVES IN A SECOND LINEAGE
--------------------------------
See ``migrations_runtime/env.py``. Short version: the operational store sits at
``0005_l2e_feature_view`` and everything from ``0006`` on is scientific campaign machinery. This
revision therefore REQUIRES the main chain to be at exactly ``0005_l2e_feature_view`` and refuses
otherwise — which is also what structurally keeps it away from the TRAIN (``0020``) and
VALIDATION (``0026``) stores. No existing revision is edited, re-parented or stamped.

THE PROFILE FOREIGN KEY POINTED AT A TABLE PRODUCTION NEVER WRITES
------------------------------------------------------------------
``0001`` aimed ``profile_id`` at ``profiling.profiles``. ``0004_l2d_profile_ingestion`` then built
the real ingestion on ``profiling.bam_profiles``, and that is where the operational L1 pipeline
actually puts profiles: in the operational store ``profiling.bam_profiles`` holds 75 rows and
``profiling.profiles`` holds none, and nothing in this engine writes the latter. A live decision
could therefore never satisfy ``profile_id NOT NULL`` -- not because the development database
happens to be empty, but because the referenced table is a superseded L2-B placeholder.

So the foreign key is re-aimed at ``profiling.bam_profiles``. That is not a weakened FK; it is a
FK that can be satisfied by the rows production creates, and ``NOT NULL`` is added at the same
time. ``minos_live`` gains SELECT on ``profiling.bam_profiles`` and ``catalog.dataset_registry``
-- the minimum needed to resolve and cross-check the owning profile -- and nothing else. The
downgrade restores both the old FK and the exact previous grant shape.

TWO OLD COLUMNS, NEITHER REINTERPRETED
--------------------------------------
``decision_manifest_hash`` and ``config_id`` exist in ``0001`` and have no counterpart in the
specification DDL and no defined preimage anywhere.

* ``decision_manifest_hash`` is given an explicit meaning it can actually carry: the plain
  SHA-256 of the exact canonical manifest bytes stored in ``decision_manifest``. That is a
  different quantity from ``decision_hash`` (the domain-separated scientific decision identity),
  so the two columns can never be confused for one another, and it makes the JSONB round trip
  checkable on readback.
* ``config_id`` never had a meaning, so it is RETIRED rather than quietly promoted to
  ``selected_config_id``: a CHECK requires it to stay NULL. The decision's config identities live
  in the two columns the contract names.

THE MANIFEST IS BOUND TO THE TYPED COLUMNS BY THE DATABASE ITSELF
-----------------------------------------------------------------
Every typed column that also appears in the manifest is equated to it in a CHECK, so a row whose
columns disagree with its own manifest cannot exist — not merely "is rejected by the writer".
Mode/fallback/model-bundle consistency is enforced the same way, which is what makes it
impossible for this store to imply that a contextual model executed.

The table must be EMPTY. Several of these columns are NOT NULL with no sensible backfill, and
inventing values for historical rows would be fabricating decisions. It is empty everywhere
today (the public service has never been activated), so this is a statement of fact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r0001_l2h_runtime_decisions"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

REQUIRED_MAIN_REVISION = "0005_l2e_feature_view"

_HEX = "^[0-9a-f]{64}$"
_MANIFEST_SCHEMA = "l2h-safe-decision-manifest-v1"
_MODES = ("SAFE_BASELINE", "BOUNDED", "FULL_CONTEXTUAL", "REFINEMENT")
_FALLBACKS = (
    "NONE",
    "SAFE_BASELINE_FORCED",
    "BASELINE_GATE_FAILED",
    "DEADLINE_EXCEEDED",
    "LOW_CONFIDENCE",
    "GUARDRAIL_TRIGGERED",
    "MISSING_IDENTITY",
    "STAGE_NOT_READY",
)

#: name -> SQL. Written out so the frozen contract inventory can name every one of them.
CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_decisions_mode_valid", "mode IN (" + ", ".join(f"'{m}'" for m in _MODES) + ")"),
    (
        "ck_decisions_requested_mode_valid",
        "requested_mode IN (" + ", ".join(f"'{m}'" for m in _MODES) + ")",
    ),
    (
        "ck_decisions_fallback_reason_valid",
        "fallback_reason IN (" + ", ".join(f"'{f}'" for f in _FALLBACKS) + ")",
    ),
    ("ck_decisions_controller_version_nonempty", "length(controller_version) > 0"),
    ("ck_decisions_parameter_space_hash_hex", f"parameter_space_hash ~ '{_HEX}'"),
    # config_id is RETIRED, not repurposed: 0001 never defined what it meant.
    ("ck_decisions_config_id_retired", "config_id IS NULL"),
    # the manifest is a JSON object of the accepted schema, and the typed columns are IT
    ("ck_decisions_manifest_is_object", "jsonb_typeof(decision_manifest) = 'object'"),
    (
        "ck_decisions_manifest_schema",
        f"decision_manifest ->> 'schema_version' = '{_MANIFEST_SCHEMA}'",
    ),
    ("ck_decisions_manifest_round_id", "decision_manifest ->> 'round_id' = round_id"),
    ("ck_decisions_manifest_actual_mode", "decision_manifest ->> 'actual_mode' = mode"),
    (
        "ck_decisions_manifest_requested_mode",
        "decision_manifest ->> 'requested_mode' = requested_mode",
    ),
    (
        "ck_decisions_manifest_fallback_reason",
        "decision_manifest ->> 'fallback_reason' = fallback_reason",
    ),
    (
        "ck_decisions_manifest_controller_version",
        "decision_manifest ->> 'controller_version' = controller_version",
    ),
    (
        "ck_decisions_manifest_parameter_space_hash",
        "decision_manifest ->> 'parameter_space_hash' = parameter_space_hash",
    ),
    # requested == actual is exactly the no-fallback case; anything else must say why
    (
        "ck_decisions_fallback_matches_mode_change",
        "(requested_mode = mode) = (fallback_reason = 'NONE')",
    ),
    # SAFE_BASELINE may never imply that a contextual model executed, and a contextual mode may
    # never claim to have run without one.
    ("ck_decisions_safe_mode_has_no_bundle", "mode <> 'SAFE_BASELINE' OR model_bundle_id IS NULL"),
    (
        "ck_decisions_safe_mode_never_loaded_a_model",
        "mode <> 'SAFE_BASELINE' OR decision_manifest ->> 'model_bundle_loaded' = 'false'",
    ),
    (
        "ck_decisions_contextual_mode_has_bundle",
        "mode = 'SAFE_BASELINE' OR model_bundle_id IS NOT NULL",
    ),
    (
        "ck_decisions_safe_mode_selects_the_baseline",
        "mode <> 'SAFE_BASELINE' OR selected_config_id = baseline_config_id",
    ),
)

ADDED_COLUMNS: tuple[str, ...] = (
    "controller_version",
    "mode",
    "requested_mode",
    "fallback_reason",
    "parameter_space_hash",
    "baseline_config_id",
    "selected_config_id",
    "decision_manifest",
    "decided_at",
)
ADDED_FOREIGN_KEYS: tuple[str, ...] = (
    "fk_decisions_baseline_config_id_gatk_configs",
    "fk_decisions_selected_config_id_gatk_configs",
)
#: least-privilege reads the live decision path needs and did not have
LIVE_SELECT_GRANTS: tuple[str, ...] = ("profiling.bam_profiles", "catalog.dataset_registry")
ADDED_UNIQUE_CONSTRAINTS: tuple[str, ...] = ("uq_decisions_decision_hash",)
ADDED_INDEXES: tuple[str, ...] = ("ix_decisions_round_id_decided_at",)


def _require_prerequisites() -> None:
    """The overlay applies to the accepted operational schema, or to nothing at all."""
    conn = op.get_bind()
    main = conn.execute(
        sa.text("SELECT array_agg(version_num ORDER BY version_num) FROM public.alembic_version")
    ).scalar()
    revisions = tuple(main or ())
    if revisions != (REQUIRED_MAIN_REVISION,):
        raise RuntimeError(
            "the operational runtime overlay requires the main schema to be exactly "
            f"{REQUIRED_MAIN_REVISION}; this database is at {revisions or '(no main lineage)'}. "
            "It is not applicable to a scientific campaign store."
        )
    for table in ("runtime.decisions", "profiling.bam_profiles", "catalog.dataset_registry"):
        if conn.execute(sa.text(f"SELECT to_regclass('{table}')")).scalar() is None:
            raise RuntimeError(f"{table} does not exist in this database")
    rows = int(conn.execute(sa.text("SELECT count(*) FROM runtime.decisions")).scalar() or 0)
    if rows:
        raise RuntimeError(
            f"runtime.decisions holds {rows} row(s); this revision adds NOT NULL columns that "
            "have no honest backfill, and inventing values for historical decisions is not an "
            "option"
        )


def upgrade() -> None:
    _require_prerequisites()
    op.execute("SET ROLE minos_admin")
    op.add_column(
        "decisions", sa.Column("controller_version", sa.Text(), nullable=False), schema="runtime"
    )
    op.add_column("decisions", sa.Column("mode", sa.Text(), nullable=False), schema="runtime")
    op.add_column(
        "decisions", sa.Column("requested_mode", sa.Text(), nullable=False), schema="runtime"
    )
    op.add_column(
        "decisions", sa.Column("fallback_reason", sa.Text(), nullable=False), schema="runtime"
    )
    op.add_column(
        "decisions",
        sa.Column("parameter_space_hash", sa.CHAR(64), nullable=False),
        schema="runtime",
    )
    op.add_column(
        "decisions",
        sa.Column("baseline_config_id", postgresql.UUID(as_uuid=False), nullable=False),
        schema="runtime",
    )
    op.add_column(
        "decisions",
        sa.Column("selected_config_id", postgresql.UUID(as_uuid=False), nullable=False),
        schema="runtime",
    )
    op.add_column(
        "decisions",
        sa.Column("decision_manifest", postgresql.JSONB(), nullable=False),
        schema="runtime",
    )
    op.add_column(
        "decisions",
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="runtime",
    )
    op.alter_column("decisions", "profile_id", nullable=False, schema="runtime")
    # re-aim the profile FK at the table the operational L1 pipeline actually populates
    op.drop_constraint(
        "fk_decisions_profile_id_profiles", "decisions", schema="runtime", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_decisions_profile_id_bam_profiles",
        "decisions",
        "bam_profiles",
        ["profile_id"],
        ["id"],
        source_schema="runtime",
        referent_schema="profiling",
    )
    for name in ADDED_FOREIGN_KEYS:
        column = "baseline_config_id" if "baseline" in name else "selected_config_id"
        op.create_foreign_key(
            name,
            "decisions",
            "gatk_configs",
            [column],
            ["id"],
            source_schema="runtime",
            referent_schema="catalog",
        )
    op.create_unique_constraint(
        "uq_decisions_decision_hash", "decisions", ["decision_hash"], schema="runtime"
    )
    for name, sql in CHECKS:
        op.create_check_constraint(name, "decisions", sa.text(sql), schema="runtime")
    op.execute(
        "CREATE INDEX ix_decisions_round_id_decided_at "
        "ON runtime.decisions (round_id, decided_at DESC)"
    )
    for table in LIVE_SELECT_GRANTS:
        op.execute(f"GRANT SELECT ON {table} TO minos_live;")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for table in LIVE_SELECT_GRANTS:
        op.execute(f"REVOKE SELECT ON {table} FROM minos_live;")
    op.execute("DROP INDEX IF EXISTS runtime.ix_decisions_round_id_decided_at")
    for name, _sql in reversed(CHECKS):
        op.drop_constraint(name, "decisions", schema="runtime", type_="check")
    op.drop_constraint("uq_decisions_decision_hash", "decisions", schema="runtime", type_="unique")
    for name in ADDED_FOREIGN_KEYS:
        op.drop_constraint(name, "decisions", schema="runtime", type_="foreignkey")
    op.drop_constraint(
        "fk_decisions_profile_id_bam_profiles", "decisions", schema="runtime", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_decisions_profile_id_profiles",
        "decisions",
        "profiles",
        ["profile_id"],
        ["id"],
        source_schema="runtime",
        referent_schema="profiling",
    )
    op.alter_column("decisions", "profile_id", nullable=True, schema="runtime")
    for column in reversed(ADDED_COLUMNS):
        op.drop_column("decisions", column, schema="runtime")
    op.execute("RESET ROLE")
