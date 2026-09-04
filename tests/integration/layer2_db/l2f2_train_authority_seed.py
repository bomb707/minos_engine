"""Seed a minimal but VALID TRAIN authority set, with one field deliberately perturbable.

The point of these fixtures is the negatives: the qualification surface must refuse an authority
whose ``candidate_set_hash`` or ``parameter_space_hash`` disagrees with its plan, and must refuse
a per-phase shape mutation even when the three phases still total 1175.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text

__all__ = ["FROZEN_SHAPES", "seed_train_authorities"]

_NS = uuid.UUID("0000000a-1111-2222-3333-4444444444aa")
PARAMETER_SPACE = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

#: phase -> (plan_hash, members, candidates, logical_jobs)
FROZEN_SHAPES: dict[str, tuple[str, int, int, int]] = {
    "PHASE_A": ("97ba598778a5fc634345ded0901e4975af9c6b875c5b70fc7e76f2ae482e1b9a", 5, 39, 195),
    "PHASE_B": ("e80594043580334ddf2504577e2fa030dff0c1217ac334804d9304a0ec72596b", 10, 48, 480),
    "PHASE_C": ("03b846e735e5817a8df7d5c37ae15778a955828a56513b16cef8ff2193a0aa43", 50, 10, 500),
}


def _u(label: str) -> str:
    return str(uuid.uuid5(_NS, label))


def _h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def seed_train_authorities(
    engine: Any,
    *,
    authority_overrides: dict[str, dict[str, Any]] | None = None,
    plan_overrides: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Seed three TRAIN plans and their execution authorities.

    ``authority_overrides`` / ``plan_overrides`` perturb one side of a phase so the disagreement
    the surface must catch can be constructed exactly.
    """
    authority_overrides = authority_overrides or {}
    plan_overrides = plan_overrides or {}

    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        split = _u("split")
        conn.execute(
            text(
                "INSERT INTO catalog.split_snapshots (id, epoch, salt, split_policy_version, "
                "  policy_hash, manifest_hash, registry_snapshot_hash, "
                "  ancestor_v1_dataset_registry_hash, transition_count, sample_count, "
                "  count_train, count_validation, count_test) "
                "VALUES (:i, 1, 's', 'v2', :p, :m, :r, :a, 0, 60, 50, 10, 0)"
            ),
            {"i": split, "p": _h("p"), "m": _h("m"), "r": _h("r"), "a": _h("a")},
        )
        snapshot = _u("snapshot")
        conn.execute(
            text(
                "INSERT INTO profiling.profile_snapshots (id, epoch, split_snapshot_id, "
                "  split_manifest_hash, registry_snapshot_hash, member_count, snapshot_hash) "
                "VALUES (:i, 1, :s, :m, :r, 60, :h)"
            ),
            {"i": snapshot, "s": split, "m": _h("m"), "r": _h("r"), "h": _h("snap")},
        )
        feature_set = _u("feature_set")
        conn.execute(
            text(
                "INSERT INTO profiling.feature_sets (id, feature_set_hash, registry_hash, "
                "  column_count, column_manifest) VALUES (:i, :f, :r, 81, '[]'::jsonb)"
            ),
            {"i": feature_set, "f": _h("fs"), "r": _h("fr")},
        )
        artifact = _u("artifact")
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (id, uri, sha256, media_type) "
                "VALUES (:i, 'mem://m', :s, 'application/octet-stream')"
            ),
            {"i": artifact, "s": _h("art")},
        )
        matrix = _u("matrix")
        conn.execute(
            text(
                "INSERT INTO profiling.feature_matrices (id, profile_snapshot_id, partition, "
                "  feature_set_id, matrix_hash, artifact_sha256, matrix_artifact_id, row_count, "
                "  column_count) VALUES (:i, :s, 'train', :f, :m, :a, :art, 50, 81)"
            ),
            {
                "i": matrix,
                "s": snapshot,
                "f": feature_set,
                "m": _h("mx"),
                "a": _h("art"),
                "art": artifact,
            },
        )

        for phase, (plan_hash, members, candidates, logical) in FROZEN_SHAPES.items():
            plan_id = _u(f"plan:{phase}")
            plan = {
                "id": plan_id,
                "snap": snapshot,
                "matrix": matrix,
                "fset": feature_set,
                "plan_hash": plan_hash,
                "candidate_set_hash": _h(f"cs:{phase}"),
                "parameter_space_hash": PARAMETER_SPACE,
                "train_member_count": members,
                "candidate_count": candidates,
                "logical_job_count": logical,
            }
            plan.update(plan_overrides.get(phase, {}))
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_plans ("
                    "  id, profile_snapshot_id, train_feature_matrix_id, feature_set_id, "
                    "  partition, snapshot_hash, split_manifest_hash, registry_snapshot_hash, "
                    "  train_matrix_hash, train_feature_view_hash, feature_set_hash, "
                    "  feature_registry_hash, gatk_registry_hash, parameter_space_hash, "
                    "  experiment_parameter_policy_hash, candidate_set_hash, train_member_count, "
                    "  candidate_count, logical_job_count, plan_hash) "
                    "VALUES (:id, :snap, :matrix, :fset, 'train', :sh, :smh, :rsh, :tmh, :tfv, "
                    "        :fsh, :frh, :grh, :parameter_space_hash, :epp, "
                    "        :candidate_set_hash, :train_member_count, :candidate_count, "
                    "        :logical_job_count, :plan_hash)"
                ),
                {
                    **plan,
                    "sh": _h("snap"),
                    "smh": _h("m"),
                    "rsh": _h("r"),
                    "tmh": _h("mx"),
                    "tfv": _h("fv"),
                    "fsh": _h("fs"),
                    "frh": _h("fr"),
                    "grh": _h("gatk"),
                    "epp": _h("policy"),
                },
            )

            authority = {
                "phase": phase,
                "plan_id": plan_id,
                "plan_hash": plan_hash,
                "candidate_set_hash": plan["candidate_set_hash"],
                "parameter_space_hash": plan["parameter_space_hash"],
                "member_count": plan["train_member_count"],
                "candidate_count": plan["candidate_count"],
                "logical_job_count": plan["logical_job_count"],
                "baseline_protocol_hash": PROTOCOL,
                # ck_l2f2_authority_canary_phase: PHASE_A carries the canary, B and C must not.
                "canary": _h("canary") if phase == "PHASE_A" else None,
            }
            authority.update(authority_overrides.get(phase, {}))
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f2_execution_authorities ("
                    "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                    "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                    "  logical_job_count, canary_job_key) "
                    "VALUES (:baseline_protocol_hash, :phase, :plan_id, :plan_hash, :ts, "
                    "        :candidate_set_hash, :parameter_space_hash, :member_count, "
                    "        :candidate_count, :logical_job_count, :canary)"
                ),
                {**authority, "ts": _h("schedule")},
            )
