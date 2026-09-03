"""Seed a COMPLETE synthetic Phase-D matrix: 4 finalists x 10 members, all forty decided.

The utilities are supplied by the caller and chosen before closure runs, so the expected winner is
known in advance. Nothing here is real validation evidence.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from minos_engine.baseline.phase_d_observations import (
    ACCEPTED_EXECUTION_ENVIRONMENT_HASH,
    ACCEPTED_MINOS_SUBNET_COMMIT,
    ACCEPTED_SCORING_CONTRACT_HASH,
)
from minos_engine.storage.l2f_execution_contract import (
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)
from tests.integration.layer2_db.l2f2_phase_d_eval_seed import (
    H,
    U,
    provision_reference_root,
    provision_validation_truth,
    seed_two_validation_campaigns,
)

_METRICS_MEDIA_TYPE = "application/vnd.minos.l2f2-evaluation-metrics+json"


def seed_complete_matrix(
    engine: Any, authority: Any, utilities: dict[int, list[float]], tmp_path: Path
) -> None:
    """Build the genuine campaign, then decide all forty cells with the supplied utilities."""
    # decide_one_job=False: the accepted seeder would otherwise decide pair (0,0) with its own
    # score. Deleting that row afterwards is impossible by design -- the ledgers are
    # append-only -- and weakening that invariant to make seeding easy is not an option.
    seeded = seed_two_validation_campaigns(engine, authority, tmp_path, decide_one_job=False)
    provision_reference_root(tmp_path / "reference")
    # the evaluation ledger references a registered truth identity per member; register the
    # ten synthetic VALIDATION bundles through the accepted registrar.
    assert provision_validation_truth(engine, tmp_path / "practice") == 10
    plan_hash = authority.plan_hash

    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        plan_id = conn.execute(
            text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
            {"h": plan_hash},
        ).scalar_one()

        for member_index in range(10):
            member_id = conn.execute(
                text(
                    "SELECT id FROM experiments.l2f_experiment_plan_members "
                    " WHERE plan_id = :p AND member_index = :i"
                ),
                {"p": plan_id, "i": member_index},
            ).scalar_one()
            registry_id = conn.execute(
                text(
                    "SELECT dataset_registry_id FROM experiments.l2f_experiment_plan_members "
                    " WHERE id = :m"
                ),
                {"m": member_id},
            ).scalar_one()
            for config_index in range(4):
                config_id = conn.execute(
                    text(
                        "SELECT id FROM experiments.l2f_experiment_plan_configs "
                        " WHERE plan_id = :p AND config_index = :i"
                    ),
                    {"p": plan_id, "i": config_index},
                ).scalar_one()
                _decide(
                    conn,
                    plan_id=plan_id,
                    member_id=member_id,
                    config_id=config_id,
                    registry_id=registry_id,
                    member_index=member_index,
                    config_index=config_index,
                    utility=utilities[config_index][member_index],
                )
    assert seeded["impostor_plan_hash"] != plan_hash


def _decide(
    conn: Any,
    *,
    plan_id: str,
    member_id: str,
    config_id: str,
    registry_id: str,
    member_index: int,
    config_index: int,
    utility: float,
) -> None:
    """One SUCCEEDED job, one execution result, one ADMITTED evaluation."""
    tag = f"{member_index}-{config_index}"
    job_key = H(f"jobkey:{tag}")
    job_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f_experiment_jobs "
            "  (plan_id, plan_member_id, plan_config_id, job_key, status) "
            "VALUES (:p, :m, :c, :k, 'SUCCEEDED') RETURNING id"
        ),
        {"p": plan_id, "m": member_id, "c": config_id, "k": job_key},
    ).scalar_one()

    vcf = _artifact(conn, f"vcf:{tag}", L2F_VCF_MEDIA_TYPE, "l2f:gatk-vcf")
    manifest = _artifact(
        conn, f"man:{tag}", L2F_RESULT_MANIFEST_MEDIA_TYPE, "l2f:execution-result-json"
    )
    metrics = _artifact(conn, f"metrics:{tag}", _METRICS_MEDIA_TYPE, "l2f:evaluation-metrics")

    config_hash, space = conn.execute(
        text(
            "SELECT c.config_hash, p.parameter_space_hash "
            "  FROM experiments.l2f_experiment_plan_configs c "
            "  JOIN experiments.l2f_experiment_plans p ON p.id = c.plan_id "
            " WHERE c.id = :c"
        ),
        {"c": config_id},
    ).one()

    execution_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f_execution_results ("
            "  plan_id, job_id, job_key, plan_member_id, plan_config_id, config_hash, "
            "  parameter_space_hash, input_identity_hash, logical_argv_hash, "
            "  gatk_executable_sha256, gatk_version, vcf_artifact_id, vcf_sha256, vcf_media_type, "
            "  result_manifest_artifact_id, result_manifest_sha256, result_manifest_media_type, "
            "  result_hash, runtime_ms, execution_environment_hash) "
            "VALUES (:p, :j, :k, :m, :c, :ch, :ps, :iid, :argv, :gx, '4.5.0.0', :va, :vs, :vmt, "
            "        :ma, :ms, :mmt, :rh, :rt, :env) RETURNING id"
        ),
        {
            "p": plan_id,
            "j": job_id,
            "k": job_key,
            "m": member_id,
            "c": config_id,
            "ch": config_hash,
            "ps": space,
            "iid": H(f"input:{tag}"),
            "argv": H(f"argv:{tag}"),
            "gx": H("gatk-exe"),
            "va": vcf,
            "vs": H(f"vcf:{tag}"),
            "vmt": L2F_VCF_MEDIA_TYPE,
            "ma": manifest,
            "ms": H(f"man:{tag}"),
            "mmt": L2F_RESULT_MANIFEST_MEDIA_TYPE,
            "rh": H(f"result:{tag}"),
            # a constant runtime keeps the FIRST tie-break level decisive in these fixtures
            "rt": 60_000,
            "env": ACCEPTED_EXECUTION_ENVIRONMENT_HASH,
        },
    ).scalar_one()

    identity_id = conn.execute(
        text(
            "SELECT id FROM evaluation.dataset_evaluation_identity WHERE dataset_registry_id = :d"
        ),
        {"d": registry_id},
    ).scalar_one()

    conn.execute(
        text(
            "INSERT INTO evaluation.l2f_evaluation_results ("
            "  execution_result_id, execution_result_hash, dataset_registry_id, partition, "
            "  dataset_evaluation_identity_id, truth_vcf_sha256, truth_tbi_sha256, "
            "  mutations_vcf_sha256, mutations_tbi_sha256, scoring_contract_hash, "
            "  scorer_upstream_commit, scoring_py_sha256, validator_py_sha256, "
            "  happy_image_digest, bcftools_image_digest, metrics_artifact_id, "
            "  metrics_artifact_sha256, metrics_media_type, overcall_penalty, "
            "  minos_score_100, minos_score, admitted, admission_code, evaluation_hash) "
            "SELECT :e, :eh, :d, 'validation', :ident, d.truth_vcf_sha256, d.truth_tbi_sha256, "
            "       d.mutations_vcf_sha256, d.mutations_tbi_sha256, :contract, :commit, "
            "       :spy, :vpy, :happy, :bcftools, :mart, :msha, :mmt, 0, :s100, :s, true, "
            "       'ADMITTED', :vh "
            "  FROM evaluation.dataset_evaluation_identity d WHERE d.id = :ident"
        ),
        {
            "e": execution_id,
            "eh": H(f"result:{tag}"),
            "d": registry_id,
            "ident": identity_id,
            "contract": ACCEPTED_SCORING_CONTRACT_HASH,
            "commit": ACCEPTED_MINOS_SUBNET_COMMIT,
            "spy": H("scoring.py"),
            "vpy": H("validator.py"),
            "happy": "fake/happy@sha256:" + "a" * 64,
            "bcftools": "fake/bcftools@sha256:" + "b" * 64,
            "mart": metrics,
            "msha": H(f"metrics:{tag}"),
            "mmt": _METRICS_MEDIA_TYPE,
            "s100": utility * 100.0,
            "s": utility,
            "vh": H(f"evalhash:{tag}"),
        },
    )


def _artifact(conn: Any, label: str, media_type: str, provenance: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (id, uri, sha256, media_type, size_bytes, "
                "  provenance) VALUES (:i, :u, :s, :m, 16, :p) RETURNING id"
            ),
            {
                "i": U(f"art:{label}"),
                "u": f"mem://{label}",
                "s": H(label),
                "m": media_type,
                "p": provenance,
            },
        ).scalar_one()
    )


_ = uuid
