"""The 49-case L2-F direct-SQL constraint attack manifest (F3-A4).

Each case is a single, independently meaningful attempt to violate exactly one PostgreSQL
invariant of the ``0006`` schema against the seeded valid graph. A case declares its target
operation, the mutation, the expected SQLSTATE, and the expected named constraint or trigger
mechanism. Cases are executed one at a time inside a SAVEPOINT and rolled back so they cannot
contaminate one another (see ``test_l2f_attack_matrix``).

Grouping and count are frozen: 12 plan + 12 plan-member + 10 config/payload + 15
job/immutability = 49 unique cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tests.integration.layer2_db.l2f_seed import (
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


def _ins(
    name: str,
    group: str,
    table: str,
    sqlstate: str,
    mech: Mechanism,
    expect: str,
    row: dict[str, Any],
    op: str = "insert",
) -> Attack:
    return Attack(name, group, op, table, sqlstate, mech, expect, row=row)


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
    # mismatched dataset_registry_id: change only dataset_registry_id (breaks dataset consistency)
    A(
        _ins(
            "member_mismatched_dataset_registry",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(dataset_registry_id=g.dsr["D4"]),
        )
    )
    # substituted bam_profile_id (matrix FK unaffected -> only snapshot FK fails)
    A(
        _ins(
            "member_substituted_bam_profile",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(bam_profile_id=g.bam["D1"]),
        )
    )
    # mismatched feature_values_hash (breaks both fvh-consistency FKs; snapshot FK fires first)
    A(
        _ins(
            "member_mismatched_feature_values_hash",
            "member",
            PM,
            FK,
            "constraint",
            "fk_l2f_pm_snapshot_member",
            g.new_member(feature_values_hash=g.fvh["D4"]),
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
    # duplicate snapshot member (only the (plan, psm) unique collides)
    A(
        _ins(
            "member_duplicate_snapshot_member",
            "member",
            PM,
            UNIQUE,
            "constraint",
            "uq_l2f_pm_plan_snapshot_member",
            g.new_member(profile_snapshot_member_id=g.psm[("SA", "D1")]),
        )
    )
    # duplicate matrix member (only the (plan, fmm) unique collides)
    A(
        _ins(
            "member_duplicate_matrix_member",
            "member",
            PM,
            UNIQUE,
            "constraint",
            "uq_l2f_pm_plan_matrix_member",
            g.new_member(feature_matrix_member_id=g.fmm[("MA", "D1")]),
        )
    )
    # duplicate member_index (only the (plan, member_index) unique collides)
    A(
        _ins(
            "member_duplicate_member_index",
            "member",
            PM,
            UNIQUE,
            "constraint",
            "uq_l2f_pm_plan_member_index",
            g.new_member(member_index=0),
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
    A(
        _ins(
            "config_wrong_payload_media_type",
            "config",
            CP,
            CHECK,
            "constraint",
            "ck_l2f_cp_media_type",
            g.new_config_payload(media_type="WRONG"),
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
GROUP_COUNTS = {"plan": 12, "member": 12, "config": 10, "job": 15}
TOTAL_ATTACKS = 49

#: Frozen ordered manifest of the 49 case names. ``build_attacks`` must reproduce this
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
    # plan member (12)
    "member_validation_snapshot_member",
    "member_test_snapshot_member",
    "member_snapshot_from_another_snapshot",
    "member_matrix_from_another_matrix",
    "member_mismatched_dataset_registry",
    "member_substituted_bam_profile",
    "member_mismatched_feature_values_hash",
    "member_wrong_member_index",
    "member_cross_plan_lineage",
    "member_duplicate_snapshot_member",
    "member_duplicate_matrix_member",
    "member_duplicate_member_index",
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


# unused import guard (media type documents the canonical value used in seed baselines)
_ = CONFIG_MEDIA_TYPE
