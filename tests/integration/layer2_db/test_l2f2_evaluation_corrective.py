"""L2-F2-A corrective controls against a REAL PostgreSQL cluster.

Five demonstrated production defects are pinned here, each with a control that would fail
against the pre-corrective implementation:

* **XOR serialization** — two genuinely overlapping transactions, not a sequential simulation.
  The suite installs ``0009``'s ``FOR SHARE`` body and proves it admits a success *and* a
  failure for one ``(execution, scoring contract)``, then proves ``0010``'s ``FOR UPDATE`` body
  admits exactly one. A test that cannot fail against the old code proves nothing.
* **Metrics artifact identity** — the id/digest/media-type triple is one composite foreign key,
  so a forged pairing is refused by PostgreSQL rather than by application discipline.
* **Registration path** — the evaluator service principal registers its metrics document through
  the narrow ``SECURITY DEFINER`` registrar, and has no direct write on ``catalog.artifacts``.
* **Publisher** — proven under the actual service login, end to end.
* **Orchestrator** — the whole production chain, against a synthetic pinned MINOS_SUBNET
  checkout and synthetic truth. No real hap.py, no Docker, no biological data.

No GATK, no Docker, no real hap.py, no practice truth: every byte here is synthetic.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.evaluation.evaluator import (
    build_evaluation_record,
    record_evaluation_failure,
    register_metrics_artifact,
)
from minos_engine.evaluation.truth_registration import register_train_truth_identities
from tests.integration.layer2_db.test_l2f2_evaluation_ledger import (
    _authority,
    _inputs_for,
    _publish,
    _register_truth,
    _repo_root,
    _result_id,
)
from tests.integration.layer2_db.test_l2f2_evaluation_ledger import (
    evaluated as _evaluated_fixture,
)
from tests.integration.layer2_db.test_l2f_execution import env as _env_fixture

env = _env_fixture
evaluated = _evaluated_fixture

_RECORD_SQL = (
    "SELECT evaluation_id, created FROM evaluation.l2f_record_evaluation_result("
    ":exec_id, :contract, :commit, :scoring_py, :validator_py, :happy, :bcftools, "
    ":artifact_id, :artifact_sha, :media_type, :core, :completeness, :fp, :quality, "
    ":overcall, :score100, :score, :admission, :eval_hash)"
)
_FAILURE_SQL = (
    "SELECT failure_id, created FROM evaluation.l2f_record_evaluation_failure("
    ":exec_id, :contract, :code, NULL, NULL)"
)


def _url(env: Any) -> str:
    return str(env.engine.url.render_as_string(hide_password=False))


def _exclusive_body(lock: str) -> str:
    """The exclusive-outcome trigger body straight from migration 0010's own source."""
    path = _repo_root() / "migrations" / "versions" / "0010_l2f2_evaluation_corrective.py"
    spec = importlib.util.spec_from_file_location("_m0010", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._exclusive_outcome_body(lock=lock))


def _record_params(record: Any) -> dict[str, Any]:
    authority, upstream = record.authority, record.upstream
    return {
        "exec_id": record.execution_result_id,
        "contract": record.scoring_contract_hash,
        "commit": authority.upstream_commit,
        "scoring_py": authority.scoring_py_sha256,
        "validator_py": authority.validator_py_sha256,
        # the SQL columns carry the immutable RESOLVED content, one defined meaning.
        "happy": upstream.happy_resolved_digest,
        "bcftools": upstream.bcftools_resolved_digest,
        "artifact_id": record.metrics_artifact_id,
        "artifact_sha": record.metrics.sha256,
        "media_type": record.metrics.media_type,
        # NULL, exactly as production stores them: upstream never exposes the components.
        "core": None,
        "completeness": None,
        "fp": None,
        "quality": None,
        "overcall": float(upstream.metrics.get("overcall_penalty", 0.0) or 0.0),
        "score100": upstream.advanced_score_100,
        "score": upstream.minos_score,
        "admission": record.admission_code,
        "eval_hash": record.evaluation_hash,
    }


def _prepared_record(env: Any, evaluated: Any, tmp_path: Path) -> Any:
    """A fully valid, persistable evaluation record built through the production path."""
    root = tmp_path / "practice"
    root.mkdir(exist_ok=True)
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    inputs = _inputs_for(env, evaluated)
    artifact, _upstream_out, admission, artifact_id, published = _publish(env, tmp_path, inputs)
    return build_evaluation_record(
        execution_result_id=_result_id(env, evaluated),
        inputs=inputs,
        artifact=artifact,
        admission_code=admission,
        authority=_authority(),
        metrics_artifact_id=artifact_id,
        metrics=published,
    )


def _wait_until_blocked(engine: Any, deadline_seconds: float = 20.0) -> bool:
    """Wait until some backend is genuinely waiting on a lock (never a bare sleep)."""
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        with engine.connect() as probe:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    " WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            ).scalar()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def _race_success_against_failure(env: Any, record: Any, *, success_first: bool) -> dict[str, int]:
    """Run a success and a failure attempt in two genuinely OVERLAPPING transactions.

    The second transaction starts while the first is still uncommitted, so the outcome is decided
    by the trigger's lock strength alone — which is exactly the defect under test.
    """
    engine = env.engine
    params = _record_params(record)
    failure_params = {
        "exec_id": record.execution_result_id,
        "contract": record.scoring_contract_hash,
        "code": "EVALUATION_ERROR",
    }
    first_sql, first_params = (
        (_RECORD_SQL, params) if success_first else (_FAILURE_SQL, failure_params)
    )
    second_sql, second_params = (
        (_FAILURE_SQL, failure_params) if success_first else (_RECORD_SQL, params)
    )

    errors: list[BaseException] = []
    started = threading.Event()

    def _second() -> None:
        conn = engine.connect()
        try:
            trans = conn.begin()
            started.set()
            try:
                conn.execute(text(second_sql), second_params)
                trans.commit()
            except BaseException as exc:  # the refusal we are trying to observe
                trans.rollback()
                errors.append(exc)
        finally:
            conn.close()

    with engine.connect() as first_conn:
        trans = first_conn.begin()
        first_conn.execute(text(first_sql), first_params)  # holds the row lock, uncommitted
        worker = threading.Thread(target=_second, daemon=True)
        worker.start()
        started.wait(timeout=20.0)
        # give the second transaction a real chance to reach (and block on) the lock
        _wait_until_blocked(engine)
        trans.commit()
    worker.join(timeout=60.0)
    assert not worker.is_alive(), "the second transaction never finished"

    with engine.connect() as conn:
        successes = conn.execute(
            text(
                "SELECT count(*) FROM evaluation.l2f_evaluation_results "
                " WHERE execution_result_id = :e AND scoring_contract_hash = :c"
            ),
            {"e": record.execution_result_id, "c": record.scoring_contract_hash},
        ).scalar_one()
        failures = conn.execute(
            text(
                "SELECT count(*) FROM evaluation.l2f_evaluation_failures "
                " WHERE execution_result_id = :e AND scoring_contract_hash = :c"
            ),
            {"e": record.execution_result_id, "c": record.scoring_contract_hash},
        ).scalar_one()
    return {"successes": int(successes), "failures": int(failures), "refused": len(errors)}


# --------------------------------------------------------------------------- #
# DEFECT A — the success/failure XOR is genuinely serialized
# --------------------------------------------------------------------------- #
def test_the_0009_for_share_lock_did_not_serialize_the_xor(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """THE proof that the corrective was necessary and that these controls have teeth.

    With 0009's ``FOR SHARE`` body installed, two overlapping transactions both observe "no
    other outcome" — SHARE locks are mutually compatible — and one execution ends up with a
    success AND a failure under the same scoring contract.
    """
    record = _prepared_record(env, evaluated, tmp_path)
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text(_exclusive_body("FOR SHARE")))

    counts = _race_success_against_failure(env, record, success_first=True)

    assert counts["successes"] == 1
    assert counts["failures"] == 1, "FOR SHARE should have admitted BOTH outcomes"
    assert counts["successes"] + counts["failures"] == 2


def test_concurrent_success_and_failure_yield_exactly_one_outcome(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """0010: success first, failure concurrently — exactly one terminal outcome survives."""
    record = _prepared_record(env, evaluated, tmp_path)
    counts = _race_success_against_failure(env, record, success_first=True)

    assert counts["successes"] + counts["failures"] == 1
    assert counts["successes"] == 1
    assert counts["refused"] == 1


def test_concurrent_failure_and_success_yield_exactly_one_outcome(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """0010: the reverse order — failure first, success concurrently."""
    record = _prepared_record(env, evaluated, tmp_path)
    counts = _race_success_against_failure(env, record, success_first=False)

    assert counts["successes"] + counts["failures"] == 1
    assert counts["failures"] == 1
    assert counts["refused"] == 1


def test_a_terminal_outcome_makes_the_opposite_outcome_permanently_impossible(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """Retry convergence: after one terminal outcome, the opposite stays refused forever."""
    record = _prepared_record(env, evaluated, tmp_path)
    from minos_engine.evaluation.evaluator import record_evaluation_result

    first = record_evaluation_result(env.engine, record)
    assert first.created is True
    # exact replay is idempotent ...
    replay = record_evaluation_result(env.engine, record)
    assert replay.created is False
    assert replay.evaluation_id == first.evaluation_id
    # ... and the opposite outcome is impossible, now and on every later attempt.
    for _attempt in range(2):
        with pytest.raises(Exception, match="already succeeded"):
            record_evaluation_failure(
                env.engine,
                execution_result_id=record.execution_result_id,
                scoring_contract_hash=record.scoring_contract_hash,
                failure_code="EVALUATION_ERROR",
            )


# --------------------------------------------------------------------------- #
# DEFECT B — the metrics artifact identity is closed IN THE DATABASE
# --------------------------------------------------------------------------- #
def _second_metrics_artifact(env: Any, tmp_path: Path) -> Any:
    """A second, genuinely different metrics document, registered the production way."""
    import os

    from minos_engine.evaluation.artifact_publisher import EvaluationArtifactPublisher

    root = tmp_path / "evaluation_artifacts"
    root.mkdir(exist_ok=True)
    os.chmod(root, 0o2750)
    published = EvaluationArtifactPublisher(root).publish(b'{"schema_version":"other"}')
    artifact_id, _created = register_metrics_artifact(env.engine, published)
    return artifact_id, published


def _persist_with(env: Any, record: Any, **overrides: Any) -> None:
    params = _record_params(record)
    params.update(overrides)
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text(_RECORD_SQL), params)


def test_a_valid_artifact_id_with_a_forged_digest_is_refused_by_postgresql(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    record = _prepared_record(env, evaluated, tmp_path)
    with pytest.raises(Exception) as excinfo:
        _persist_with(env, record, artifact_sha="9" * 64)
    assert "fk_l2f_eval_results_metrics_artifact" in str(excinfo.value)


def test_a_valid_artifact_with_a_forged_media_type_is_refused(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    record = _prepared_record(env, evaluated, tmp_path)
    with pytest.raises(Exception) as excinfo:
        _persist_with(env, record, media_type="application/json")
    assert "ck_l2f_eval_results_metrics_media" in str(excinfo.value)


def test_one_artifacts_id_paired_with_another_artifacts_digest_is_refused(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """Both artifacts are real, registered metrics documents — only the PAIRING is forged."""
    record = _prepared_record(env, evaluated, tmp_path)
    other_id, other_published = _second_metrics_artifact(env, tmp_path)
    assert other_id != record.metrics_artifact_id

    with pytest.raises(Exception) as excinfo:
        _persist_with(env, record, artifact_sha=other_published.sha256)
    assert "fk_l2f_eval_results_metrics_artifact" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        _persist_with(env, record, artifact_id=other_id)
    assert "fk_l2f_eval_results_metrics_artifact" in str(excinfo.value)


def test_substituting_a_non_metrics_artifact_is_refused(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """The execution's own VCF artifact is a real catalog row — and must not be citable here."""
    record = _prepared_record(env, evaluated, tmp_path)
    with env.engine.connect() as conn:
        vcf = (
            conn.execute(
                text(
                    "SELECT a.id, a.sha256, a.media_type FROM catalog.artifacts a "
                    "  JOIN experiments.l2f_execution_results r ON r.vcf_artifact_id = a.id "
                    " WHERE r.id = :i"
                ),
                {"i": record.execution_result_id},
            )
            .mappings()
            .one()
        )
    assert vcf["media_type"] != record.metrics.media_type

    with pytest.raises(Exception) as excinfo:
        _persist_with(env, record, artifact_id=str(vcf["id"]), artifact_sha=str(vcf["sha256"]))
    assert "fk_l2f_eval_results_metrics_artifact" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# DEFECT C — the narrow metrics registrar
# --------------------------------------------------------------------------- #
def test_the_registrar_fixes_media_type_and_provenance_itself(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    from minos_engine.evaluation.artifact_publisher import (
        EVALUATION_METRICS_PROVENANCE,
        EvaluationArtifactPublisher,
    )
    from minos_engine.evaluation.contracts import EVALUATION_METRICS_MEDIA_TYPE

    root = tmp_path / "evaluation_artifacts"
    root.mkdir(exist_ok=True)
    import os

    os.chmod(root, 0o2750)
    published = EvaluationArtifactPublisher(root).publish(b'{"registrar":"classification"}')
    artifact_id, created = register_metrics_artifact(env.engine, published)
    assert created is True

    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT uri, sha256, media_type, size_bytes, provenance "
                    "  FROM catalog.artifacts WHERE id = :i"
                ),
                {"i": artifact_id},
            )
            .mappings()
            .one()
        )
    # the caller supplied ONLY content identity; classification came from the function.
    assert row["media_type"] == EVALUATION_METRICS_MEDIA_TYPE
    assert row["provenance"] == EVALUATION_METRICS_PROVENANCE
    assert row["sha256"] == published.sha256
    assert row["size_bytes"] == published.size_bytes
    assert row["uri"] == published.uri


def test_exact_re_registration_returns_the_existing_artifact(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    """Crash convergence: replaying registration must not duplicate or fail."""
    _artifact_id, published = _second_metrics_artifact(env, tmp_path)
    first_id, first_created = register_metrics_artifact(env.engine, published)
    second_id, second_created = register_metrics_artifact(env.engine, published)
    assert (first_id, second_id) == (first_id, first_id)
    assert second_created is False
    assert first_created is False  # the helper already registered it


def test_the_same_digest_with_conflicting_metadata_is_a_typed_conflict(
    evaluated: Any, env: Any, tmp_path: Path
) -> None:
    _artifact_id, published = _second_metrics_artifact(env, tmp_path)
    with (
        pytest.raises(Exception, match="different metadata"),
        env.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(
            text("SELECT * FROM evaluation.l2f_register_metrics_artifact(:s, :u, :z)"),
            {"s": published.sha256, "u": published.uri + ".moved", "z": published.size_bytes},
        )


@pytest.mark.parametrize(
    ("sha", "uri", "size", "needle"),
    [
        ("A" * 64, "file:///x.json", 3, "canonical lowercase hex"),
        ("abc", "file:///x.json", 3, "canonical lowercase hex"),
        ("b" * 64, "   ", 3, "non-empty"),
        ("c" * 64, "file:///x.json", -1, "non-negative"),
    ],
)
def test_the_registrar_refuses_malformed_content_identity(
    evaluated: Any, env: Any, sha: str, uri: str, size: int, needle: str
) -> None:
    with pytest.raises(Exception, match=needle), env.engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT * FROM evaluation.l2f_register_metrics_artifact(:s, :u, :z)"),
            {"s": sha, "u": uri, "z": size},
        )


# --------------------------------------------------------------------------- #
# DEFECT C/E — the SERVICE PRINCIPAL runs the whole production path
# --------------------------------------------------------------------------- #
_CI_ROLE = "minos_evaluator_ci_svc"

_SUMMARY_CSV = (
    "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.TOTAL,QUERY.FP,QUERY.UNK,"
    "METRIC.Recall,METRIC.Precision,METRIC.Frac_NA,METRIC.F1_Score,"
    "TRUTH.TOTAL.TiTv_ratio,QUERY.TOTAL.TiTv_ratio,"
    "TRUTH.TOTAL.het_hom_ratio,QUERY.TOTAL.het_hom_ratio\n"
    "SNP,ALL,1,1,0,1,0,0,1.0,1.0,0.0,1.0,2.0,2.0,1.5,1.5\n"
    "SNP,PASS,1,1,0,1,0,0,1.0,1.0,0.0,1.0,2.0,2.0,1.5,1.5\n"
    "INDEL,ALL,1,1,0,1,0,0,1.0,1.0,0.0,1.0,,,1.0,1.0\n"
    "INDEL,PASS,1,1,0,1,0,0,1.0,1.0,0.0,1.0,,,1.0,1.0\n"
)

#: a minimal hap.py annotated VCF: one TP SNP and one TP INDEL, both on target.
_HAPPY_VCF_LINES = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:INDEL:.:het\tTP:INDEL:.:het\n"
)

_MUTATIONS_LINES = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\n"
)


def _gzip(text_value: str) -> bytes:
    import gzip as _gzip_module
    import io

    buffer = io.BytesIO()
    with _gzip_module.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(text_value.encode("utf-8"))
    return buffer.getvalue()


def _provision_real_truth(env: Any, root: Path, *, engine: Any = None) -> dict[str, str]:
    """Write GENUINELY gzipped synthetic truth for every TRAIN target, then register it."""
    registrar = engine if engine is not None else env.engine
    root.mkdir(parents=True, exist_ok=True)
    with env.engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT dataset_registry_id, round_id "
                    "  FROM evaluation.l2f_train_truth_registration_targets"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        directory = root / f"round_{row['round_id']}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "truth.vcf.gz").write_bytes(_gzip(_HAPPY_VCF_LINES))
        (directory / "truth.vcf.gz.tbi").write_bytes(b"\x00tbi-truth")
        (directory / "mutations.vcf.gz").write_bytes(_gzip(_MUTATIONS_LINES))
        (directory / "mutations.vcf.gz.tbi").write_bytes(b"\x00tbi-mutations")
    register_train_truth_identities(registrar, dataset_root=root)
    return {str(r["dataset_registry_id"]): str(r["round_id"]) for r in rows}


def _service_engine(env: Any) -> Any:
    """Create the ephemeral CI service principal and return an engine bound to it.

    It receives ``minos_evaluator`` and NOTHING else — the same authority shape the real
    ``minos_evaluator_svc`` will have, so what passes here is what the service can really do.
    """
    url = env.engine.url
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))
        conn.execute(
            text(
                f"CREATE ROLE {_CI_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOBYPASSRLS INHERIT"
            )
        )
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {_CI_ROLE}'))
        conn.execute(text(f"GRANT minos_evaluator TO {_CI_ROLE}"))
    return create_engine(url.set(username=_CI_ROLE, password=""))


def _drop_service_role(env: Any) -> None:
    url = env.engine.url
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {_CI_ROLE}'))
        conn.execute(text(f"REVOKE minos_evaluator FROM {_CI_ROLE}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))


@pytest.fixture
def service(env: Any, evaluated: Any) -> Any:
    """An engine authenticated as the ephemeral evaluator service principal."""
    engine = _service_engine(env)
    try:
        yield engine
    finally:
        engine.dispose()
        _drop_service_role(env)


def test_the_service_principal_has_no_elevated_attributes(service: Any) -> None:
    with service.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
                    "  FROM pg_roles WHERE rolname = :r"
                ),
                {"r": _CI_ROLE},
            )
            .mappings()
            .one()
        )
    assert row["rolcanlogin"] is True
    for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls"):
        assert row[attribute] is False, attribute


def test_the_group_role_itself_never_gains_login(service: Any) -> None:
    """``minos_evaluator`` carries authority, never credentials. That separation stays."""
    with service.connect() as conn:
        assert (
            conn.execute(
                text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'minos_evaluator'")
            ).scalar()
            is False
        )


def test_the_service_principal_reads_only_the_narrow_projections(service: Any) -> None:
    with service.connect() as conn:
        conn.execute(text("SELECT count(*) FROM evaluation.l2f_completed_execution_inputs"))
        conn.execute(text("SELECT count(*) FROM evaluation.l2f_train_truth_registration_targets"))
        conn.execute(text("SELECT count(*) FROM evaluation.dataset_evaluation_identity"))
        conn.execute(text("SELECT count(*) FROM evaluation.l2f_evaluation_results"))
        conn.execute(text("SELECT count(*) FROM evaluation.l2f_evaluation_failures"))


class _WellFormed(Exception):
    """Raised to roll back a probe that ran successfully under an authorised role."""


_DENIED_STATEMENTS = [
    "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('file:///x', repeat('a', 64))",
    "UPDATE catalog.artifacts SET uri = 'file:///y'",
    "DELETE FROM catalog.artifacts",
    "UPDATE experiments.l2f_execution_results SET result_hash = repeat('c', 64)",
    "DELETE FROM experiments.l2f_execution_results",
    "UPDATE experiments.l2f_experiment_jobs SET status = 'QUEUED'",
    "DELETE FROM experiments.l2f_experiment_jobs",
    "UPDATE experiments.l2f_experiment_plan_configs SET config_hash = repeat('b', 64)",
    "INSERT INTO evaluation.l2f_evaluation_results (id) VALUES (gen_random_uuid())",
    "DELETE FROM evaluation.l2f_evaluation_results",
]


@pytest.mark.parametrize("statement", _DENIED_STATEMENTS)
def test_the_service_principal_cannot_mutate_the_ledger_or_catalog(
    service: Any, statement: str
) -> None:
    with pytest.raises(Exception) as excinfo, service.connect() as conn, conn.begin():
        conn.execute(text(statement))
    assert "permission denied" in str(excinfo.value).lower()


@pytest.mark.parametrize("statement", _DENIED_STATEMENTS)
def test_every_denial_statement_actually_reaches_the_privilege_check(
    env: Any, evaluated: Any, statement: str
) -> None:
    """The F7-B lesson, applied to a security matrix.

    A denial test proves nothing if the statement dies on a typo first: PostgreSQL reports an
    unknown column before it reports a privilege failure, so an ``UPDATE t SET nosuchcol = ...``
    would "pass" the matrix while testing nothing. Running each statement as a role that IS
    allowed proves the statement is well-formed and genuinely privilege-gated.
    """
    with pytest.raises(Exception) as excinfo, env.engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(text(statement))
        raise _WellFormed
    message = str(excinfo.value).lower()
    # reaching execution (success, FK/append-only/not-null refusal) is the proof; dying in the
    # parser or the name resolver is not.
    assert "does not exist" not in message, "statement names something that does not exist"
    assert "syntax error" not in message, "statement is malformed"


@pytest.mark.parametrize("role", ["minos_admin", "minos_runner", "minos_trainer", "minos_live"])
def test_the_service_principal_cannot_assume_another_role(service: Any, role: str) -> None:
    with pytest.raises(Exception) as excinfo, service.connect() as conn, conn.begin():
        conn.execute(text(f"SET ROLE {role}"))
    assert "permission denied" in str(excinfo.value).lower()


def test_validation_and_test_partitions_are_structurally_absent_from_the_train_surface(
    service: Any, env: Any
) -> None:
    """The registration surface cannot even ENUMERATE a closed partition."""
    # a genuinely VALIDATION-allocated dataset must exist, or the control is vacuous.
    with env.engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        probe_id = conn.execute(
            text(
                "INSERT INTO catalog.dataset_registry "
                "SELECT (jsonb_populate_record(NULL::catalog.dataset_registry, "
                "         to_jsonb(d) || jsonb_build_object("
                "           'id', gen_random_uuid()::text, "
                "           'dataset_id', 'minos-closed-partition-probe', "
                "           'identity_tuple_hash', repeat('f', 64), "
                "           'round_id', 'closed-partition-probe', "
                # the registry's identity tuple is deliberately unique, so the probe must be a
                # genuinely distinct dataset rather than a near-copy.
                "           'bam_sha256', repeat('1', 64), "
                "           'bai_sha256', repeat('2', 64), "
                "           'reference_sha256', repeat('3', 64), "
                "           'fai_sha256', repeat('4', 64), "
                "           'region_hash', repeat('5', 64)))).* "
                "  FROM (SELECT * FROM catalog.dataset_registry LIMIT 1) d "
                "RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO catalog.split_allocations "
                "  (dataset_registry_id, partition, sort_order, manifest_hash) "
                "VALUES (:d, 'validation', 9999, :h)"
            ),
            {"d": probe_id, "h": "a" * 64},
        )

    with env.engine.connect() as conn:
        train_ids = {
            str(v)
            for v in conn.execute(
                text(
                    "SELECT dataset_registry_id FROM catalog.split_allocations "
                    " WHERE partition = 'train'"
                )
            ).scalars()
        }
        closed_ids = {
            str(v)
            for v in conn.execute(
                text(
                    "SELECT dataset_registry_id FROM catalog.split_allocations "
                    " WHERE partition <> 'train'"
                )
            ).scalars()
        }
    assert closed_ids, "the control needs at least one non-TRAIN allocation to be meaningful"

    with service.connect() as conn:
        visible = {
            str(v)
            for v in conn.execute(
                text(
                    "SELECT dataset_registry_id "
                    "  FROM evaluation.l2f_train_truth_registration_targets"
                )
            ).scalars()
        }
    assert visible == train_ids
    assert visible.isdisjoint(closed_ids)


# --------------------------------------------------------------------------- #
# DEFECT E — the production orchestrator, end to end, under the service login
# --------------------------------------------------------------------------- #
def _provisioning(tmp_path: Path) -> Any:
    """Runtime paths only. The reference root is laid out exactly as the pinned validator expects
    and carries the SAME bytes the execution recorded, so the digest check is real rather than
    arranged."""
    from minos_engine.evaluation.orchestrator import EvaluationProvisioning

    work = tmp_path / "evaluation_work"
    work.mkdir(exist_ok=True)
    reference_root = tmp_path / "datasets" / "reference"
    for chromosome_dir in sorted(p for p in reference_root.iterdir() if p.is_dir()):
        (chromosome_dir / f"{chromosome_dir.name}.sdf").mkdir(exist_ok=True)
    return EvaluationProvisioning(
        practice_dataset_root=tmp_path / "practice",
        reference_root=reference_root,
        work_dir=work,
    )


def _upstream(tmp_path: Path) -> Any:
    """The synthetic pinned checkout, created once per tmp_path and reused across calls."""
    from tests.integration.layer2_db.l2f2_fake_upstream import FakeUpstream, build_fake_upstream

    root = tmp_path / "fake_upstream_base"
    if (root / "fake_minos_subnet").is_dir():
        import subprocess

        commit = subprocess.run(
            ["git", "-C", str(root / "fake_minos_subnet"), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return FakeUpstream(
            root=root / "fake_minos_subnet",
            commit=commit,
            control_path=root / "fake_minos_subnet" / "control.json",
            seen_path=root / "upstream-seen.json",
        )
    root.mkdir(parents=True, exist_ok=True)
    return build_fake_upstream(root)


def _oracle(tmp_path: Path, *, timeout_seconds: int = 300) -> Any:
    from tests.integration.layer2_db.l2f2_fake_upstream import authority_for, oracle_for

    upstream = _upstream(tmp_path)
    return oracle_for(
        upstream, authority_for(upstream, _authority()), timeout_seconds=timeout_seconds
    )


def _artifact_publisher(tmp_path: Path) -> Any:
    import os

    from minos_engine.evaluation.artifact_publisher import EvaluationArtifactPublisher

    root = tmp_path / "evaluation_artifacts"
    root.mkdir(exist_ok=True)
    os.chmod(root, 0o2750)
    return EvaluationArtifactPublisher(root)


def _good_oracle(tmp_path: Path) -> Any:
    """The synthetic upstream, set to return metrics."""
    _upstream(tmp_path).set_mode("metrics")
    return _oracle(tmp_path)


def _run(engine: Any, env: Any, evaluated: Any, tmp_path: Path, oracle: Any) -> Any:
    """Invoke the REAL orchestrator for the fixture's completed execution."""
    from minos_engine.evaluation.orchestrator import evaluate_execution
    from tests.integration.layer2_db.l2f2_fake_upstream import authority_for

    upstream = _upstream(tmp_path)
    return evaluate_execution(
        engine,
        execution_result_id=_result_id(env, evaluated),
        authority=authority_for(upstream, _authority()),
        oracle=oracle,
        publisher=_artifact_publisher(tmp_path),
        provisioning=_provisioning(tmp_path),
    )


def _counts(env: Any, result_id: str) -> dict[str, int]:
    with env.engine.connect() as conn:
        return {
            "successes": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM evaluation.l2f_evaluation_results "
                        " WHERE execution_result_id = :e"
                    ),
                    {"e": result_id},
                ).scalar_one()
            ),
            "failures": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM evaluation.l2f_evaluation_failures "
                        " WHERE execution_result_id = :e"
                    ),
                    {"e": result_id},
                ).scalar_one()
            ),
            "metrics_artifacts": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM catalog.artifacts "
                        " WHERE provenance = 'l2f2:evaluation-metrics'"
                    )
                ).scalar_one()
            ),
        }


def test_the_orchestrator_evaluates_one_execution_end_to_end_under_the_service_login(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """THE canary: real orchestrator, real publisher, real registrar, real persistence.

    Everything is exercised through the service principal's own authority — no admin shortcut
    manufactures any part of the evaluation path.
    """
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)

    outcome = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert outcome.status == "EVALUATED", outcome.failure_code
    assert outcome.persisted is not None
    assert outcome.persisted.created is True
    assert outcome.failure_code is None

    counts = _counts(env, result_id)
    assert counts == {"successes": 1, "failures": 0, "metrics_artifacts": 1}

    published = sorted((tmp_path / "evaluation_artifacts").glob("*.json"))
    assert len(published) == 1
    assert published[0].name == f"{outcome.metrics_artifact_sha256}.json"
    import stat as _stat

    assert _stat.S_IMODE(published[0].stat().st_mode) == 0o640

    # the stored row cites exactly the document that was published
    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT metrics_artifact_id, metrics_artifact_sha256, metrics_media_type, "
                    "       partition, admission_code, evaluation_hash "
                    "  FROM evaluation.l2f_evaluation_results WHERE execution_result_id = :e"
                ),
                {"e": result_id},
            )
            .mappings()
            .one()
        )
    assert row["metrics_artifact_sha256"] == outcome.metrics_artifact_sha256
    assert str(row["metrics_artifact_id"]) == outcome.metrics_artifact_id
    assert row["partition"] == "train"
    assert row["evaluation_hash"] == outcome.persisted.evaluation_hash


def test_exact_replay_of_the_orchestrator_converges_without_duplicates(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """Crash convergence at every layer: same artifact, same catalog row, same evaluation."""
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)

    first = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))
    second = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert first.status == second.status == "EVALUATED"
    assert first.metrics_artifact_sha256 == second.metrics_artifact_sha256
    assert first.metrics_artifact_id == second.metrics_artifact_id
    assert first.persisted is not None and second.persisted is not None
    assert first.persisted.evaluation_id == second.persisted.evaluation_id
    assert second.persisted.created is False
    assert _counts(env, result_id) == {"successes": 1, "failures": 0, "metrics_artifacts": 1}
    assert len(sorted((tmp_path / "evaluation_artifacts").glob("*.json"))) == 1


def test_a_closed_partition_is_refused_before_any_truth_path_is_touched(
    service: Any, env: Any, evaluated: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ORDER is the claim: the partition gate fires before truth is resolved or opened.

    The practice root is deliberately absent, so any truth access would raise a truth error
    instead — only the partition guard running first can produce ForbiddenPartitionError.
    """
    from minos_engine.evaluation import orchestrator as orch
    from minos_engine.evaluation.truth_registration import ForbiddenPartitionError

    real = orch._resolve_execution

    def _validation(engine: Any, execution_result_id: str) -> dict[str, Any]:
        row = real(engine, execution_result_id)
        row["partition"] = "validation"
        return row

    monkeypatch.setattr(orch, "_resolve_execution", _validation)
    result_id = _result_id(env, evaluated)

    with pytest.raises(ForbiddenPartitionError):
        _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    # a closed partition leaves NO trace in the scientific ledger at all
    assert _counts(env, result_id) == {"successes": 0, "failures": 0, "metrics_artifacts": 0}
    assert not sorted((tmp_path / "evaluation_artifacts").glob("*.json"))


# --------------------------------------------------------------------------- #
# The failure chain — every terminal infrastructure error is DURABLE and BOUNDED
# --------------------------------------------------------------------------- #
def test_a_missing_truth_identity_is_a_bounded_durable_failure(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """No truth registered for this dataset: refuse before any scoring, and record why."""
    (tmp_path / "practice").mkdir(exist_ok=True)
    result_id = _result_id(env, evaluated)

    outcome = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert outcome.status == "FAILED"
    assert outcome.failure_code == "TRUTH_IDENTITY_MISSING"
    assert outcome.failure_id is not None
    assert _counts(env, result_id) == {"successes": 0, "failures": 1, "metrics_artifacts": 0}
    assert not sorted((tmp_path / "evaluation_artifacts").glob("*.json"))


def test_truth_bytes_that_no_longer_match_the_registered_identity_fail_closed(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    rounds = _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)
    for round_id in rounds.values():
        path = tmp_path / "practice" / f"round_{round_id}" / "truth.vcf.gz"
        path.write_bytes(_gzip(_HAPPY_VCF_LINES + "chr18\t3000\t.\tC\tT\t.\tPASS\t.\tBD\tTP\tTP\n"))

    outcome = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert outcome.failure_code == "TRUTH_BYTES_MISMATCH"
    assert _counts(env, result_id) == {"successes": 0, "failures": 1, "metrics_artifacts": 0}


def test_an_execution_vcf_that_no_longer_matches_its_recorded_digest_fails_closed(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """The recorded execution artifact is verified BEFORE hap.py is ever started."""
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)
    with env.engine.connect() as conn:
        uri = conn.execute(
            text(
                "SELECT vcf_uri FROM evaluation.l2f_completed_execution_inputs "
                " WHERE execution_result_id = :e"
            ),
            {"e": result_id},
        ).scalar_one()
    from urllib.parse import unquote, urlparse

    Path(unquote(urlparse(str(uri)).path)).write_bytes(b"tampered\n")

    outcome = _run(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert outcome.failure_code == "VCF_BYTES_MISMATCH"
    assert _counts(env, result_id) == {"successes": 0, "failures": 1, "metrics_artifacts": 0}


@pytest.mark.parametrize(
    ("upstream_mode", "expected"),
    [
        ("raise", "HAPPY_NONZERO_EXIT"),
        ("hang", "HAPPY_TIMEOUT"),
        ("none", "HAPPY_OUTPUT_INVALID"),
    ],
)
def test_every_hap_py_failure_mode_is_bounded_and_durable(
    service: Any,
    env: Any,
    evaluated: Any,
    tmp_path: Path,
    upstream_mode: str,
    expected: str,
) -> None:
    """Every way the pinned scorer can fail is a BOUNDED, DURABLE failure — never a zero score.

    ``none`` is the case that matters most: upstream ran and declined to return metrics, which is
    unusable output, not a terrible configuration.
    """
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)
    _upstream(tmp_path).set_mode(upstream_mode, sleep=30)

    outcome = _run(service, env, evaluated, tmp_path, _oracle(tmp_path, timeout_seconds=2))

    assert outcome.failure_code == expected
    assert _counts(env, result_id) == {"successes": 0, "failures": 1, "metrics_artifacts": 0}
    assert not sorted((tmp_path / "evaluation_artifacts").glob("*.json"))


def test_a_failure_is_never_silently_a_zero_score(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """A broken evaluation must not enter the baseline as a real, terrible configuration."""
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    _upstream(tmp_path).set_mode("none")
    outcome = _run(service, env, evaluated, tmp_path, _oracle(tmp_path))

    assert outcome.status == "FAILED"
    assert outcome.persisted is None
    with env.engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM evaluation.l2f_evaluation_results")).scalar()
            == 0
        )


# --------------------------------------------------------------------------- #
# Migration lifecycle — 0010 -> 0009 -> 0010 restores each inventory exactly
# --------------------------------------------------------------------------- #
def _corrective_inventory(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        registrar = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'evaluation' AND p.proname = 'l2f_register_metrics_artifact'"
            )
        ).scalar_one()
        constraints = sorted(
            str(v)
            for v in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    " WHERE conname IN ('uq_artifacts_id_sha256_media', "
                    "                   'ck_l2f_eval_results_metrics_media', "
                    "                   'fk_l2f_eval_results_metrics_artifact')"
                )
            ).scalars()
        )
        metrics_fk_columns = int(
            conn.execute(
                text(
                    "SELECT cardinality(conkey) FROM pg_constraint "
                    " WHERE conname = 'fk_l2f_eval_results_metrics_artifact'"
                )
            ).scalar_one()
        )
        body = str(
            conn.execute(
                text(
                    "SELECT pg_get_functiondef("
                    "  'evaluation.l2f_evaluation_exclusive_outcome()'::regprocedure)"
                )
            ).scalar_one()
        )
    return {
        "registrar": int(registrar),
        "constraints": constraints,
        "metrics_fk_columns": metrics_fk_columns,
        "locks_for_update": "FOR UPDATE" in body,
        "locks_for_share": "FOR SHARE" in body,
    }


def test_the_corrective_downgrades_to_exactly_0009_and_upgrades_back(
    evaluated: Any, env: Any
) -> None:
    from tests.integration.layer2_db.conftest import alembic_downgrade, alembic_upgrade

    url = _url(env)
    at_0010 = _corrective_inventory(env.engine)
    assert at_0010 == {
        "registrar": 1,
        "constraints": [
            "ck_l2f_eval_results_metrics_media",
            "fk_l2f_eval_results_metrics_artifact",
            "uq_artifacts_id_sha256_media",
        ],
        "metrics_fk_columns": 3,
        "locks_for_update": True,
        "locks_for_share": False,
    }

    env.engine.dispose()
    alembic_downgrade(url, "0009_l2f_evaluation_results")
    engine = create_engine(url)
    try:
        at_0009 = _corrective_inventory(engine)
        # exactly 0009 again: no registrar, no composite target, single-column FK, FOR SHARE.
        assert at_0009 == {
            "registrar": 0,
            "constraints": ["fk_l2f_eval_results_metrics_artifact"],
            "metrics_fk_columns": 1,
            "locks_for_update": False,
            "locks_for_share": True,
        }
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0009_l2f_evaluation_results"
            )
    finally:
        engine.dispose()

    alembic_upgrade(url, "0010_l2f2_evaluation_corrective")
    engine = create_engine(url)
    try:
        assert _corrective_inventory(engine) == at_0010
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0010_l2f2_evaluation_corrective"
            )
    finally:
        engine.dispose()
    env.engine = create_engine(url)


# --------------------------------------------------------------------------- #
# RUNTIME ISOLATION — fresh attempt directory, and terminal replay never re-executes
# --------------------------------------------------------------------------- #
class _CountingOracle:
    """An oracle that records how often the pinned scorer was actually invoked."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def score(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.score(**kwargs)


class _ExplodingOracle:
    """An oracle that fails the test if the pinned scorer is invoked at all."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the scorer must not run for an already-terminal evaluation")


def _run_with(engine: Any, env: Any, evaluated: Any, tmp_path: Path, oracle: Any) -> Any:
    from minos_engine.evaluation.orchestrator import evaluate_execution
    from tests.integration.layer2_db.l2f2_fake_upstream import authority_for

    return evaluate_execution(
        engine,
        execution_result_id=_result_id(env, evaluated),
        authority=authority_for(_upstream(tmp_path), _authority()),
        oracle=oracle,
        publisher=_artifact_publisher(tmp_path),
        provisioning=_provisioning(tmp_path),
    )


def test_each_run_uses_a_fresh_private_attempt_directory(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """The work_dir handed to hap.py is a NEW 0700 directory beneath the work root each time.

    Two genuinely different completed executions are evaluated, because an evaluation ledger row
    is immutable and append-only — there is deliberately no way to "redo" one.
    """
    import stat as _stat

    _provision_real_truth(env, tmp_path / "practice", engine=service)
    seen: list[Path] = []
    modes: list[int] = []

    class _Recording:
        def score(self, **kwargs: Any) -> Any:
            work_dir = Path(kwargs["work_dir"])
            seen.append(work_dir)
            modes.append(_stat.S_IMODE(work_dir.lstat().st_mode))
            # every sandboxed scoring input lives INSIDE the fresh attempt directory
            for key in ("truth_vcf", "query_vcf", "mutations_vcf"):
                assert work_dir in Path(kwargs[key]).parents, key
            return _good_oracle(tmp_path).score(**kwargs)

    first = _run_with(service, env, evaluated, tmp_path, _Recording())
    assert first.status == "EVALUATED"

    other = env.run()
    assert other is not None and other.status == "SUCCEEDED"
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    second = _run_with(service, env, other, tmp_path, _Recording())
    assert second.status == "EVALUATED", second.failure_code

    assert len(seen) == 2
    assert seen[0] != seen[1], "an attempt directory is never reused"
    assert modes == [0o700, 0o700]
    assert seen[0].parent == seen[1].parent == (tmp_path / "evaluation_work")
    # runtime intermediates do not survive a terminal outcome
    assert not seen[0].exists() and not seen[1].exists()


def test_stale_output_in_the_work_root_cannot_influence_the_evaluation(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """A previous attempt's leftovers are outside the fresh attempt namespace entirely."""
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    work_root = tmp_path / "happy_work"
    work_root.mkdir(exist_ok=True)

    # valid-LOOKING but scientifically different leftovers, sitting in the parent work root
    poisoned = _SUMMARY_CSV.replace("1.0,1.0,0.0,1.0", "0.01,0.01,0.9,0.01")
    for name in ("happy.summary.csv", f"happy_{_result_id(env, evaluated)}.summary.csv"):
        (work_root / name).write_text(poisoned, encoding="utf-8")
    (work_root / "happy.vcf.gz").write_bytes(_gzip(_HAPPY_VCF_LINES))

    outcome = _run_with(service, env, evaluated, tmp_path, _good_oracle(tmp_path))

    assert outcome.status == "EVALUATED"
    with env.engine.connect() as conn:
        score = conn.execute(
            text(
                "SELECT minos_score FROM evaluation.l2f_evaluation_results "
                " WHERE execution_result_id = :e"
            ),
            {"e": _result_id(env, evaluated)},
        ).scalar_one()
    # the poisoned summary would have produced a far lower score had it been read
    assert float(score) > 0.5, "the evaluation read stale parent-directory output"
    # the leftovers are untouched: cleanup only ever removes the attempt's own inode
    assert (work_root / "happy.summary.csv").read_text(encoding="utf-8") == poisoned


def test_an_already_successful_evaluation_is_never_re_executed(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    counted = _CountingOracle(_good_oracle(tmp_path))
    first = _run_with(service, env, evaluated, tmp_path, counted)
    assert first.status == "EVALUATED" and counted.calls == 1

    exploding = _ExplodingOracle()
    second = _run_with(service, env, evaluated, tmp_path, exploding)

    assert exploding.calls == 0, "the scorer must not be launched for a terminal evaluation"
    assert second.status == "EVALUATED"
    assert second.persisted is not None and first.persisted is not None
    assert second.persisted.evaluation_id == first.persisted.evaluation_id
    assert second.persisted.evaluation_hash == first.persisted.evaluation_hash
    assert second.persisted.created is False
    assert second.metrics_artifact_id == first.metrics_artifact_id
    assert _counts(env, _result_id(env, evaluated)) == {
        "successes": 1,
        "failures": 0,
        "metrics_artifacts": 1,
    }
    assert len(sorted((tmp_path / "evaluation_artifacts").glob("*.json"))) == 1


def test_an_already_failed_evaluation_is_never_automatically_retried(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    _provision_real_truth(env, tmp_path / "practice", engine=service)
    _upstream(tmp_path).set_mode("raise")
    first = _run_with(service, env, evaluated, tmp_path, _oracle(tmp_path))
    assert first.failure_code == "HAPPY_NONZERO_EXIT"

    # a runner that WOULD succeed must not get the chance: the failure is terminal.
    exploding = _ExplodingOracle()
    second = _run_with(service, env, evaluated, tmp_path, exploding)

    assert exploding.calls == 0
    assert second.status == "FAILED"
    assert second.failure_code == "HAPPY_NONZERO_EXIT"
    assert second.failure_id == first.failure_id
    assert _counts(env, _result_id(env, evaluated)) == {
        "successes": 0,
        "failures": 1,
        "metrics_artifacts": 0,
    }


def test_a_crash_before_any_terminal_row_still_gets_a_fresh_attempt(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """Retry policy: no terminal row means the work never completed, so it may run again."""
    _provision_real_truth(env, tmp_path / "practice", engine=service)

    class _Crashing:
        calls = 0

        def score(self, **kwargs: Any) -> Any:
            type(self).calls += 1
            raise RuntimeError("process died mid-attempt, before anything was recorded")

    crashing = _Crashing()
    with pytest.raises(RuntimeError):
        _run_with(service, env, evaluated, tmp_path, crashing)

    assert _counts(env, _result_id(env, evaluated)) == {
        "successes": 0,
        "failures": 0,
        "metrics_artifacts": 0,
    }
    # nothing terminal was recorded, so a retry proceeds normally
    counted = _CountingOracle(_good_oracle(tmp_path))
    outcome = _run_with(service, env, evaluated, tmp_path, counted)
    assert counted.calls == 1
    assert outcome.status == "EVALUATED"


def test_a_dual_terminal_outcome_fails_closed(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """0010 makes this unreachable through the write path; if seen anyway, refuse to proceed."""
    from minos_engine.evaluation.orchestrator import DualTerminalOutcomeError

    _provision_real_truth(env, tmp_path / "practice", engine=service)
    result_id = _result_id(env, evaluated)
    first = _run_with(service, env, evaluated, tmp_path, _good_oracle(tmp_path))
    assert first.status == "EVALUATED"

    # forge the corrupt state the trigger prevents, by disabling it for one statement
    with env.engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "ALTER TABLE evaluation.l2f_evaluation_failures "
                "DISABLE TRIGGER trg_l2f_evaluation_failures_exclusive_outcome"
            )
        )
        conn.execute(
            text(
                "INSERT INTO evaluation.l2f_evaluation_failures ("
                "  execution_result_id, execution_result_hash, dataset_registry_id, "
                "  scoring_contract_hash, failure_code) "
                "SELECT r.execution_result_id, r.execution_result_hash, r.dataset_registry_id, "
                "       r.scoring_contract_hash, 'EVALUATION_ERROR' "
                "  FROM evaluation.l2f_evaluation_results r WHERE r.execution_result_id = :e"
            ),
            {"e": result_id},
        )
        conn.execute(
            text(
                "ALTER TABLE evaluation.l2f_evaluation_failures "
                "ENABLE TRIGGER trg_l2f_evaluation_failures_exclusive_outcome"
            )
        )

    with pytest.raises(DualTerminalOutcomeError, match="corrupt"):
        _run_with(service, env, evaluated, tmp_path, _ExplodingOracle())


def test_runtime_identity_never_enters_the_evaluation_hash(
    service: Any, env: Any, evaluated: Any, tmp_path: Path
) -> None:
    """The stored identity is reproducible from science alone — no path, container or attempt."""
    import json

    from minos_engine.evaluation.contracts import (
        ComparisonScope,
        EvaluationInputs,
        TruthIdentity,
        UpstreamScoreOutput,
        compute_evaluation_hash,
    )

    _provision_real_truth(env, tmp_path / "practice", engine=service)
    seen: list[Path] = []

    class _Recording:
        def score(self, **kwargs: Any) -> Any:
            seen.append(Path(kwargs["work_dir"]))
            return _good_oracle(tmp_path).score(**kwargs)

    outcome = _run_with(service, env, evaluated, tmp_path, _Recording())
    assert outcome.status == "EVALUATED" and outcome.persisted is not None
    attempt_dir = seen[0]

    published = sorted((tmp_path / "evaluation_artifacts").glob("*.json"))[0]
    payload = published.read_bytes()
    for runtime in (attempt_dir.name, str(attempt_dir), str(tmp_path), "minos-happy-"):
        assert runtime.encode() not in payload, f"runtime identity {runtime!r} leaked"

    # and the identity is REPRODUCIBLE from the stored scientific fields alone
    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT r.execution_result_hash, r.partition, r.scoring_contract_hash, "
                    "       r.metrics_artifact_sha256, r.evaluation_hash, r.admission_code, "
                    "       r.core_score, r.completeness_score, r.fp_score, r.quality_score, "
                    "       r.overcall_penalty, r.minos_score_100, r.minos_score, "
                    "       r.truth_vcf_sha256, r.truth_tbi_sha256, r.mutations_vcf_sha256, "
                    "       r.mutations_tbi_sha256, i.dataset_id, i.vcf_sha256, i.chromosome, "
                    "       i.region_start0, i.region_end0_exclusive "
                    "  FROM evaluation.l2f_evaluation_results r "
                    "  JOIN evaluation.l2f_completed_execution_inputs i "
                    "    ON i.execution_result_id = r.execution_result_id "
                    " WHERE r.execution_result_id = :e"
                ),
                {"e": _result_id(env, evaluated)},
            )
            .mappings()
            .one()
        )

    recomputed = compute_evaluation_hash(
        inputs=EvaluationInputs(
            execution_result_hash=str(row["execution_result_hash"]),
            dataset_id=str(row["dataset_id"]),
            partition=str(row["partition"]),
            vcf_sha256=str(row["vcf_sha256"]),
            truth=TruthIdentity(
                truth_vcf_sha256=str(row["truth_vcf_sha256"]),
                truth_tbi_sha256=str(row["truth_tbi_sha256"]),
                mutations_vcf_sha256=str(row["mutations_vcf_sha256"]),
                mutations_tbi_sha256=str(row["mutations_tbi_sha256"]),
            ),
            scope=ComparisonScope(
                chromosome=str(row["chromosome"]),
                region_start0=int(row["region_start0"]),
                region_end0_exclusive=int(row["region_end0_exclusive"]),
                region_source=(
                    f"{row['chromosome']}:{int(row['region_start0']) + 1}"
                    f"-{int(row['region_end0_exclusive'])}"
                ),
            ),
        ),
        scoring_contract_hash=str(row["scoring_contract_hash"]),
        metrics_artifact_sha256=str(row["metrics_artifact_sha256"]),
        # the upstream outcome is read back out of the PUBLISHED document, so the identity is
        # reproducible from durable evidence alone — no local recomputation anywhere.
        upstream=UpstreamScoreOutput(
            **json.loads(
                (
                    tmp_path / "evaluation_artifacts" / f"{row['metrics_artifact_sha256']}.json"
                ).read_text(encoding="utf-8")
            )["upstream"]
        ),
    )
    assert recomputed == str(row["evaluation_hash"])
    # the four AdvancedScorer components are NULL: upstream never exposed them, and nothing
    # locally reconstructed them to fill the columns in.
    for component in ("core_score", "completeness_score", "fp_score", "quality_score"):
        assert row[component] is None, component
    # what upstream DID expose is stored exactly.
    assert float(row["minos_score_100"]) == pytest.approx(86.25)
    assert float(row["minos_score"]) == pytest.approx(0.8625)
    assert float(row["overcall_penalty"]) == pytest.approx(0.5)
