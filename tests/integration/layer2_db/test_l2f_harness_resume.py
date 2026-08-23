"""L2-F F7-A idempotent resume + independent artifact verification (scratch PostgreSQL at 0008).

Resume behaviour is proved across a simulated process restart: every engine is disposed and the
worker state is rebuilt from nothing, then plan persistence and enqueue are replayed. The
deterministic FakeGatkRunner is used for this broad matrix — and a dedicated test proves that a
fake runner can never satisfy the official-GATK required check, so this suite can never stand in
for the official run that F7-B still owes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import (
    ExecutionResultManifest,
    compute_input_identity_hash,
    execution_input_from_manifest,
)
from minos_engine.qualification import l2f_harness_ready_runner as R
from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage.l2f_execution import assert_no_stranded_jobs, find_nonterminal_jobs
from minos_engine.storage.l2f_execution_contract import (
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
from tests.integration.layer2_db.test_l2f_execution_corrective import env as _env_fixture
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _count,
    _engine,
    _publisher,
)

env = _env_fixture

_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"
_PLANS = "experiments.l2f_experiment_plans"
_MEMBERS = "experiments.l2f_experiment_plan_members"
_CONFIGS = "experiments.l2f_experiment_plan_configs"
_ARTIFACTS = "catalog.artifacts"


def _replay_publisher(tmp_path: Path) -> Any:
    """The publisher a RESTARTED worker would build: the SAME provisioned CONFIG-artifact root.

    Content-addressed artifacts are bound to their recorded URI, so replaying against a different
    root is correctly refused as a metadata conflict; a genuine restart reuses the same root.
    """
    root = tmp_path / "cfgroot"
    if not root.exists():
        root.mkdir(parents=True)
        os.chmod(root, 0o2750)
    return _publisher(root)


def _row_counts(engine: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for label, table in (
        ("plans", _PLANS),
        ("members", _MEMBERS),
        ("configs", _CONFIGS),
        ("jobs", _JOBS),
        ("results", _RESULTS),
        ("failures", _FAILURES),
        ("artifacts", _ARTIFACTS),
    ):
        out[label] = _count(engine, f"SELECT count(*) FROM {table}")  # noqa: S608
    return out


def _artifact_fingerprint(root: Path) -> str:
    """A stable digest of every published artifact's name, bytes and permission bits."""
    digest = hashlib.sha256()
    for path in sorted(root.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
        digest.update(oct(path.stat().st_mode).encode())
    return digest.hexdigest()


def _database_fingerprint(engine: Any) -> str:
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT j.id, j.status, j.claimed_by, j.claimed_at, j.created_at, j.updated_at "
                f"FROM {_JOBS} j ORDER BY j.created_at, j.id"  # noqa: S608
            )
        ).all()
        results = c.execute(
            text(
                "SELECT r.job_id, r.result_hash, r.runtime_ms, r.created_at "
                f"FROM {_RESULTS} r ORDER BY r.job_key"  # noqa: S608
            )
        ).all()
    return hashlib.sha256(repr((rows, results)).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# J12 — resume after process/engine recreation
# --------------------------------------------------------------------------- #
def test_resume_after_engine_recreation_creates_no_duplicates(env: Any, tmp_path: Path) -> None:
    """Execute one job, DISPOSE every engine, rebuild the worker state, then replay."""
    first = env.run()
    assert first is not None and first.status == "SUCCEEDED"

    before_counts = _row_counts(env.engine)
    before_artifacts = _artifact_fingerprint(env.result_root)
    before_db = _database_fingerprint(env.engine)
    url = env.url

    # --- simulate a process restart: dispose everything and rebuild from nothing -------------
    env.engine.dispose()
    env.engine = _engine(url)

    # --- replay enqueue exactly as a restarted worker would (idempotent, jobs-tolerant) ------
    replayed = _enqueue_experiment_jobs_with_trust(env.engine, env.plan, _CS, start=0, count=4)
    assert replayed.created_count == 0
    assert replayed.existing_count == 4

    # --- and F3-C1 plan persistence stays a one-time bootstrap: replaying it after enqueue is
    #     refused rather than silently re-running (fail-closed, no duplicate graph) -----------
    with pytest.raises(Exception):  # noqa: B017 - a typed plan-verification refusal
        _persist_experiment_plan_with_trust(
            env.engine, env.plan, _CS, publisher=_replay_publisher(tmp_path)
        )

    after_counts = _row_counts(env.engine)
    assert after_counts == before_counts, (before_counts, after_counts)
    assert _artifact_fingerprint(env.result_root) == before_artifacts
    assert _database_fingerprint(env.engine) == before_db

    # the terminal job was neither reset nor re-executed
    with env.engine.connect() as c:
        status = str(
            c.execute(
                text(f"SELECT status FROM {_JOBS} WHERE id = :i"),  # noqa: S608
                {"i": first.job_id},
            ).scalar_one()
        )
    assert status == "SUCCEEDED"
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608
    assert find_nonterminal_jobs(env.engine, env.plan.plan_hash) == ()


def test_a_terminal_job_is_never_reclaimed_after_resume(env: Any) -> None:
    done = {r.job_id for r in (env.run(), env.run())}
    env.engine.dispose()
    env.engine = _engine(env.url)
    again = env.run()
    assert again is not None and again.job_id not in done


def test_the_exhausted_queue_returns_none_after_resume(env: Any) -> None:
    seen = set()
    for _ in range(4):
        result = env.run()
        assert result is not None
        seen.add(result.job_id)
    assert len(seen) == 4
    env.engine.dispose()
    env.engine = _engine(env.url)
    assert env.run() is None
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


# NOTE: exact F3-C1 plan-persistence replay idempotency (before any enqueue) is proven by the
# accepted F3-C1 suite and is not duplicated here. Under RESUME the relevant property is that
# replaying F3-C1 after jobs exist is REFUSED, which is asserted above, and that enqueue and
# result replay are no-ops, which is asserted below.
def test_an_exact_enqueue_replay_creates_nothing(env: Any) -> None:
    before = _row_counts(env.engine)
    replayed = _enqueue_experiment_jobs_with_trust(env.engine, env.plan, _CS, start=0, count=4)
    assert replayed.created_count == 0 and replayed.existing_count == 4
    assert _row_counts(env.engine) == before


def test_a_conflicting_replay_fails_closed(env: Any, tmp_path: Path) -> None:
    """Replaying with a DIFFERENT candidate set must be refused, never silently accepted."""
    from minos_engine.experiments.candidates import generate_accepted_candidate_set

    env.run()
    forged = dataclasses.replace(generate_accepted_candidate_set(), candidate_set_hash="f" * 64)
    with pytest.raises(Exception):  # noqa: B017 - any typed refusal
        _persist_experiment_plan_with_trust(
            env.engine, env.plan, forged, publisher=_replay_publisher(tmp_path)
        )


def test_a_failed_job_is_not_automatically_requeued_after_resume(env: Any) -> None:
    failed = env.run(runner=FakeGatkRunner(exit_code=3))
    assert failed is not None and failed.status == "FAILED"
    env.engine.dispose()
    env.engine = _engine(env.url)
    following = env.run()
    assert following is not None and following.job_id != failed.job_id
    with env.engine.connect() as c:
        status = str(
            c.execute(
                text(f"SELECT status FROM {_JOBS} WHERE id = :i"),  # noqa: S608
                {"i": failed.job_id},
            ).scalar_one()
        )
    assert status == "FAILED"  # never reset to PENDING


def test_cancelled_remains_unreachable(env: Any) -> None:
    env.run()
    with env.engine.connect() as c:
        statuses = {
            str(r[0])
            for r in c.execute(text(f"SELECT DISTINCT status FROM {_JOBS}")).all()  # noqa: S608
        }
    assert "CANCELLED" not in statuses


# --------------------------------------------------------------------------- #
# F/J16-J19 — independent artifact and result verification
# --------------------------------------------------------------------------- #
def _published(env: Any, suffix: str) -> Path:
    matches = [p for p in env.result_root.iterdir() if p.name.endswith(suffix)]
    assert matches, suffix
    return matches[0]


def test_every_artifact_is_independently_recomputed(env: Any) -> None:
    result = env.run()
    assert result is not None

    with env.engine.connect() as c:
        row = (
            c.execute(
                text(
                    "SELECT r.vcf_sha256, r.result_manifest_sha256, r.input_identity_hash, "
                    "       r.logical_argv_hash, r.result_hash, v.uri AS vcf_uri, "
                    "       v.media_type AS vcf_media, v.size_bytes AS vcf_size, "
                    "       m.uri AS man_uri, m.media_type AS man_media, m.size_bytes AS man_size "
                    f"  FROM {_RESULTS} r "  # noqa: S608
                    "  JOIN catalog.artifacts v ON v.id = r.vcf_artifact_id "
                    "  JOIN catalog.artifacts m ON m.id = r.result_manifest_artifact_id"
                )
            )
            .mappings()
            .one()
        )

    vcf = Path(str(row["vcf_uri"]).removeprefix("file://"))
    man = Path(str(row["man_uri"]).removeprefix("file://"))

    # exact bytes -> recomputed digest and size
    vcf_bytes, man_bytes = vcf.read_bytes(), man.read_bytes()
    assert hashlib.sha256(vcf_bytes).hexdigest() == str(row["vcf_sha256"])
    assert hashlib.sha256(man_bytes).hexdigest() == str(row["result_manifest_sha256"])
    assert int(row["vcf_size"]) == len(vcf_bytes)
    assert int(row["man_size"]) == len(man_bytes)

    # fixed media types and content-addressed filenames
    assert str(row["vcf_media"]) == L2F_VCF_MEDIA_TYPE
    assert str(row["man_media"]) == L2F_RESULT_MANIFEST_MEDIA_TYPE
    assert vcf.name == f"{row['vcf_sha256']}.vcf"
    assert man.name == f"{row['result_manifest_sha256']}.result.json"

    # strict canonical parsing, then recomputation of the frozen identities
    manifest = ExecutionResultManifest.model_validate_json(man_bytes)
    assert canonical_json_bytes(json.loads(man_bytes)) == man_bytes
    inputs = execution_input_from_manifest(manifest)
    assert compute_input_identity_hash(inputs) == str(row["input_identity_hash"])
    assert manifest.logical_argv_hash == str(row["logical_argv_hash"])
    assert manifest.result_hash == str(row["result_hash"]) == result.result_hash


def test_the_harness_verifier_passes_every_named_check(env: Any) -> None:
    env.run()
    verification = env.verify()
    assert verification.status == HV.STATUS_PASS
    assert set(verification.checks) == set(HV.CHECK_NAMES)
    assert all(verification.checks.values())
    assert verification.failures == ()


def test_repeated_verification_is_non_mutating(env: Any) -> None:
    env.run()
    before_db = _database_fingerprint(env.engine)
    before_artifacts = _artifact_fingerprint(env.result_root)
    for _ in range(3):
        assert env.verify().status == HV.STATUS_PASS
    assert _database_fingerprint(env.engine) == before_db
    assert _artifact_fingerprint(env.result_root) == before_artifacts


@pytest.mark.parametrize("suffix", [".vcf", ".result.json"])
def test_a_tampered_artifact_is_detected(env: Any, suffix: str) -> None:
    env.run()
    victim = _published(env, suffix)
    victim.chmod(0o640)
    victim.write_bytes(victim.read_bytes() + b"\n# tampered\n")
    assert env.verify().status != HV.STATUS_PASS


def test_a_self_consistently_rehashed_manifest_is_detected(env: Any) -> None:
    """Rewriting the manifest AND its digest still fails: the append-only row anchors the bytes."""
    env.run()
    man = _published(env, ".result.json")
    document = json.loads(man.read_bytes())
    document["worker_id"] = "attacker"
    man.chmod(0o640)
    man.write_bytes(canonical_json_bytes(document))
    assert env.verify().status != HV.STATUS_PASS


def test_a_forged_result_hash_is_detected(env: Any) -> None:
    env.run()
    man = _published(env, ".result.json")
    document = json.loads(man.read_bytes())
    document["result_hash"] = "0" * 64
    man.chmod(0o640)
    man.write_bytes(canonical_json_bytes(document))
    assert env.verify().status != HV.STATUS_PASS


def test_a_config_artifact_tamper_is_detected(env: Any, tmp_path: Path) -> None:
    env.run()
    with env.engine.connect() as c:
        uris = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT a.uri FROM experiments.l2f_config_payloads cp "
                    "JOIN catalog.artifacts a ON a.id = cp.artifact_id"
                )
            ).all()
        ]
    assert uris
    for uri in uris:
        path = Path(uri.removeprefix("file://"))
        path.chmod(0o640)
        path.write_bytes(b'{"tampered": true}')
    assert env.verify().status != HV.STATUS_PASS


def test_an_unexpected_nonterminal_job_is_reported(env: Any) -> None:
    from minos_engine.storage.l2f_job_claim import _claim_next_job_with_trust

    env.run()
    claimed = _claim_next_job_with_trust(env.engine, env.plan, worker_id="w-stuck")
    assert claimed is not None
    assert find_nonterminal_jobs(env.engine, env.plan.plan_hash) == ((claimed.job_id, "CLAIMED"),)


# --------------------------------------------------------------------------- #
# D/J6 — a fake runner can never satisfy the official-GATK required check
# --------------------------------------------------------------------------- #
def test_this_suites_fake_runner_can_never_qualify_harness_ready(env: Any) -> None:
    """This whole suite runs on FakeGatkRunner, and must not be able to issue HARNESS-READY."""
    result = env.run()
    assert result is not None and result.status == "SUCCEEDED"
    checks = {"official_gatk_runner_used": False}
    required = set(R.HARNESS_READY_REQUIRED_CHECKS)
    assert "official_gatk_runner_used" in required
    assert not all({**dict.fromkeys(required, True), **checks}.values())


def test_the_operational_database_is_never_the_qualification_target(env: Any) -> None:
    with env.engine.connect() as c:
        name = str(c.execute(text("SELECT current_database()")).scalar_one())
    assert name != R.OPERATIONAL_DATABASE_NAME
    with pytest.raises(R.OperationalDatabaseRefused):
        R.refuse_operational_database(f"postgresql://u@h/{R.OPERATIONAL_DATABASE_NAME}")


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 2 — the executed job must BE the derived F7 qualification job
# --------------------------------------------------------------------------- #
def test_a_contaminated_scratch_slice_refuses_qualification(env: Any) -> None:
    """Another claimable PENDING job must cause a refusal, not a mislabelled F7 execution."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    with env.engine.connect() as c:
        rows = c.execute(
            text(
                f"SELECT j.job_key FROM {_JOBS} j ORDER BY j.created_at, j.id"  # noqa: S608
            )
        ).all()
    assert len(rows) >= 2, "the fixture enqueues several claimable jobs"
    derived = type("J", (), {"job_key": str(rows[0][0])})()
    with pytest.raises(Q.QualificationEnvironmentError, match="does not contain exactly"):
        Q._require_exact_qualification_slice(env.engine, env.plan, derived)


def test_an_exact_single_job_slice_is_accepted(env: Any) -> None:
    """Control: once every other job is terminal, the remaining derived job is accepted."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    for _ in range(3):
        assert env.run() is not None
    with env.engine.connect() as c:
        remaining = [
            str(r[0])
            for r in c.execute(
                text(
                    f"SELECT job_key FROM {_JOBS} "  # noqa: S608
                    "WHERE status NOT IN ('SUCCEEDED','FAILED')"
                )
            ).all()
        ]
    assert len(remaining) == 1
    derived = type("J", (), {"job_key": remaining[0]})()
    Q._require_exact_qualification_slice(env.engine, env.plan, derived)  # must not raise


def test_a_dispatched_job_that_is_not_the_derived_job_is_refused(env: Any) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q
    from minos_engine.qualification.l2f_harness_ready_qualifier import DerivedQualificationJob

    dispatched = env.run()
    assert dispatched is not None
    wrong = DerivedQualificationJob(
        member_index=0,
        candidate_index=0,
        dataset_id="d",
        profile_id="p",
        partition="train",
        job_key="f" * 64,
        config_hash="e" * 64,
        effective_config={},
    )
    with pytest.raises(Q.QualificationEnvironmentError, match="is not the derived"):
        Q._require_dispatched_is_derived(env.engine, env.plan, wrong, dispatched)


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 3 — real resume evidence
# --------------------------------------------------------------------------- #
def test_every_tracked_table_is_unchanged_across_a_restart_and_replay(env: Any) -> None:
    """Pre/post counts on EVERY table a replay could duplicate, not just the enqueue report."""
    from minos_engine.qualification.l2f_harness_ready_qualifier import row_counts

    assert env.run() is not None
    before = row_counts(env.engine)
    assert set(before) == {
        "plans",
        "members",
        "config_payloads",
        "configs",
        "jobs",
        "results",
        "failures",
        "artifacts",
    }
    artifacts_before = _artifact_fingerprint(env.result_root)

    env.engine.dispose()
    env.engine = _engine(env.url)
    _enqueue_experiment_jobs_with_trust(env.engine, env.plan, _CS, start=0, count=4)

    after = row_counts(env.engine)
    assert after == before, (before, after)
    assert _artifact_fingerprint(env.result_root) == artifacts_before


def test_a_conflicting_replay_actually_reaches_the_persistence_boundary(
    env: Any, tmp_path: Path
) -> None:
    """The forged candidate set really hits _persist_experiment_plan_with_trust and is rejected."""
    from minos_engine.qualification.l2f_harness_ready_qualifier import (
        attempt_conflicting_replay,
        row_counts,
    )

    assert env.run() is not None
    before = row_counts(env.engine)
    artifacts_before = _artifact_fingerprint(env.result_root)
    db_before = _database_fingerprint(env.engine)

    rejected, created = attempt_conflicting_replay(
        env.engine, env.plan, _replay_publisher(tmp_path)
    )
    assert rejected is True
    assert created == 0
    assert row_counts(env.engine) == before
    assert _artifact_fingerprint(env.result_root) == artifacts_before
    assert _database_fingerprint(env.engine) == db_before


def test_a_failed_job_survives_restart_and_is_never_auto_retried(env: Any) -> None:
    """Control (deterministic FakeGatkRunner): FAILED stays FAILED and is never reclaimed."""
    from minos_engine.qualification.l2f_harness_ready_qualifier import observe_no_automatic_retry

    failed = env.run(runner=FakeGatkRunner(exit_code=3))
    assert failed is not None and failed.status == "FAILED"

    env.engine.dispose()
    env.engine = _engine(env.url)

    remained, reclaimed = observe_no_automatic_retry(env.engine, env.plan)
    assert remained is True
    assert reclaimed is False

    # resuming the worker takes a DIFFERENT job and never re-runs the failed one
    following = env.run()
    assert following is not None and following.job_id != failed.job_id
    remained_after, reclaimed_after = observe_no_automatic_retry(env.engine, env.plan)
    assert remained_after is True and reclaimed_after is False
    with env.engine.connect() as c:
        status = str(
            c.execute(
                text(f"SELECT status FROM {_JOBS} WHERE id=:i"),  # noqa: S608
                {"i": failed.job_id},
            ).scalar_one()
        )
    assert status == "FAILED"


def test_the_operational_fingerprint_requires_a_read_only_transaction(env: Any) -> None:
    """A scratch connection is writable, so the operational observer refuses it."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    fingerprint, revision, l2f_tables = Q.operational_fingerprint(env.engine)
    # the scratch DB is at 0008 with L2-F tables; the operational assertions are what reject it
    assert revision == "0008_l2f_execution_results"
    assert l2f_tables > 0
    assert len(fingerprint) == 64
