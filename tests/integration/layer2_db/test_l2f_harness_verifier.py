"""F3-D accepted-harness verifier — real-PostgreSQL behavioral tests.

Covers the relational attacks that genuinely require a database (forged job rows, a job key bound
to the wrong member/config, tampered artifact bytes, non-train membership introduced into the live
upstream), the fail-closed absent/ambiguous-graph behavior, the identity + revision ordering, the
external proof that verification mutates nothing, and partial/complete job coverage over two
uneven non-75 plans. Pure hash/contract attacks live in
``tests/unit/experiments/test_harness_verifier_attacks.py``.

Uses the dedicated ``isolated_pg_base_url`` cluster (see the plan-store suite for the CI-isolation
rationale); the real operational store is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.experiments import harness_verifier as HV
from minos_engine.experiments.harness_verifier import (
    STATUS_FAIL,
    STATUS_PASS,
    HarnessGraphError,
    verify_accepted_experiment_harness,
)
from minos_engine.experiments.plan import iter_logical_jobs
from minos_engine.storage import l2f_job_enqueue as EN
from minos_engine.storage.database import OperationalDatabaseIdentityError
from minos_engine.storage.l2f_plan_store import (
    PlanRevisionError,
    _persist_experiment_plan_with_trust,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.l2f_seed import H, U, _bam_row, _dataset_row, _insert
from tests.integration.layer2_db.test_l2f_plan_store import (
    _ACCEPTED_PLAN,
    _CS,
    _SNAPSHOT_A,
    _SNAPSHOT_B,
    _artifact_files,
    _count,
    _engine,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_OP_DB = "minos_engine_db"


def _persist(engine: Engine, plan: Any, root: Path) -> None:
    _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))


def _enqueue(engine: Engine, plan: Any, *, start: int, count: int) -> Any:
    return EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=start, count=count)


def _verify(engine: Engine, plan: Any) -> Any:
    return HV._verify_experiment_harness_with_trust(engine, plan, _CS)


def _scalar(engine: Engine, sql: str, **p: Any) -> str:
    with engine.connect() as c:
        return str(c.execute(text(sql), p).scalar_one())


def _plan_id(engine: Engine, plan_hash: str) -> str:
    with engine.connect() as c:
        return str(
            c.execute(
                text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash=:h"),
                {"h": plan_hash},
            ).scalar_one()
        )


def _member_config_ids(engine: Engine, plan_id: str) -> tuple[list[str], list[str]]:
    with engine.connect() as c:
        members = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT id FROM experiments.l2f_experiment_plan_members "
                    "WHERE plan_id=:p ORDER BY member_index"
                ),
                {"p": plan_id},
            ).all()
        ]
        configs = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT id FROM experiments.l2f_experiment_plan_configs "
                    "WHERE plan_id=:p ORDER BY config_index"
                ),
                {"p": plan_id},
            ).all()
        ]
    return members, configs


def _insert_raw_job(
    engine: Engine, plan_id: str, member_id: str, config_id: str, job_key: str
) -> None:
    """Insert a job row with fully valid foreign keys but a caller-chosen job_key. The database
    cannot police a hash, so this is a legitimate forgery — no constraint is disabled."""
    with engine.connect() as c, c.begin():
        c.execute(text("SET ROLE minos_admin"))
        c.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_jobs "
                "(plan_id, plan_member_id, plan_config_id, job_key, status) "
                "VALUES (:p,:m,:c,:k,'PENDING')"
            ),
            {"p": plan_id, "m": member_id, "c": config_id, "k": job_key},
        )


# --------------------------------------------------------------------------- #
# happy paths + partial / complete job coverage (two uneven non-75 plans)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("spec", "train"), [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)])
def test_zero_jobs_is_valid_and_reports_all_missing(
    isolated_pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], train: int
) -> None:
    plan = _synthetic_plan(spec)
    ljc = train * len(_CS.configs)
    assert plan.logical_job_count == ljc  # derived, never a constant
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            r = _verify(engine, plan)
            assert r.status == STATUS_PASS and r.failures == ()
            assert r.persisted_job_count == 0 and r.missing_job_count == ljc
            assert r.logical_job_count == ljc
        finally:
            engine.dispose()


def test_partial_bounded_range_is_valid(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            _enqueue(engine, plan, start=0, count=9)
            r = _verify(engine, plan)
            assert r.status == STATUS_PASS
            assert r.persisted_job_count == 9
            assert r.missing_job_count == plan.logical_job_count - 9
        finally:
            engine.dispose()


def test_overlapping_idempotent_enqueue_verifies(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            _enqueue(engine, plan, start=0, count=10)
            _enqueue(engine, plan, start=5, count=10)  # overlapping replay
            r = _verify(engine, plan)
            assert r.status == STATUS_PASS
            assert r.persisted_job_count == 15
        finally:
            engine.dispose()


def test_complete_synthetic_coverage_verifies(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """A small non-75 plan enqueued to completion (in bounded batches) verifies with zero
    missing jobs — counts derive from membership × candidates, never 50/41/2050."""
    plan = _synthetic_plan(_SNAPSHOT_B)  # 2 train members
    ljc = plan.logical_job_count
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            start = 0
            while start < ljc:
                count = min(EN.MAX_ENQUEUE_BATCH, ljc - start)
                _enqueue(engine, plan, start=start, count=count)
                start += count
            r = _verify(engine, plan)
            assert r.status == STATUS_PASS and r.failures == ()
            assert r.persisted_job_count == ljc and r.missing_job_count == 0
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# relational attacks 10-12: forged / mis-bound job rows
# --------------------------------------------------------------------------- #
def test_attack10_forged_job_key_with_valid_foreign_keys(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            _insert_raw_job(engine, pid, members[0], configs[0], "a" * 64)
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "jobs_within_logical_universe" in r.failures
            assert "job_keys_recompute" in r.failures
        finally:
            engine.dispose()


def test_attack11_correct_job_key_on_wrong_plan_member(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            jobs = list(iter_logical_jobs(plan))
            # job[0] is (member 0, config 0); attach its genuine key to member 1 instead.
            _insert_raw_job(engine, pid, members[1], configs[0], jobs[0].job_key)
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "job_member_config_binding" in r.failures
            assert "job_keys_recompute" in r.failures
        finally:
            engine.dispose()


def test_attack12_correct_job_key_on_wrong_plan_config(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            jobs = list(iter_logical_jobs(plan))
            _insert_raw_job(engine, pid, members[0], configs[1], jobs[0].job_key)
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "job_member_config_binding" in r.failures
            assert "job_keys_recompute" in r.failures
        finally:
            engine.dispose()


def test_one_forged_job_in_otherwise_valid_subset_is_rejected(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            _enqueue(engine, plan, start=0, count=8)  # a valid bounded subset
            assert _verify(engine, plan).status == STATUS_PASS
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            # one forged job among eight legitimate ones.
            _insert_raw_job(engine, pid, members[3], configs[7], "b" * 64)
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "jobs_within_logical_universe" in r.failures
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# attack 14: noncanonical CONFIG artifact bytes on disk
# --------------------------------------------------------------------------- #
def test_attack14_noncanonical_artifact_bytes(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            _persist(engine, plan, root)
            assert _verify(engine, plan).status == STATUS_PASS
            target = _artifact_files(root)[0]
            os.chmod(target, 0o640)
            target.write_bytes(b'{"tampered": true}')
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "config_payload_bytes_canonical" in r.failures
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# attack 16: non-train membership introduced into the live upstream
# --------------------------------------------------------------------------- #
def test_attack16_extra_upstream_train_member_detected(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """An extra live train member appearing AFTER a valid persist (which F3-C1 would have
    rejected at write time) is caught by the verifier's upstream exactness check."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            assert _verify(engine, plan).status == STATUS_PASS
            # introduce a fully valid EXTRA train member into the live snapshot + matrix.
            tag = plan.plan_hash
            snap = _scalar(
                engine,
                "SELECT id FROM profiling.profile_snapshots WHERE snapshot_hash=:h",
                h=plan.snapshot_hash,
            )
            mat = _scalar(
                engine,
                "SELECT id FROM profiling.feature_matrices WHERE matrix_hash=:h",
                h=plan.train_matrix_hash,
            )
            agen = _scalar(
                engine, "SELECT id FROM catalog.artifacts WHERE uri=:u", u=f"mem://gen/{tag}"
            )
            with engine.connect() as c, c.begin():
                c.execute(text("SET ROLE minos_admin"))
                dsr = U(f"attack16:dsr:{tag}")
                drow = _dataset_row(f"attack16-{tag}", 4)
                drow["id"] = dsr
                drow["dataset_id"] = f"attack16-ds-{tag}"
                _insert(c, "catalog", "dataset_registry", drow)
                bam = _bam_row(f"attack16-{tag}", dsr, agen)
                _insert(c, "profiling", "bam_profiles", bam, jsonb_cols=("profile_document",))
                _insert(
                    c,
                    "profiling",
                    "profile_snapshot_members",
                    {
                        "id": U(f"attack16:psm:{tag}"),
                        "profile_snapshot_id": snap,
                        "bam_profile_id": bam["id"],
                        "dataset_registry_id": dsr,
                        "partition": "train",
                        "feature_values_hash": bam["feature_values_hash"],
                    },
                )
                _insert(
                    c,
                    "profiling",
                    "feature_matrix_members",
                    {
                        "id": U(f"attack16:fmm:{tag}"),
                        "feature_matrix_id": mat,
                        "dataset_registry_id": dsr,
                        "member_index": plan.train_member_count,
                        "vector_hash": H(f"attack16:vec:{tag}"),
                        "feature_values_hash": bam["feature_values_hash"],
                    },
                )
            r = _verify(engine, plan)
            assert r.status == STATUS_FAIL
            assert "upstream_membership_exact" in r.failures
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# attack 18 (external proof) + fail-closed + identity/revision ordering
# --------------------------------------------------------------------------- #
def _db_state(engine: Engine, root: Path) -> dict[str, Any]:
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, job_key, status, claimed_by, claimed_at, created_at, updated_at "
                "FROM experiments.l2f_experiment_jobs ORDER BY id"
            )
        ).all()
        stamps = c.execute(
            text("SELECT id, created_at FROM experiments.l2f_experiment_plan_members ORDER BY id")
        ).all()
    return {
        "counts": {
            t: _count(engine, f"SELECT count(*) FROM experiments.{t}")  # noqa: S608
            for t in (
                "l2f_experiment_plans",
                "l2f_experiment_plan_members",
                "l2f_config_payloads",
                "l2f_experiment_plan_configs",
                "l2f_experiment_jobs",
            )
        },
        "artifacts": _count(engine, "SELECT count(*) FROM catalog.artifacts"),
        "jobs": [tuple(str(v) for v in r) for r in rows],
        "member_stamps": [tuple(str(v) for v in r) for r in stamps],
        "files": {f.name: f.read_bytes() for f in _artifact_files(root)},
    }


def test_attack18_verification_is_externally_non_mutating(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            _persist(engine, plan, root)
            _enqueue(engine, plan, start=0, count=6)
            before = _db_state(engine, root)

            for _ in range(3):  # repeated verification changes nothing
                r = _verify(engine, plan)
                assert r.status == STATUS_PASS
                assert r.checks["verification_non_mutating"] is True

            assert _db_state(engine, root) == before
        finally:
            engine.dispose()


def test_absent_plan_graph_fails_closed(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)  # upstream seeded, plan graph never persisted
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            with pytest.raises(HarnessGraphError):
                _verify(engine, plan)
        finally:
            engine.dispose()


def _install_build_sentinel(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"plan": 0, "candidates": 0, "read": 0}
    orig_plan = HV.build_accepted_experiment_plan
    orig_cs = HV._build_accepted_candidate_set
    orig_read = HV._read_persisted_graph

    def _p() -> Any:
        calls["plan"] += 1
        return orig_plan()

    def _c() -> Any:
        calls["candidates"] += 1
        return orig_cs()

    def _r(conn: Any, plan: Any) -> Any:
        calls["read"] += 1
        return orig_read(conn, plan)

    monkeypatch.setattr(HV, "build_accepted_experiment_plan", _p)
    monkeypatch.setattr(HV, "_build_accepted_candidate_set", _c)
    monkeypatch.setattr(HV, "_read_persisted_graph", _r)
    return calls


def test_wrong_database_identity_fails_before_construction_or_query(
    isolated_pg_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, "not_the_operational_store") as url:
        alembic_upgrade(url, _HEAD)
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        calls = _install_build_sentinel(monkeypatch)
        with pytest.raises(OperationalDatabaseIdentityError):
            verify_accepted_experiment_harness()
        assert calls == {"plan": 0, "candidates": 0, "read": 0}


def test_revision_0005_fails_before_construction_or_query(
    isolated_pg_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _PREV)
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        calls = _install_build_sentinel(monkeypatch)
        with pytest.raises(PlanRevisionError):
            verify_accepted_experiment_harness()
        assert calls == {"plan": 0, "candidates": 0, "read": 0}
        # the verifier NEVER migrates.
        assert _count_version(url) == _PREV


def _count_version(url: str) -> str:
    engine = _engine(url)
    try:
        with engine.connect() as c:
            return str(c.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    finally:
        engine.dispose()


def test_accepted_production_verification_end_to_end(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted no-argument entry point verifies a genuine accepted F3-C1 + F3-C2 state."""
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv("MINOS_L2F_CONFIG_ARTIFACT_ROOT", str(root))
            from minos_engine.storage.l2f_plan_store import persist_accepted_experiment_plan

            persist_accepted_experiment_plan()
            EN.enqueue_accepted_experiment_jobs(start=0, count=5)

            r = verify_accepted_experiment_harness()
            assert r.status == STATUS_PASS and r.failures == ()
            assert r.plan_hash == _ACCEPTED_PLAN.plan_hash
            assert r.candidate_set_hash == _ACCEPTED_PLAN.candidate_set_hash
            assert r.logical_job_count == _ACCEPTED_PLAN.logical_job_count
            assert r.persisted_job_count == 5
            assert r.missing_job_count == _ACCEPTED_PLAN.logical_job_count - 5
            assert all(r.checks.values())
        finally:
            engine.dispose()
