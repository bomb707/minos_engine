"""F4 safe job claiming + pre-execution transitions — real-PostgreSQL behavioral tests.

Covers the 0006<->0007 lifecycle, the exact grant/function inventory and role denials, claim
concurrency (one winner, distinct jobs, SKIP LOCKED past independently held row locks),
deterministic ordering, rollback, wrong-worker rejection, invalid transitions, commit ambiguity,
scientific-identity immutability, the updated F3-D verifier, and F3-C/legacy immutability — over
the accepted plan and two uneven non-75 plans.

Sequencing note: F3-C1 persistence and F3-C2 enqueue each require live revision exactly
``0006_l2f_experiment_plan``, so every fixture persists + enqueues at 0006 and only then upgrades
to ``0007_l2f_job_claiming`` to exercise claiming. Uses the dedicated ``isolated_pg_base_url``
cluster; the real operational store is never touched.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage import l2f_job_claim as JC
from minos_engine.storage.database import OperationalDatabaseIdentityError
from minos_engine.storage.l2f_job_claim import (
    AmbiguousClaimCommitError,
    InvalidJobTransitionError,
    JobPlanMissingError,
    claim_next_accepted_job,
)
from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
from minos_engine.storage.l2f_plan_store import (
    PlanRevisionError,
    _persist_experiment_plan_with_trust,
)
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
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

_L2F = "0006_l2f_experiment_plan"
_F4 = "0007_l2f_job_claiming"
_OP_DB = "minos_engine_db"

_CLAIM_SIG = "experiments.minos_l2f_claim_next_job(text, text)"
_START_SIG = "experiments.minos_l2f_start_job(uuid, text)"
_RELEASE_SIG = "experiments.minos_l2f_release_job(uuid, text)"
_F4_SIGS = (_CLAIM_SIG, _START_SIG, _RELEASE_SIG)
_JOBS = "experiments.l2f_experiment_jobs"


def _prepare(url: str, plan: Any, root: Path, *, jobs: int) -> Engine:
    """Persist the F3-C1 graph and enqueue ``jobs`` at 0006, then upgrade to the F4 revision."""
    alembic_upgrade(url, _L2F)
    engine = _engine(url)
    with engine.connect() as conn, conn.begin():
        seed_upstream_for_plan(conn, plan)
    _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))
    if jobs:
        _enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=jobs)
    engine.dispose()
    alembic_upgrade(url, _F4)
    return _engine(url)


def _claim(engine: Engine, plan: Any, worker: str) -> Any:
    return JC._claim_next_job_with_trust(engine, plan, worker_id=worker)


def _job_state(engine: Engine, job_id: str) -> tuple[str, str | None, bool]:
    with engine.connect() as c:
        row = c.execute(
            text(f"SELECT status, claimed_by, claimed_at IS NULL FROM {_JOBS} WHERE id=:i"),  # noqa: S608
            {"i": job_id},
        ).one()
    return str(row[0]), row[1], bool(row[2])


def _identity_rows(engine: Engine) -> list[tuple[str, ...]]:
    with engine.connect() as c:
        return [
            tuple(str(v) for v in r)
            for r in c.execute(
                text(
                    "SELECT id, plan_id, plan_member_id, plan_config_id, job_key, created_at "
                    f"FROM {_JOBS} ORDER BY id"  # noqa: S608
                )
            ).all()
        ]


# --------------------------------------------------------------------------- #
# G1/G2: migration lifecycle + 0006 byte identity
# --------------------------------------------------------------------------- #
def test_0006_0007_0006_0007_lifecycle(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        alembic_upgrade(url, _L2F)
        engine = _engine(url)
        try:

            def _rev() -> str:
                with engine.connect() as c:
                    return str(
                        c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    )

            def _fn_count() -> int:
                with engine.connect() as c:
                    return int(
                        c.execute(
                            text(
                                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                                "ON n.oid=p.pronamespace WHERE n.nspname='experiments' "
                                "AND p.proname IN ('minos_l2f_claim_next_job','minos_l2f_start_job',"
                                "'minos_l2f_release_job','minos_l2f_job_transition_guard')"
                            )
                        ).scalar_one()
                    )

            def _guard_trigger() -> int:
                with engine.connect() as c:
                    return int(
                        c.execute(
                            text(
                                "SELECT count(*) FROM pg_trigger WHERE tgrelid=CAST(:t AS regclass) "
                                "AND tgname='trg_l2f_jobs_transition_guard'"
                            ),
                            {"t": _JOBS},
                        ).scalar_one()
                    )

            assert _rev() == _L2F and _fn_count() == 0 and _guard_trigger() == 0
            alembic_upgrade(url, _F4)
            assert _rev() == _F4 and _fn_count() == 4 and _guard_trigger() == 1
            alembic_downgrade(url, _L2F)
            assert _rev() == _L2F and _fn_count() == 0 and _guard_trigger() == 0
            # 0006's own job triggers survive the F4 downgrade untouched.
            with engine.connect() as c:
                names = sorted(
                    str(r[0])
                    for r in c.execute(
                        text(
                            "SELECT tgname FROM pg_trigger WHERE tgrelid=CAST(:t AS regclass) "
                            "AND NOT tgisinternal"
                        ),
                        {"t": _JOBS},
                    ).all()
                )
            assert names == ["trg_l2f_jobs_identity_immutable", "trg_l2f_jobs_no_delete"]
            alembic_upgrade(url, _F4)
            assert _rev() == _F4 and _fn_count() == 4 and _guard_trigger() == 1
        finally:
            engine.dispose()


def test_migration_0006_remains_byte_identical() -> None:
    from minos_engine.storage.l2f_migration_contract import (
        L2F_MIGRATION_SHA256,
        compute_migration_sha256,
    )

    assert compute_migration_sha256() == L2F_MIGRATION_SHA256
    assert L2F_MIGRATION_SHA256 == (
        "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
    )


# --------------------------------------------------------------------------- #
# G3/G4/G5: grants, function permissions, role denial
# --------------------------------------------------------------------------- #
def test_exact_function_grants_and_role_denials(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        alembic_upgrade(url, _F4)
        engine = _engine(url)
        try:
            with engine.connect() as c:
                for sig in _F4_SIGS:
                    # SECURITY DEFINER, owned by minos_admin
                    secdef, owner = c.execute(
                        text(
                            "SELECT p.prosecdef, pg_get_userbyid(p.proowner) FROM pg_proc p "
                            "WHERE p.oid = CAST(:s AS regprocedure)"
                        ),
                        {"s": sig},
                    ).one()
                    assert secdef is True and owner == "minos_admin", sig
                    # fixed secure search_path
                    cfg = c.execute(
                        text("SELECT proconfig FROM pg_proc WHERE oid = CAST(:s AS regprocedure)"),
                        {"s": sig},
                    ).scalar_one()
                    assert cfg == ["search_path=pg_catalog"], (sig, cfg)
                    # EXECUTE: runner + admin only; PUBLIC and every other role denied
                    assert c.execute(
                        text("SELECT has_function_privilege('minos_runner', :s, 'EXECUTE')"),
                        {"s": sig},
                    ).scalar_one()
                    assert c.execute(
                        text("SELECT has_function_privilege('minos_admin', :s, 'EXECUTE')"),
                        {"s": sig},
                    ).scalar_one()
                    assert not c.execute(
                        text("SELECT has_function_privilege('public', :s, 'EXECUTE')"),
                        {"s": sig},
                    ).scalar_one()
                    for role in ("minos_live", "minos_trainer", "minos_evaluator"):
                        assert not c.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": sig},
                        ).scalar_one(), (role, sig)
                # no role holds ANY direct privilege on the job table
                for role in ("minos_runner", "minos_live", "minos_trainer", "minos_evaluator"):
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert not c.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": _JOBS, "p": priv},
                        ).scalar_one(), (role, priv)
        finally:
            engine.dispose()


def test_runner_may_only_use_the_three_functions(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=3)
        try:
            # as minos_runner: the claim function works...
            with engine.connect() as c, c.begin():
                c.execute(text("SET ROLE minos_runner"))
                row = (
                    c.execute(
                        text("SELECT * FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                        {"h": plan.plan_hash, "w": "runner-1"},
                    )
                    .mappings()
                    .first()
                )
                assert row is not None and row["status"] == "CLAIMED"
            # ...but a direct table mutation is denied.
            for stmt in (
                f"UPDATE {_JOBS} SET status='PENDING'",  # noqa: S608
                f"DELETE FROM {_JOBS}",  # noqa: S608
                f"SELECT count(*) FROM {_JOBS}",  # noqa: S608
            ):
                with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
                    c.execute(text("SET ROLE minos_runner"))
                    c.execute(text(stmt))
                assert "permission denied" in str(ei.value).lower(), stmt
        finally:
            engine.dispose()


@pytest.mark.parametrize("role", ["minos_live", "minos_trainer", "minos_evaluator"])
def test_other_roles_cannot_execute_f4_functions(
    isolated_pg_base_url: str, tmp_path: Path, role: str
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=2)
        try:
            with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
                c.execute(text(f"SET ROLE {role}"))
                c.execute(
                    text("SELECT * FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                    {"h": plan.plan_hash, "w": "w1"},
                )
            assert "permission denied" in str(ei.value).lower()
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# G6/G7/G8/G10: concurrency, SKIP LOCKED, deterministic order
# --------------------------------------------------------------------------- #
def test_same_job_concurrent_claim_has_one_winner(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=1)  # exactly ONE job
        try:

            def _run(worker: str) -> Any:
                eng = _engine(url)
                try:
                    return _claim(eng, plan, worker)
                finally:
                    eng.dispose()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [f.result() for f in [pool.submit(_run, "w-a"), pool.submit(_run, "w-b")]]
            claimed = [r for r in results if r is not None]
            assert len(claimed) == 1  # exactly one winner; the loser gets None
            assert _count(engine, f"SELECT count(*) FROM {_JOBS} WHERE status='CLAIMED'") == 1  # noqa: S608
        finally:
            engine.dispose()


def test_multiple_workers_claim_distinct_jobs(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=4)
        try:

            def _run(worker: str) -> Any:
                eng = _engine(url)
                try:
                    return _claim(eng, plan, worker)
                finally:
                    eng.dispose()

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = [f.result() for f in [pool.submit(_run, f"w-{i}") for i in range(4)]]
            claimed = [r for r in results if r is not None]
            assert len(claimed) == 4
            assert len({r.job_id for r in claimed}) == 4  # all DISTINCT jobs
            assert len({r.claimed_by for r in claimed}) == 4
        finally:
            engine.dispose()


def test_skip_locked_passes_over_an_independently_locked_row(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=2)
        try:
            with engine.connect() as c:
                ordered = [
                    str(r[0])
                    for r in c.execute(
                        text(f"SELECT id FROM {_JOBS} ORDER BY created_at, id")  # noqa: S608
                    ).all()
                ]
            first, second = ordered[0], ordered[1]
            holder = _engine(url)
            try:
                hc = holder.connect()
                ht = hc.begin()
                try:
                    hc.execute(
                        text(f"SELECT id FROM {_JOBS} WHERE id=:i FOR UPDATE"),  # noqa: S608
                        {"i": first},
                    )
                    # the claim must SKIP the locked first row and take the second, not block.
                    got = _claim(engine, plan, "w-skip")
                    assert got is not None and got.job_id == second
                finally:
                    ht.rollback()
                    hc.close()
            finally:
                holder.dispose()
        finally:
            engine.dispose()


def test_claim_order_is_created_at_then_id(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=5)
        try:
            with engine.connect() as c:
                expected = [
                    str(r[0])
                    for r in c.execute(
                        text(f"SELECT id FROM {_JOBS} ORDER BY created_at, id")  # noqa: S608
                    ).all()
                ]
            got = [_claim(engine, plan, f"w{i}").job_id for i in range(5)]
            assert got == expected
            assert _claim(engine, plan, "w-last") is None  # G: empty queue -> None
        finally:
            engine.dispose()


def test_empty_queue_returns_none(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=0)  # no jobs enqueued
        try:
            assert _claim(engine, plan, "w-empty") is None
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# G9/G11/G12: rollback, wrong worker, invalid transitions
# --------------------------------------------------------------------------- #
def test_rollback_releases_an_uncommitted_claim(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=1)
        try:
            conn = engine.connect()
            trans = conn.begin()
            try:
                row = (
                    conn.execute(
                        text("SELECT * FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                        {"h": plan.plan_hash, "w": "w-rollback"},
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "CLAIMED"
            finally:
                trans.rollback()  # never committed
                conn.close()
            # the job is back to PENDING with no claim metadata.
            with engine.connect() as c:
                st, by, at_null = c.execute(
                    text(f"SELECT status, claimed_by, claimed_at IS NULL FROM {_JOBS}")  # noqa: S608
                ).one()
            assert (st, by, at_null) == ("PENDING", None, True)
            # ...and a committed claim persists as CLAIMED.
            got = _claim(engine, plan, "w-commit")
            assert got is not None
            assert _job_state(engine, got.job_id) == ("CLAIMED", "w-commit", False)
        finally:
            engine.dispose()


def test_start_and_release_only_for_the_owning_worker(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=2)
        try:
            got = _claim(engine, plan, "owner-1")
            assert got is not None
            jid = uuid.UUID(got.job_id)
            before = _identity_rows(engine)

            # a different worker can neither start nor release the claim.
            for fn in (JC._start_job_with_trust, JC._release_job_with_trust):
                with pytest.raises(InvalidJobTransitionError):
                    fn(engine, job_id=jid, worker_id="intruder-9")
            assert _job_state(engine, got.job_id) == ("CLAIMED", "owner-1", False)

            # the owner may start: CLAIMED -> RUNNING, claim metadata preserved.
            started = JC._start_job_with_trust(engine, job_id=jid, worker_id="owner-1")
            assert started.status == "RUNNING" and started.claimed_by == "owner-1"
            assert _job_state(engine, got.job_id) == ("RUNNING", "owner-1", False)

            # RUNNING may not be released or re-started in F4 (only CLAIMED can transition).
            for fn in (JC._start_job_with_trust, JC._release_job_with_trust):
                with pytest.raises(InvalidJobTransitionError):
                    fn(engine, job_id=jid, worker_id="owner-1")
            assert _job_state(engine, got.job_id) == ("RUNNING", "owner-1", False)

            # a second job: the owner releases it back to PENDING, clearing both claim fields.
            other = _claim(engine, plan, "owner-2")
            assert other is not None
            released = JC._release_job_with_trust(
                engine, job_id=uuid.UUID(other.job_id), worker_id="owner-2"
            )
            assert released.status == "PENDING" and released.claimed_by is None
            assert _job_state(engine, other.job_id) == ("PENDING", None, True)

            # scientific identity never changed across any transition.
            assert _identity_rows(engine) == before
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    ("status", "claimed_by", "claimed_at"),
    [
        ("SUCCEEDED", "'w'", "now()"),
        ("FAILED", "'w'", "now()"),
        ("CANCELLED", "'w'", "now()"),
        ("CLAIMED", "NULL", "NULL"),  # CLAIMED without claim metadata
        ("PENDING", "'w'", "now()"),  # PENDING carrying claim metadata
        ("RUNNING", "'w'", "now()"),  # PENDING -> RUNNING skips CLAIMED
    ],
)
def test_invalid_transitions_are_rejected_without_mutation(
    isolated_pg_base_url: str, tmp_path: Path, status: str, claimed_by: str, claimed_at: str
) -> None:
    """Even a privileged direct UPDATE cannot bypass the transition guard, and nothing changes."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=1)
        try:
            with engine.connect() as c:
                jid = str(c.execute(text(f"SELECT id FROM {_JOBS}")).scalar_one())  # noqa: S608
            before = _job_state(engine, jid)
            assert before == ("PENDING", None, True)
            with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
                c.execute(text("SET ROLE minos_admin"))
                c.execute(
                    text(
                        f"UPDATE {_JOBS} SET status='{status}', "  # noqa: S608
                        f"claimed_by={claimed_by}, claimed_at={claimed_at} WHERE id=:i"
                    ),
                    {"i": jid},
                )
            assert getattr(ei.value.orig, "sqlstate", "") in {"MN010", "MN011", "MN012"}
            assert _job_state(engine, jid) == before  # no mutation
        finally:
            engine.dispose()


def test_job_deletion_still_rejected(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=1)
        try:
            with engine.connect() as c, c.begin(), pytest.raises(Exception):  # noqa: B017, PT011
                c.execute(text("SET ROLE minos_admin"))
                c.execute(text(f"DELETE FROM {_JOBS}"))  # noqa: S608
            assert _count(engine, f"SELECT count(*) FROM {_JOBS}") == 1  # noqa: S608
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# G13: commit ambiguity is typed and never retried
# --------------------------------------------------------------------------- #
def test_commit_ambiguity_is_typed_and_not_retried(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=2)
        try:
            calls = {"n": 0}

            def _raising_commit(_trans: Any) -> None:
                calls["n"] += 1
                raise AmbiguousClaimCommitError("simulated ambiguous COMMIT")

            monkeypatch.setattr(JC, "_commit_or_ambiguous", _raising_commit)
            with pytest.raises(AmbiguousClaimCommitError):
                _claim(engine, plan, "w-ambiguous")
            assert calls["n"] == 1  # exactly one attempt: NO automatic retry
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# accepted boundary guards + G14/G15/G16
# --------------------------------------------------------------------------- #
def test_wrong_identity_and_wrong_revision_rejected(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "not_the_operational_store") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=1)
        engine.dispose()
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        with pytest.raises(OperationalDatabaseIdentityError):
            claim_next_accepted_job(worker_id="w1")
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _L2F)  # 0006, NOT the required 0007
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        with pytest.raises(PlanRevisionError):
            claim_next_accepted_job(worker_id="w1")


def test_missing_plan_graph_fails_closed(isolated_pg_base_url: str) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        alembic_upgrade(url, _F4)  # no plan graph persisted at all
        engine = _engine(url)
        try:
            with pytest.raises((JobPlanMissingError, HV.HarnessGraphError)):
                _claim(engine, plan, "w1")
        finally:
            engine.dispose()


@pytest.mark.parametrize(("spec", "train"), [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)])
def test_uneven_non75_plans_claim_without_fixed_counts(
    isolated_pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], train: int
) -> None:
    plan = _synthetic_plan(spec)
    assert plan.logical_job_count == train * len(_CS.configs)  # derived, never a constant
    n = min(6, plan.logical_job_count)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=n)
        try:
            claimed = [_claim(engine, plan, f"w{i}") for i in range(n)]
            assert all(c is not None for c in claimed)
            assert len({c.job_id for c in claimed}) == n
            assert _claim(engine, plan, "w-extra") is None
        finally:
            engine.dispose()


def test_verifier_accepts_mixed_pending_claimed_running_and_rejects_malformed(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        engine = _prepare(url, plan, _provisioned_root(tmp_path), jobs=5)
        try:
            # a mixed but VALID subset: 1 RUNNING, 1 CLAIMED, 3 PENDING.
            a = _claim(engine, plan, "w-run")
            assert a is not None
            JC._start_job_with_trust(engine, job_id=uuid.UUID(a.job_id), worker_id="w-run")
            b = _claim(engine, plan, "w-claim")
            assert b is not None
            r = HV._verify_experiment_harness_with_trust(engine, plan, _CS)
            assert r.status == HV.STATUS_PASS, r.failures
            assert r.checks["job_status_claim_consistency"] is True
            assert r.persisted_job_count == 5

            # now force a malformed state past the guard by dropping it, then verify rejection.
            with engine.connect() as c, c.begin():
                c.execute(text("SET ROLE minos_admin"))
                c.execute(
                    text(f"ALTER TABLE {_JOBS} DISABLE TRIGGER trg_l2f_jobs_transition_guard")
                )
                c.execute(
                    text(f"UPDATE {_JOBS} SET claimed_by=NULL WHERE id=:i"),  # noqa: S608
                    {"i": b.job_id},
                )
                c.execute(text(f"ALTER TABLE {_JOBS} ENABLE TRIGGER trg_l2f_jobs_transition_guard"))
            bad = HV._verify_experiment_harness_with_trust(engine, plan, _CS)
            assert bad.status == HV.STATUS_FAIL
            assert "job_status_claim_consistency" in bad.failures
        finally:
            engine.dispose()


def test_legacy_and_f3c_data_unchanged_by_claiming(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f4") as url:
        root = _provisioned_root(tmp_path)
        engine = _prepare(url, plan, root, jobs=4)
        try:
            legacy_before = {
                t: _count(engine, f"SELECT count(*) FROM {t}")  # noqa: S608
                for t in ("profiling.profiles", "experiments.jobs", "experiments.results")
            }
            f3c_before = {
                t: _count(engine, f"SELECT count(*) FROM experiments.{t}")  # noqa: S608
                for t in (
                    "l2f_experiment_plans",
                    "l2f_experiment_plan_members",
                    "l2f_config_payloads",
                    "l2f_experiment_plan_configs",
                )
            }
            files_before = {f.name: f.read_bytes() for f in _artifact_files(root)}

            got = _claim(engine, plan, "w-x")
            assert got is not None
            JC._start_job_with_trust(engine, job_id=uuid.UUID(got.job_id), worker_id="w-x")
            other = _claim(engine, plan, "w-y")
            assert other is not None
            JC._release_job_with_trust(engine, job_id=uuid.UUID(other.job_id), worker_id="w-y")

            for t, n in legacy_before.items():
                assert _count(engine, f"SELECT count(*) FROM {t}") == n  # noqa: S608
            for t, n in f3c_before.items():
                assert _count(engine, f"SELECT count(*) FROM experiments.{t}") == n  # noqa: S608
            assert {f.name: f.read_bytes() for f in _artifact_files(root)} == files_before
        finally:
            engine.dispose()
