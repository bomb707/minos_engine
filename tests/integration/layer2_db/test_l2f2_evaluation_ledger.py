"""L2-F2-A Tier-2 controls against a REAL migrated PostgreSQL schema.

Positive happy paths come first and are mandatory. An L2-F1 defect reached a real GATK
qualification precisely because the only test of a production query exercised an early-return
guard and never executed the SQL, so every production statement here is proven to RUN, not just
to reject.

No GATK, no hap.py, no Docker, no real practice truth: truth files are synthetic bytes with
deterministic hashes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from minos_engine.evaluation.contracts import (
    ComparisonScope,
    EvaluationInputs,
    TruthIdentity,
    build_metrics_artifact_bytes,
)
from minos_engine.evaluation.evaluator import (
    EvaluationArtifactPublisher,
    build_evaluation_record,
    evaluate_metrics,
    record_evaluation_failure,
    record_evaluation_result,
    register_metrics_artifact,
)
from minos_engine.evaluation.scoring_contract import (
    compute_scoring_contract_hash,
    load_scoring_authority,
)
from minos_engine.evaluation.truth_registration import (
    register_train_truth_identities,
)
from tests.integration.layer2_db.conftest import alembic_downgrade, alembic_upgrade
from tests.integration.layer2_db.test_l2f_execution import env as _env_fixture

env = _env_fixture

_F5 = "0008_l2f_execution_results"
_F2A = "0009_l2f_evaluation_results"
_F2A_CORRECTIVE = "0010_l2f2_evaluation_corrective"
#: 0013 makes the four AdvancedScorer components nullable, because the pinned upstream scorer
#: does not expose them. Evaluation persistence therefore requires it.
#:
#: This suite stops at 0014 deliberately. Its execution row is produced by the HISTORICAL L2-F1
#: path, which cannot persist against 0015's widened writer, and 0015 refuses to upgrade a store
#: that already holds one — by design, since such a row has no runtime identity. Evaluation
#: persistence itself is untouched by 0015, and the L2-F2 Phase-A suites exercise the evaluator
#: against a 0015 store end to end.
_SCORE_ORACLE = "0014_l2f2_exec_failure_runtime"
_METRICS = {
    "f1_snp": 0.95,
    "f1_indel": 0.9,
    "recall_snp": 0.95,
    "recall_indel": 0.9,
    "truth_total_snp": 1000,
    "truth_total_indel": 100,
    "query_total_snp": 1000,
    "query_total_indel": 100,
    "fp_snp": 2,
    "fp_indel": 1,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _authority() -> Any:
    return load_scoring_authority(_repo_root())


@pytest.fixture
def evaluated(env: Any) -> Any:
    """A real successful execution, with the schema advanced to the score-oracle revision."""
    dispatched = env.run()
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    url = str(env.engine.url.render_as_string(hide_password=False))
    env.engine.dispose()
    alembic_upgrade(url, _SCORE_ORACLE)
    from sqlalchemy import create_engine

    env.engine = create_engine(url)
    # the L2-F1 fixture seeds the plan graph but not catalog.split_allocations, which the TRAIN
    # registration projection reads. Seed every plan dataset as TRAIN so the projection is real.
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                "INSERT INTO catalog.split_allocations "
                "  (dataset_registry_id, partition, sort_order, manifest_hash) "
                "SELECT DISTINCT m.dataset_registry_id, 'train', "
                "       row_number() OVER (ORDER BY m.dataset_registry_id), :h "
                "  FROM experiments.l2f_experiment_plan_members m "
                " WHERE NOT EXISTS (SELECT 1 FROM catalog.split_allocations sa "
                "                    WHERE sa.dataset_registry_id = m.dataset_registry_id)"
            ),
            {"h": "a" * 64},
        )
    return dispatched


def _register_truth(env: Any, root: Path, *, marker: bytes = b"v1") -> dict[str, str]:
    """Register synthetic TRAIN truth for every registered target."""
    with env.engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT dataset_registry_id, round_id FROM "
                    "evaluation.l2f_train_truth_registration_targets"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        directory = root / f"round_{row['round_id']}"
        directory.mkdir(parents=True, exist_ok=True)
        for name in (
            "truth.vcf.gz",
            "truth.vcf.gz.tbi",
            "mutations.vcf.gz",
            "mutations.vcf.gz.tbi",
        ):
            (directory / name).write_bytes(marker + name.encode())
    return {str(r["dataset_registry_id"]): str(r["round_id"]) for r in rows}


def _register_artifact(env: Any, published: Any) -> str:
    """Register through the production registrar — never a privileged catalog INSERT."""
    artifact_id, _created = register_metrics_artifact(env.engine, published)
    return artifact_id


def _oracle_result(**overrides: Any) -> Any:
    """A stand-in for what the pinned upstream scorer returned. Never computed locally."""
    from minos_engine.evaluation.minos_subnet_oracle import MinosSubnetOracleResult

    authority = _authority()
    base: dict[str, Any] = {
        "scored": True,
        "metrics": dict(_METRICS),
        "advanced_score_100": 86.25,
        "minos_score": 0.8625,
        "minos_score_accepted": True,
        "zero_input_fingerprint": False,
        "admitted": True,
        "admission_code": "ADMITTED",
        "upstream_commit": authority.upstream_commit,
        "upstream_source_sha256": {
            "utils/scoring.py": authority.scoring_py_sha256,
            "neurons/validator.py": authority.validator_py_sha256,
            "templates/tool_params.py": authority.tool_params_py_sha256,
        },
        "upstream_provenance": {
            "happy_upstream_ref": authority.happy.upstream_ref,
            "bcftools_upstream_ref": authority.bcftools.upstream_ref,
        },
        "happy_upstream_ref": authority.happy.upstream_ref,
        "happy_resolved_digest": authority.happy.resolved_digest,
        "bcftools_upstream_ref": authority.bcftools.upstream_ref,
        "bcftools_resolved_digest": authority.bcftools.resolved_digest,
    }
    base.update(overrides)
    return MinosSubnetOracleResult(**base)


def _scoring_inputs() -> Any:
    from minos_engine.evaluation.contracts import ScoringInputIdentity

    return ScoringInputIdentity(
        truth_vcf_sha256="1" * 64,
        truth_tbi_sha256="2" * 64,
        mutations_vcf_sha256="3" * 64,
        mutations_tbi_sha256="4" * 64,
        query_vcf_sha256="5" * 64,
        reference_fasta_sha256="6" * 64,
        reference_sdf_present=True,
    )


def _publish(env: Any, tmp_path: Path, inputs: EvaluationInputs) -> tuple[Any, Any, str, str, Any]:
    """Record an upstream outcome, publish the canonical document, register it as production does."""
    import os

    root = tmp_path / "evaluation_artifacts"
    root.mkdir(exist_ok=True)
    os.chmod(root, 0o2750)
    artifact, admission, _contract = evaluate_metrics(
        inputs=inputs,
        oracle_result=_oracle_result(),
        scoring_inputs=_scoring_inputs(),
        authority=_authority(),
    )
    payload = build_metrics_artifact_bytes(artifact)
    published = EvaluationArtifactPublisher(root).publish(payload)
    artifact_id = _register_artifact(env, published)
    return artifact, artifact.upstream, admission, artifact_id, published


def _inputs_for(env: Any, dispatched: Any) -> EvaluationInputs:
    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT execution_result_hash, dataset_id, partition, vcf_sha256, chromosome, "
                    "region_start0, region_end0_exclusive "
                    "FROM evaluation.l2f_completed_execution_inputs "
                    "WHERE execution_result_id = :i"
                ),
                {"i": _result_id(env, dispatched)},
            )
            .mappings()
            .one()
        )
    with env.engine.connect() as conn:
        truth = (
            conn.execute(
                text(
                    "SELECT truth_vcf_sha256, truth_tbi_sha256, mutations_vcf_sha256, "
                    "mutations_tbi_sha256 FROM evaluation.dataset_evaluation_identity d "
                    "JOIN experiments.l2f_experiment_plan_members m "
                    "  ON m.dataset_registry_id = d.dataset_registry_id "
                    "JOIN experiments.l2f_experiment_jobs j ON j.plan_member_id = m.id "
                    "JOIN experiments.l2f_execution_results r ON r.job_id = j.id "
                    "WHERE r.id = :i"
                ),
                {"i": _result_id(env, dispatched)},
            )
            .mappings()
            .one()
        )
    return EvaluationInputs(
        execution_result_hash=str(row["execution_result_hash"]),
        dataset_id=str(row["dataset_id"]),
        partition=str(row["partition"]),
        vcf_sha256=str(row["vcf_sha256"]),
        truth=TruthIdentity(**{k: str(v) for k, v in truth.items()}),
        scope=ComparisonScope(
            chromosome=str(row["chromosome"]),
            region_start0=int(row["region_start0"]),
            region_end0_exclusive=int(row["region_end0_exclusive"]),
            region_source=f"{row['chromosome']}:{int(row['region_start0']) + 1}-"
            f"{int(row['region_end0_exclusive'])}",
        ),
    )


def _result_id(env: Any, dispatched: Any) -> str:
    with env.engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM experiments.l2f_execution_results WHERE job_key = :k"),
                {"k": dispatched.job_key},
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# A / B — migration lifecycle
# --------------------------------------------------------------------------- #
def test_upgrade_creates_every_l2f2_object_and_downgrade_removes_exactly_them(env: Any) -> None:
    url = str(env.engine.url.render_as_string(hide_password=False))
    env.engine.dispose()
    from sqlalchemy import create_engine

    def inventory(engine: Any) -> dict[str, set[str]]:
        with engine.connect() as conn:
            return {
                "tables": {
                    r[0]
                    for r in conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='evaluation'")
                    )
                },
                "views": {
                    r[0]
                    for r in conn.execute(
                        text("SELECT viewname FROM pg_views WHERE schemaname='evaluation'")
                    )
                },
                "functions": {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='evaluation'"
                        )
                    )
                },
            }

    engine = create_engine(url)
    before = inventory(engine)
    engine.dispose()

    alembic_upgrade(url, _F2A)
    engine = create_engine(url)
    after = inventory(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _F2A
    engine.dispose()
    assert {"l2f_evaluation_results", "l2f_evaluation_failures"} <= after["tables"]
    assert {"l2f_completed_execution_inputs", "l2f_train_truth_registration_targets"} <= after[
        "views"
    ]
    assert {
        "l2f_register_train_truth_identity",
        "l2f_record_evaluation_result",
        "l2f_record_evaluation_failure",
    } <= after["functions"]

    alembic_downgrade(url, _F5)
    engine = create_engine(url)
    restored = inventory(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _F5
        # the legacy L2-B placeholder is untouched throughout
        assert conn.execute(text("SELECT count(*) FROM evaluation.evaluations")).scalar() == 0
    engine.dispose()
    assert restored == before, "downgrade did not restore the exact 0008 inventory"


# --------------------------------------------------------------------------- #
# C / D / E — TRAIN truth registration
# --------------------------------------------------------------------------- #
def test_train_truth_registration_succeeds_and_is_idempotent(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    first = register_train_truth_identities(env.engine, dataset_root=root)
    assert first.requested >= 1 and first.created == first.requested
    second = register_train_truth_identities(env.engine, dataset_root=root)
    assert second.created == 0 and second.already_registered == second.requested


def test_a_differing_truth_hash_conflicts_and_never_overwrites(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root, marker=b"v1")
    register_train_truth_identities(env.engine, dataset_root=root)
    _register_truth(env, root, marker=b"TAMPERED")
    with pytest.raises(Exception) as excinfo:
        register_train_truth_identities(env.engine, dataset_root=root)
    assert "different bytes" in str(excinfo.value)


def test_the_train_projection_exposes_train_only(evaluated: Any, env: Any) -> None:
    """Validation and test are structurally absent from the registration interface."""
    with env.engine.connect() as conn:
        targets = {
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT dataset_registry_id FROM "
                    "evaluation.l2f_train_truth_registration_targets"
                )
            )
        }
        train = {
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT dataset_registry_id FROM catalog.split_allocations "
                    "WHERE partition = 'train'"
                )
            )
        }
        non_train = {
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT dataset_registry_id FROM catalog.split_allocations "
                    "WHERE partition <> 'train'"
                )
            )
        }
    assert targets == train
    assert not (targets & non_train)


# --------------------------------------------------------------------------- #
# F / G — execution projection binds exactly one dataset
# --------------------------------------------------------------------------- #
def test_the_execution_projection_returns_the_correct_dataset(evaluated: Any, env: Any) -> None:
    result_id = _result_id(env, evaluated)
    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT dataset_registry_id, partition, execution_result_hash, vcf_sha256 "
                    "FROM evaluation.l2f_completed_execution_inputs WHERE execution_result_id = :i"
                ),
                {"i": result_id},
            )
            .mappings()
            .one()
        )
        expected = (
            conn.execute(
                text(
                    "SELECT m.dataset_registry_id, m.partition, r.result_hash, r.vcf_sha256 "
                    "FROM experiments.l2f_execution_results r "
                    "JOIN experiments.l2f_experiment_jobs j ON j.id = r.job_id "
                    "JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
                    "WHERE r.id = :i"
                ),
                {"i": result_id},
            )
            .mappings()
            .one()
        )
    assert str(row["dataset_registry_id"]) == str(expected["dataset_registry_id"])
    assert row["partition"] == expected["partition"]
    assert row["execution_result_hash"] == expected["result_hash"]


def test_the_evaluator_cannot_substitute_another_dataset_or_truth(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """Identity is DERIVED inside the SECURITY DEFINER function, never accepted from the caller.

    The persistence signature has no dataset/truth parameters at all, so substitution is not
    merely rejected — it is unrepresentable.
    """
    import inspect

    from minos_engine.evaluation import evaluator

    signature = inspect.signature(evaluator.record_evaluation_result)
    for forbidden in ("dataset_registry_id", "partition", "truth_vcf_sha256", "truth"):
        assert forbidden not in signature.parameters, forbidden


# --------------------------------------------------------------------------- #
# H / I / J — evaluation persistence
# --------------------------------------------------------------------------- #
def _persist(env: Any, evaluated: Any, tmp_path: Path) -> Any:
    inputs = _inputs_for(env, evaluated)
    artifact, _upstream_out, admission, artifact_id, published = _publish(env, tmp_path, inputs)
    return record_evaluation_result(
        env.engine,
        build_evaluation_record(
            execution_result_id=_result_id(env, evaluated),
            inputs=inputs,
            artifact=artifact,
            admission_code=admission,
            authority=_authority(),
            metrics_artifact_id=artifact_id,
            metrics=published,
        ),
    )


def test_evaluation_persistence_succeeds_and_binds_derived_identity(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)

    persisted = _persist(env, evaluated, tmp_path)
    assert persisted.created is True
    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT partition, admitted, admission_code, minos_score, minos_score_100, "
                    "dataset_registry_id, truth_vcf_sha256 "
                    "FROM evaluation.l2f_evaluation_results WHERE evaluation_hash = :h"
                ),
                {"h": persisted.evaluation_hash},
            )
            .mappings()
            .one()
        )
    assert row["partition"] == "train"
    assert row["admitted"] is True and row["admission_code"] == "ADMITTED"
    assert row["minos_score"] == pytest.approx(row["minos_score_100"] / 100.0, abs=1e-9)
    assert row["truth_vcf_sha256"] is not None


def test_exact_replay_returns_the_existing_evaluation(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    first = _persist(env, evaluated, tmp_path)
    second = _persist(env, evaluated, tmp_path)
    assert second.created is False
    assert second.evaluation_id == first.evaluation_id
    with env.engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM evaluation.l2f_evaluation_results")).scalar()
            == 1
        )


def test_a_conflicting_replay_under_the_same_contract_fails_closed(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    inputs = _inputs_for(env, evaluated)
    artifact, _upstream_out, admission, artifact_id, published = _publish(env, tmp_path, inputs)

    def _record(evaluation_inputs: EvaluationInputs) -> Any:
        return build_evaluation_record(
            execution_result_id=_result_id(env, evaluated),
            inputs=evaluation_inputs,
            artifact=artifact,
            admission_code=admission,
            authority=_authority(),
            metrics_artifact_id=artifact_id,
            metrics=published,
        )

    record_evaluation_result(env.engine, _record(inputs))
    # same execution + same contract, DIFFERENT science -> different evaluation_hash -> refused
    with pytest.raises(Exception) as excinfo:
        record_evaluation_result(
            env.engine, _record(inputs.model_copy(update={"vcf_sha256": "9" * 64}))
        )
    assert "different identity" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# K / L — failure ledger and success/failure exclusivity
# --------------------------------------------------------------------------- #
def test_evaluation_failure_persistence_is_idempotent(evaluated: Any, env: Any) -> None:
    contract = compute_scoring_contract_hash(_authority())
    result_id = _result_id(env, evaluated)
    first_id, created = record_evaluation_failure(
        env.engine,
        execution_result_id=result_id,
        scoring_contract_hash=contract,
        failure_code="HAPPY_NONZERO_EXIT",
        tool_exit_code=3,
    )
    assert created is True
    again_id, again = record_evaluation_failure(
        env.engine,
        execution_result_id=result_id,
        scoring_contract_hash=contract,
        failure_code="HAPPY_NONZERO_EXIT",
        tool_exit_code=3,
    )
    assert again is False and again_id == first_id


def test_success_and_failure_cannot_both_exist(evaluated: Any, env: Any, tmp_path: Path) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    contract = compute_scoring_contract_hash(_authority())
    result_id = _result_id(env, evaluated)

    record_evaluation_failure(
        env.engine,
        execution_result_id=result_id,
        scoring_contract_hash=contract,
        failure_code="HAPPY_TIMEOUT",
    )
    with pytest.raises(Exception) as excinfo:
        _persist(env, evaluated, tmp_path)
    assert "already failed" in str(excinfo.value)


def test_failure_after_success_is_rejected(evaluated: Any, env: Any, tmp_path: Path) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    _persist(env, evaluated, tmp_path)
    with pytest.raises(Exception) as excinfo:
        record_evaluation_failure(
            env.engine,
            execution_result_id=_result_id(env, evaluated),
            scoring_contract_hash=compute_scoring_contract_hash(_authority()),
            failure_code="EVALUATION_ERROR",
        )
    assert "already succeeded" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# M — append-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_the_evaluation_ledgers_are_append_only(
    evaluated: Any, env: Any, tmp_path: Path, statement: str
) -> None:
    root = tmp_path / "practice"
    root.mkdir()
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    _persist(env, evaluated, tmp_path)
    sql = (
        "UPDATE evaluation.l2f_evaluation_results SET minos_score = 0.1"
        if statement == "UPDATE"
        else "DELETE FROM evaluation.l2f_evaluation_results"
    )
    with pytest.raises(DatabaseError), env.engine.connect() as conn, conn.begin():
        conn.execute(text(sql))


# --------------------------------------------------------------------------- #
# N / O / P — the external evaluator SERVICE principal
# --------------------------------------------------------------------------- #
def test_an_external_evaluator_service_login_has_exactly_the_intended_authority(
    evaluated: Any, env: Any
) -> None:
    """``minos_evaluator`` stays a NOLOGIN group role; the credential identity is external."""
    from sqlalchemy import create_engine

    url = env.engine.url
    role = "minos_evaluator_ci_svc"
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'ci-only-ephemeral'"))
        conn.execute(text(f"GRANT minos_evaluator TO {role}"))
        conn.execute(text("GRANT CONNECT ON DATABASE " + url.database + f" TO {role}"))

    try:
        # the group role itself must remain NOLOGIN
        with env.engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT rolcanlogin FROM pg_roles WHERE rolname='minos_evaluator'")
                ).scalar()
                is False
            )

        svc = create_engine(url.set(username=role, password="ci-only-ephemeral"))
        try:
            with svc.connect() as conn:
                # MUST be able to read the two evaluator projections
                conn.execute(
                    text("SELECT count(*) FROM evaluation.l2f_train_truth_registration_targets")
                ).scalar()
                conn.execute(
                    text("SELECT count(*) FROM evaluation.l2f_completed_execution_inputs")
                ).scalar()
                conn.execute(
                    text("SELECT count(*) FROM evaluation.l2f_evaluation_results")
                ).scalar()

            # MUST NOT be able to mutate the experiment execution ledger
            for sql in (
                "UPDATE experiments.l2f_execution_results SET runtime_ms = 1",
                "DELETE FROM experiments.l2f_experiment_jobs",
                "UPDATE experiments.l2f_experiment_plan_configs SET config_index = 99",
            ):
                with pytest.raises(DatabaseError), svc.connect() as conn, conn.begin():
                    conn.execute(text(sql))
        finally:
            svc.dispose()
    finally:
        with env.engine.connect() as conn, conn.begin():
            conn.execute(text(f"REVOKE ALL ON DATABASE {url.database} FROM {role}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


def test_non_evaluator_roles_get_no_l2f2_evaluation_authority(evaluated: Any, env: Any) -> None:
    """live / runner / trainer must not reach the truth-aware ledger at all."""
    with env.engine.connect() as conn:
        for role in ("minos_live", "minos_runner", "minos_trainer"):
            granted = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.table_privileges "
                    "WHERE grantee = :r AND table_schema = 'evaluation' "
                    "AND table_name IN ('l2f_evaluation_results','l2f_evaluation_failures')"
                ),
                {"r": role},
            ).scalar()
            assert granted == 0, role


# --------------------------------------------------------------------------- #
# Q — HARNESS evidence still verifies while the repository head advances
# --------------------------------------------------------------------------- #
def test_harness_offline_verification_still_passes_as_the_repository_head_advances() -> None:
    """The seam property, re-observed at the corrective head.

    HARNESS-READY proves the migration state of the L2-F1 source it qualified. Additive L2-F2
    migrations legitimately move the repository head; they must never move the HARNESS head or
    invalidate frozen evidence.
    """
    from minos_engine.qualification.l2f_accepted_identities import (
        recompute_alembic_head,
        recompute_harness_alembic_head,
    )
    from minos_engine.qualification.l2f_harness_ready_runner import (
        verify_committed_harness_ready_gate,
    )

    root = _repo_root()
    # the seam property, not a pinned value: the repository head is free to advance with each
    # additive L2-F2 migration, while the HARNESS head stays anchored to the stage it qualified.
    repository = recompute_alembic_head(root)
    assert repository > "0009_l2f_evaluation_results", repository
    assert recompute_harness_alembic_head(root) == "0008_l2f_execution_results"
    if not (root / "gates" / "harness-ready.json").exists():  # pragma: no cover
        pytest.skip("HARNESS evidence is not committed in this tree")
    result = verify_committed_harness_ready_gate(
        base_dir=root,
        gate_path=root / "gates" / "harness-ready.json",
        qualification_path=root / "reports" / "layer2" / "harness-ready-result.json",
    )
    assert result["ok"] is True, result["reasons"]
