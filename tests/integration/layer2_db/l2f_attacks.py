"""The 47-case L2-F direct-SQL constraint attack manifest (F3-A4).

Each case is a single, independently meaningful attempt to violate exactly one PostgreSQL
invariant of the ``0006`` schema against the seeded valid graph. A case declares its target
operation, the mutation, the expected SQLSTATE, and the expected named constraint or trigger
mechanism. Cases are executed one at a time inside a SAVEPOINT and rolled back so they cannot
contaminate one another (see ``test_l2f_attack_matrix``).

Grouping and count are frozen: 12 plan + 10 plan-member + 10 config/payload + 15
job/immutability = 47 unique cases. (The member group is 10: the three separately-named
member-duplicate cases were collapsed into one canonical ``member_duplicate_logical_membership``
equivalence-class case, since those three UNIQUE constraints are not independently isolatable
against the deployed composite-FK graph.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tests.integration.layer2_db.l2f_seed import (
    CK_MEDIA_WRONG,
    CONFIG_MEDIA_TYPE,
    H,
    SeededGraph,
)

# SQLSTATE codes
FK = "23503"  # foreign_key_violation
UNIQUE = "23505"  # unique_violation
CHECK = "23514"  # check_violation
APPEND_ONLY = "23001"  # restrict_violation (audit.minos_reject_mutation)
RAISE = "P0001"  # raise_exception (job identity-change trigger)

Mechanism = Literal["constraint", "trigger"]


@dataclass(frozen=True)
class Isolation:
    """Optional pre-attack proof that a case can only reach its declared mechanism.

    ``present`` tuples must each already exist (the non-target composite-FK/unique targets
    the attack row satisfies); ``absent`` tuples must each not exist (the single target tuple
    the attack row is missing). Each entry is ``(count_sql, params)``. ``check_false`` is a
    Python-evaluated boolean that must be False (the fixed-value CHECK the attack violates).
    """

    present: tuple[tuple[str, dict[str, Any]], ...] = ()
    absent: tuple[tuple[str, dict[str, Any]], ...] = ()
    check_false: bool | None = None


@dataclass(frozen=True)
class Attack:
    name: str
    group: str
    op: str  # 'insert' | 'update' | 'delete'
    target: str  # table (unqualified)
    sqlstate: str
    mechanism: Mechanism  # 'constraint' -> assert constraint_name; 'trigger' -> assert message
    expect: str  # constraint name OR message substring
    # exactly one of the following is populated
    row: dict[str, Any] | None = None  # for op == 'insert'
    sql: str | None = None  # for op in ('update','delete')
    params: dict[str, Any] | None = None
    isolation: Isolation | None = None
    #: for a behavioral equivalence class (constraint mechanism only): the observed
    #: constraint_name must be one of these. Used by the member-duplicate case, whose three
    #: member UNIQUE constraints are mutually inseparable under the complete composite-FK graph.
    expect_any: tuple[str, ...] | None = None


def _ins(
    name: str,
    group: str,
    table: str,
    sqlstate: str,
    mech: Mechanism,
    expect: str,
    row: dict[str, Any],
    op: str = "insert",
    iso: Isolation | None = None,
    expect_any: tuple[str, ...] | None = None,
) -> Attack:
    return Attack(
        name,
        group,
        op,
        table,
        sqlstate,
        mech,
        expect,
        row=row,
        isolation=iso,
        expect_any=expect_any,
    )


# ---- SQL fragments for isolation existence checks ----
_PM_MATRIX_TUPLE = (
    "SELECT count(*) FROM profiling.feature_matrix_members "
    "WHERE id = :fmm AND feature_matrix_id = :mat AND dataset_registry_id = :dsr "
    "AND member_index = :idx AND feature_values_hash = :fvh"
)
_PM_SNAPSHOT_TUPLE = (
    "SELECT count(*) FROM profiling.profile_snapshot_members "
    "WHERE id = :psm AND profile_snapshot_id = :snap AND dataset_registry_id = :dsr "
    "AND bam_profile_id = :bam AND partition = 'train' AND feature_values_hash = :fvh"
)
_PM_PLAN_TUPLE = (
    "SELECT count(*) FROM experiments.l2f_experiment_plans "
    "WHERE id = :plan AND profile_snapshot_id = :snap AND train_feature_matrix_id = :mat"
)
_PM_UQ_SNAPSHOT = (
    "SELECT count(*) FROM experiments.l2f_experiment_plan_members "
    "WHERE plan_id = :plan AND profile_snapshot_member_id = :psm"
)
_PM_UQ_MATRIX = (
    "SELECT count(*) FROM experiments.l2f_experiment_plan_members "
    "WHERE plan_id = :plan AND feature_matrix_member_id = :fmm"
)
_PM_UQ_INDEX = (
    "SELECT count(*) FROM experiments.l2f_experiment_plan_members "
    "WHERE plan_id = :plan AND member_index = :idx"
)
_ARTIFACT_TUPLE = (
    "SELECT count(*) FROM catalog.artifacts "
    "WHERE id = :art AND sha256 = :sha AND media_type = :media"
)


def _pm_iso(row: dict[str, Any]) -> dict[str, Any]:
    """Bind the plan-member composite-tuple existence-check params from an attack row."""
    return {
        "plan": row["plan_id"],
        "snap": row["profile_snapshot_id"],
        "mat": row["feature_matrix_id"],
        "psm": row["profile_snapshot_member_id"],
        "fmm": row["feature_matrix_member_id"],
        "bam": row["bam_profile_id"],
        "dsr": row["dataset_registry_id"],
        "idx": row["member_index"],
        "fvh": row["feature_values_hash"],
    }


def build_attacks(g: SeededGraph) -> list[Attack]:
    P = "l2f_experiment_plans"
    PM = "l2f_experiment_plan_members"
    CP = "l2f_config_payloads"
    PC = "l2f_experiment_plan_configs"
    J = "l2f_experiment_jobs"
    attacks: list[Attack] = []
    A = attacks.append

    # ============================ PLAN (12) ============================
    A(
        _ins(
            "plan_forged_snapshot_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_snapshot_identity",
            g.new_plan(snapshot_hash=H("forged_snap")),
        )
    )
    A(
        _ins(
            "plan_forged_split_manifest_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_snapshot_identity",
            g.new_plan(split_manifest_hash=H("forged_split")),
        )
    )
    A(
        _ins(
            "plan_forged_registry_snapshot_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_snapshot_identity",
            g.new_plan(registry_snapshot_hash=H("forged_reg")),
        )
    )
    A(
        _ins(
            "plan_wrong_feature_set_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_feature_set_identity",
            g.new_plan(feature_set_hash=H("forged_fsh")),
        )
    )
    A(
        _ins(
            "plan_wrong_feature_registry_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_feature_set_identity",
            g.new_plan(feature_registry_hash=H("forged_fr")),
        )
    )
    # matrix from another snapshot: MB belongs to SB, plan snapshot is SA
    A(
        _ins(
            "plan_matrix_from_another_snapshot",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_train_matrix_lineage",
            g.new_plan(train_feature_matrix_id=g.mb, train_matrix_hash=g.mb_hash),
        )
    )
    # non-train matrix/partition: MV is a validation matrix (partition kept 'train' to reach the FK)
    A(
        _ins(
            "plan_non_train_matrix",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_train_matrix_lineage",
            g.new_plan(train_feature_matrix_id=g.mv, train_matrix_hash=g.mv_hash),
        )
    )
    A(
        _ins(
            "plan_wrong_train_matrix_hash",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_train_matrix_lineage",
            g.new_plan(train_matrix_hash=H("forged_mh")),
        )
    )
    # wrong feature_set_id: FS_B (with FS_B's own hashes so the feature-set FK passes) but MA is over FS_A
    A(
        _ins(
            "plan_wrong_feature_set_id",
            "plan",
            P,
            FK,
            "constraint",
            "fk_l2f_plans_train_matrix_lineage",
            g.new_plan(
                feature_set_id=g.fsb, feature_set_hash=g.fsb_hash, feature_registry_hash=g.fsb_reg
            ),
        )
    )
    A(
        _ins(
            "plan_invalid_derived_counts",
            "plan",
            P,
            CHECK,
            "constraint",
            "ck_l2f_plans_job_count_consistent",
            g.new_plan(logical_job_count=5),
        )
    )
    A(
        _ins(
            "plan_duplicate_plan_hash",
            "plan",
            P,
            UNIQUE,
            "constraint",
            "uq_l2f_plans_plan_hash",
            g.new_plan(plan_hash=g.pa_hash),
        )
    )
    # duplicate complete logical identity: replicate Plan A's 11 identity hashes, fresh plan_hash
    A(
        _ins(
            "plan_duplicate_logical_identity",
            "plan",
            P,
            UNIQUE,
            "constraint",
            "uq_l2f_plans_logical_identity",
            g.new_plan(
                train_feature_view_hash=H("tfv_A"),
                parameter_space_hash=g.ps_a,
                experiment_parameter_policy_hash=g.epp,
                candidate_set_hash=H("csh_A"),
                plan_hash=H("dup_logical_fresh_hash"),
            ),
        )
    )

    # ========================= PLAN MEMBER (12) =========================
    # validation snapshot member (psm is validation; matrix side satisfiable via D2 in MA)
    A(
        _ins(
            "member_validation_snapshot_member",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(
                profile_snapshot_member_id=g.psm[("SA", "D2")],
                feature_matrix_member_id=g.fmm[("MA", "D2")],
                bam_profile_id=g.bam["D2"],
                dataset_registry_id=g.dsr["D2"],
                feature_values_hash=g.fvh["D2"],
                member_index=g.fmm_index[("MA", "D2")],
            ),
        )
    )
    A(
        _ins(
            "member_test_snapshot_member",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(
                profile_snapshot_member_id=g.psm[("SA", "D3")],
                feature_matrix_member_id=g.fmm[("MA", "D3")],
                bam_profile_id=g.bam["D3"],
                dataset_registry_id=g.dsr["D3"],
                feature_values_hash=g.fvh["D3"],
                member_index=g.fmm_index[("MA", "D3")],
            ),
        )
    )
    # snapshot member from another snapshot: D8 psm belongs to SB, matrix member is in MA
    A(
        _ins(
            "member_snapshot_from_another_snapshot",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(
                profile_snapshot_member_id=g.psm[("SB", "D8")],
                feature_matrix_member_id=g.fmm[("MA", "D8")],
                bam_profile_id=g.bam["D8"],
                dataset_registry_id=g.dsr["D8"],
                feature_values_hash=g.fvh["D8"],
                member_index=g.fmm_index[("MA", "D8")],
            ),
        )
    )
    # matrix member from another matrix: D9 matrix member is in MB, snapshot member is in SA
    A(
        _ins(
            "member_matrix_from_another_matrix",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_matrix_member",
            g.new_member(
                profile_snapshot_member_id=g.psm[("SA", "D9")],
                feature_matrix_member_id=g.fmm[("MB", "D9")],
                bam_profile_id=g.bam["D9"],
                dataset_registry_id=g.dsr["D9"],
                feature_values_hash=g.fvh["D9"],
                member_index=g.fmm_index[("MB", "D9")],
            ),
        )
    )
    # mismatched dataset_registry_id: the row's dataset (D_MDR) is a real MA matrix member
    # (matrix-member FK passes) but the named snapshot member (PSM(SA,D7)) belongs to dataset
    # D7 -> the snapshot-member composite tuple is absent. Only the snapshot-member FK fails.
    _mm_dsr = g.new_member(
        feature_matrix_member_id=g.up.fmm_mdr,
        dataset_registry_id=g.up.d_mdr,
        feature_values_hash=g.up.fvh_mdr,
        member_index=g.up.mdr_index,
    )
    A(
        _ins(
            "member_mismatched_dataset_registry",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            _mm_dsr,
            iso=Isolation(
                present=((_PM_MATRIX_TUPLE, _pm_iso(_mm_dsr)), (_PM_PLAN_TUPLE, _pm_iso(_mm_dsr))),
                absent=((_PM_SNAPSHOT_TUPLE, _pm_iso(_mm_dsr)),),
            ),
        )
    )
    # substituted bam_profile_id (matrix FK unaffected -> only snapshot FK fails)
    _mm_bam = g.new_member(bam_profile_id=g.bam["D1"])
    A(
        _ins(
            "member_substituted_bam_profile",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            _mm_bam,
            iso=Isolation(
                present=((_PM_MATRIX_TUPLE, _pm_iso(_mm_bam)), (_PM_PLAN_TUPLE, _pm_iso(_mm_bam))),
                absent=((_PM_SNAPSHOT_TUPLE, _pm_iso(_mm_bam)),),
            ),
        )
    )
    # mismatched feature_values_hash: the row's fvh (D_MFVH's MA matrix-member hash) makes the
    # matrix-member FK pass, but D_MFVH's SA snapshot member carries a DIFFERENT fvh, so the
    # snapshot-member composite tuple is absent. Only the snapshot-member FK fails.
    _mm_fvh = g.new_member(
        profile_snapshot_member_id=g.up.psm_mfvh,
        feature_matrix_member_id=g.up.fmm_mfvh,
        bam_profile_id=g.up.bam_mfvh,
        dataset_registry_id=g.up.d_mfvh,
        feature_values_hash=g.up.fvh_mfvh_mat,
        member_index=g.up.mfvh_index,
    )
    A(
        _ins(
            "member_mismatched_feature_values_hash",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            _mm_fvh,
            iso=Isolation(
                present=((_PM_MATRIX_TUPLE, _pm_iso(_mm_fvh)), (_PM_PLAN_TUPLE, _pm_iso(_mm_fvh))),
                absent=((_PM_SNAPSHOT_TUPLE, _pm_iso(_mm_fvh)),),
            ),
        )
    )
    # wrong member_index (matrix FK only uses index -> matrix FK fails)
    A(
        _ins(
            "member_wrong_member_index",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_matrix_member",
            g.new_member(member_index=99),
        )
    )
    # cross-plan lineage: Plan A id with Plan B's snapshot+matrix and Plan B's members
    A(
        _ins(
            "member_cross_plan_lineage",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_plan_lineage",
            g.new_member(
                profile_snapshot_id=g.sb,
                feature_matrix_id=g.mb,
                profile_snapshot_member_id=g.psm[("SB", "D5")],
                feature_matrix_member_id=g.fmm[("MB", "D5")],
                bam_profile_id=g.bam["D5"],
                dataset_registry_id=g.dsr["D5"],
                feature_values_hash=g.fvh["D5"],
                member_index=g.fmm_index[("MB", "D5")],
            ),
        )
    )
    # duplicate logical membership: a COMPLETELY FK-valid duplicate of Plan A's D1 member.
    # Because the snapshot-member and matrix-member composite FKs fix the dataset, the
    # feature-values hash and the frozen matrix index, a fully-valid duplicate necessarily
    # collides with ALL THREE member UNIQUE constraints at once:
    #   uq_l2f_pm_plan_snapshot_member (plan_id, profile_snapshot_member_id)
    #   uq_l2f_pm_plan_matrix_member   (plan_id, feature_matrix_member_id)
    #   uq_l2f_pm_plan_member_index    (plan_id, member_index)
    # These three form one behavioral equivalence class under the complete composite-FK graph;
    # they are NOT independently isolatable against the deployed schema (proven by the earlier
    # three-way attempt). We therefore assert one canonical case: SQLSTATE 23505 with an observed
    # constraint drawn from the equivalence set, and prove every composite-FK target tuple and
    # all three member-unique target tuples already exist (so the duplicate is genuinely valid
    # and the collision is over the logical membership, not a forged/absent tuple).
    _dup_member = g.new_member(
        profile_snapshot_member_id=g.psm[("SA", "D1")],
        feature_matrix_member_id=g.fmm[("MA", "D1")],
        bam_profile_id=g.bam["D1"],
        dataset_registry_id=g.dsr["D1"],
        feature_values_hash=g.fvh["D1"],
        member_index=g.fmm_index[("MA", "D1")],
    )
    A(
        _ins(
            "member_duplicate_logical_membership",
            "member",
            PM,
            UNIQUE,
            "constraint",
            "uq_l2f_pm_plan_snapshot_member",  # representative; observed may be any of the set
            _dup_member,
            iso=Isolation(
                present=(
                    (_PM_PLAN_TUPLE, _pm_iso(_dup_member)),
                    (_PM_SNAPSHOT_TUPLE, _pm_iso(_dup_member)),
                    (_PM_MATRIX_TUPLE, _pm_iso(_dup_member)),
                    (_PM_UQ_SNAPSHOT, _pm_iso(_dup_member)),
                    (_PM_UQ_MATRIX, _pm_iso(_dup_member)),
                    (_PM_UQ_INDEX, _pm_iso(_dup_member)),
                ),
            ),
            expect_any=(
                "uq_l2f_pm_plan_snapshot_member",
                "uq_l2f_pm_plan_matrix_member",
                "uq_l2f_pm_plan_member_index",
            ),
        )
    )

    # ========================= CONFIG / PAYLOAD (10) =========================
    A(
        _ins(
            "config_artifact_sha_ne_config_hash",
            "config",
            CP,
            FK,
            "constraint",
            "fk_l2f_cp_artifact_sha_media",
            g.new_config_payload(config_hash=H("cfg_diff")),
        )
    )
    A(
        _ins(
            "config_wrong_artifact_media_type",
            "config",
            CP,
            FK,
            "constraint",
            "fk_l2f_cp_artifact_sha_media",
            g.new_config_payload(artifact_id=g.ar["WM"], config_hash=g.ch["WM"]),
        )
    )
    A(
        _ins(
            "config_wrong_payload_schema_version",
            "config",
            CP,
            CHECK,
            "constraint",
            "ck_l2f_cp_schema_version",
            g.new_config_payload(schema_version="WRONG"),
        )
    )
    # wrong payload media_type: reference the probe artifact whose exact (id, sha256,
    # media_type) tuple equals this row's, so the composite artifact FK PASSES and only the
    # fixed-media-type CHECK fails — independent of FK evaluation order.
    _cfg_media = g.new_config_payload(
        artifact_id=g.up.ar_ckmedia, config_hash=g.up.ch_ckmedia, media_type=CK_MEDIA_WRONG
    )
    A(
        _ins(
            "config_wrong_payload_media_type",
            "config",
            CP,
            CHECK,
            "constraint",
            "ck_l2f_cp_media_type",
            _cfg_media,
            iso=Isolation(
                present=(
                    (
                        _ARTIFACT_TUPLE,
                        {
                            "art": _cfg_media["artifact_id"],
                            "sha": _cfg_media["config_hash"],
                            "media": _cfg_media["media_type"],
                        },
                    ),
                ),
                check_false=(_cfg_media["media_type"] == CONFIG_MEDIA_TYPE),
            ),
        )
    )
    A(
        _ins(
            "config_duplicate_config_hash",
            "config",
            CP,
            UNIQUE,
            "constraint",
            "uq_l2f_config_payloads_config_hash",
            g.new_config_payload(config_hash=g.ch["CH1"], artifact_id=g.ar["CH1"]),
        )
    )
    # plan-config parameter-space mismatch with plan (payload PS_B, plan PS_A)
    A(
        _ins(
            "planconfig_param_space_mismatch_plan",
            "config",
            PC,
            FK,
            "constraint",
            "fk_l2f_pc_plan_param_space",
            g.new_plan_config(
                config_payload_id=g.cp3,
                config_hash=g.ch["CH3"],
                parameter_space_hash=g.ps_b,
                config_index=5,
            ),
        )
    )
    # plan-config parameter-space mismatch with payload (row PS_A matches plan; payload is PS_B)
    A(
        _ins(
            "planconfig_param_space_mismatch_payload",
            "config",
            PC,
            FK,
            "constraint",
            "fk_l2f_pc_payload_identity",
            g.new_plan_config(config_payload_id=g.cp3, config_hash=g.ch["CH3"], config_index=5),
        )
    )
    # forged config_hash on the plan-config (payload identity FK); CP4 is unlinked so no
    # (plan, payload) unique collision precedes the FK.
    A(
        _ins(
            "planconfig_forged_config_hash",
            "config",
            PC,
            FK,
            "constraint",
            "fk_l2f_pc_payload_identity",
            g.new_plan_config(config_hash=H("cfg_forged")),
        )
    )
    # duplicate plan/config payload
    A(
        _ins(
            "planconfig_duplicate_plan_payload",
            "config",
            PC,
            UNIQUE,
            "constraint",
            "uq_l2f_pc_plan_payload",
            g.new_plan_config(config_payload_id=g.cp1, config_hash=g.ch["CH1"], config_index=5),
        )
    )
    # duplicate config_index (fresh payload CP4, index 0 collides with PC1)
    A(
        _ins(
            "planconfig_duplicate_config_index",
            "config",
            PC,
            UNIQUE,
            "constraint",
            "uq_l2f_pc_plan_index",
            g.new_plan_config(config_index=0),
        )
    )

    # ========================= JOB / IMMUTABILITY (15) =========================
    A(
        _ins(
            "job_member_from_another_plan",
            "job",
            J,
            FK,
            "constraint",
            "fk_l2f_job_member_plan",
            g.new_job(plan_member_id=g.pm_d5),
        )
    )
    A(
        _ins(
            "job_config_from_another_plan",
            "job",
            J,
            FK,
            "constraint",
            "fk_l2f_job_config_plan",
            g.new_job(plan_config_id=g.pcb),
        )
    )
    A(
        _ins(
            "job_duplicate_job_key",
            "job",
            J,
            UNIQUE,
            "constraint",
            "uq_l2f_jobs_job_key",
            g.new_job(job_key=g.jk1),
        )
    )
    A(
        _ins(
            "job_duplicate_logical_identity",
            "job",
            J,
            UNIQUE,
            "constraint",
            "uq_l2f_jobs_logical_identity",
            g.new_job(plan_member_id=g.pm_d1, plan_config_id=g.pc1, job_key=H("fresh_key")),
        )
    )
    A(
        _ins(
            "job_invalid_status",
            "job",
            J,
            CHECK,
            "constraint",
            "ck_l2f_jobs_status_valid",
            g.new_job(status="BOGUS"),
        )
    )
    # append-only UPDATE/DELETE on the four fully-immutable tables
    A(
        Attack(
            "plan_update_rejected",
            "job",
            "update",
            P,
            APPEND_ONLY,
            "trigger",
            "append-only: UPDATE on experiments.l2f_experiment_plans",
            sql="UPDATE experiments.l2f_experiment_plans SET train_feature_view_hash = train_feature_view_hash WHERE id = :id",
            params={"id": g.pa},
        )
    )
    A(
        Attack(
            "plan_delete_rejected",
            "job",
            "delete",
            P,
            APPEND_ONLY,
            "trigger",
            "append-only: DELETE on experiments.l2f_experiment_plans",
            sql="DELETE FROM experiments.l2f_experiment_plans WHERE id = :id",
            params={"id": g.pa},
        )
    )
    A(
        Attack(
            "plan_member_update_rejected",
            "job",
            "update",
            PM,
            APPEND_ONLY,
            "trigger",
            "append-only: UPDATE on experiments.l2f_experiment_plan_members",
            sql="UPDATE experiments.l2f_experiment_plan_members SET member_index = member_index WHERE id = :id",
            params={"id": g.pm_d1},
        )
    )
    A(
        Attack(
            "plan_member_delete_rejected",
            "job",
            "delete",
            PM,
            APPEND_ONLY,
            "trigger",
            "append-only: DELETE on experiments.l2f_experiment_plan_members",
            sql="DELETE FROM experiments.l2f_experiment_plan_members WHERE id = :id",
            params={"id": g.pm_d1},
        )
    )
    A(
        Attack(
            "config_payload_update_rejected",
            "job",
            "update",
            CP,
            APPEND_ONLY,
            "trigger",
            "append-only: UPDATE on experiments.l2f_config_payloads",
            sql="UPDATE experiments.l2f_config_payloads SET schema_version = schema_version WHERE id = :id",
            params={"id": g.cp1},
        )
    )
    A(
        Attack(
            "config_payload_delete_rejected",
            "job",
            "delete",
            CP,
            APPEND_ONLY,
            "trigger",
            "append-only: DELETE on experiments.l2f_config_payloads",
            sql="DELETE FROM experiments.l2f_config_payloads WHERE id = :id",
            params={"id": g.cp1},
        )
    )
    A(
        Attack(
            "plan_config_update_rejected",
            "job",
            "update",
            PC,
            APPEND_ONLY,
            "trigger",
            "append-only: UPDATE on experiments.l2f_experiment_plan_configs",
            sql="UPDATE experiments.l2f_experiment_plan_configs SET config_index = config_index WHERE id = :id",
            params={"id": g.pc1},
        )
    )
    A(
        Attack(
            "plan_config_delete_rejected",
            "job",
            "delete",
            PC,
            APPEND_ONLY,
            "trigger",
            "append-only: DELETE on experiments.l2f_experiment_plan_configs",
            sql="DELETE FROM experiments.l2f_experiment_plan_configs WHERE id = :id",
            params={"id": g.pc1},
        )
    )
    # job scientific-identity mutation (identity-change trigger; distinct P0001 message)
    A(
        Attack(
            "job_identity_mutation_rejected",
            "job",
            "update",
            J,
            RAISE,
            "trigger",
            "immutable identity: L2-F job scientific identity may not change",
            sql="UPDATE experiments.l2f_experiment_jobs SET job_key = :k WHERE id = :id",
            params={"k": H("mutated_key"), "id": g.j1},
        )
    )
    # job deletion (no-delete trigger -> append-only restrict_violation)
    A(
        Attack(
            "job_delete_rejected",
            "job",
            "delete",
            J,
            APPEND_ONLY,
            "trigger",
            "append-only: DELETE on experiments.l2f_experiment_jobs",
            sql="DELETE FROM experiments.l2f_experiment_jobs WHERE id = :id",
            params={"id": g.j1},
        )
    )

    return attacks


# frozen expected group tallies
GROUP_COUNTS = {"plan": 12, "member": 10, "config": 10, "job": 15}
TOTAL_ATTACKS = 47

#: Frozen ordered manifest of the 47 case names. ``build_attacks`` must reproduce this
#: exactly (a drift guard asserts equality); parametrized tests iterate this tuple.
ATTACK_NAMES: tuple[str, ...] = (
    # plan (12)
    "plan_forged_snapshot_hash",
    "plan_forged_split_manifest_hash",
    "plan_forged_registry_snapshot_hash",
    "plan_wrong_feature_set_hash",
    "plan_wrong_feature_registry_hash",
    "plan_matrix_from_another_snapshot",
    "plan_non_train_matrix",
    "plan_wrong_train_matrix_hash",
    "plan_wrong_feature_set_id",
    "plan_invalid_derived_counts",
    "plan_duplicate_plan_hash",
    "plan_duplicate_logical_identity",
    # plan member (10)
    "member_validation_snapshot_member",
    "member_test_snapshot_member",
    "member_snapshot_from_another_snapshot",
    "member_matrix_from_another_matrix",
    "member_mismatched_dataset_registry",
    "member_substituted_bam_profile",
    "member_mismatched_feature_values_hash",
    "member_wrong_member_index",
    "member_cross_plan_lineage",
    "member_duplicate_logical_membership",
    # config / payload (10)
    "config_artifact_sha_ne_config_hash",
    "config_wrong_artifact_media_type",
    "config_wrong_payload_schema_version",
    "config_wrong_payload_media_type",
    "config_duplicate_config_hash",
    "planconfig_param_space_mismatch_plan",
    "planconfig_param_space_mismatch_payload",
    "planconfig_forged_config_hash",
    "planconfig_duplicate_plan_payload",
    "planconfig_duplicate_config_index",
    # job / immutability (15)
    "job_member_from_another_plan",
    "job_config_from_another_plan",
    "job_duplicate_job_key",
    "job_duplicate_logical_identity",
    "job_invalid_status",
    "plan_update_rejected",
    "plan_delete_rejected",
    "plan_member_update_rejected",
    "plan_member_delete_rejected",
    "config_payload_update_rejected",
    "config_payload_delete_rejected",
    "plan_config_update_rejected",
    "plan_config_delete_rejected",
    "job_identity_mutation_rejected",
    "job_delete_rejected",
)


def attacks_by_name(g: SeededGraph) -> dict[str, Attack]:
    return {a.name: a for a in build_attacks(g)}
