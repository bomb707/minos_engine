"""Migration ``0014`` — a failed GATK execution carries its own elapsed runtime.

The frozen objective takes ``gatk_runtime_ms`` from EVERY decided observation and uses the mean
as its tie-break. A success has always carried its runtime; a failure carried none, so turning a
failed Phase-A job into an observation meant inventing a duration. These controls prove the
measurement is real, mandatory and produced by the runner's own clock — and that nothing else
about the least-privilege failure path moved.

No GATK, no hap.py, no score: the runner is ``FakeGatkRunner`` through the private test seam, and
every write still goes through the narrow ``SECURITY DEFINER`` writer under a
``minos_runner``-only LOGIN.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from minos_engine.storage.l2f2_runner import (
    _execute_l2f2_job,
    _fail,
)
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import (
    l2f2 as _l2f2_fixture,
)
from tests.integration.layer2_db.test_l2f2_runner_boundary import (
    service as _service_fixture,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_baseline"
_PRIOR = "0013_l2f2_upstream_score_oracle"
_HEAD = "0014_l2f2_exec_failure_runtime"

_FAILURES = "experiments.l2f_execution_failures"
_RESULTS = "experiments.l2f_execution_results"
_RUNTIME_CHECK = "ck_l2f_exec_failures_runtime_nonneg"
_OLD_SIG = "experiments.minos_l2f_fail_job(text, uuid, text, text, integer, text)"
_NEW_SIG = "experiments.minos_l2f_fail_job(text, uuid, text, text, integer, text, bigint)"

_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _signatures(engine: Any) -> list[str]:
    with engine.connect() as conn:
        return sorted(
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
                    "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                    " WHERE n.nspname = 'experiments' AND p.proname = 'minos_l2f_fail_job'"
                )
            )
        )


def _runtime_column(engine: Any) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                " WHERE table_schema='experiments' AND table_name='l2f_execution_failures' "
                "   AND column_name='runtime_ms'"
            )
        ).scalar_one_or_none()


def _runtime_position(state: Any) -> int | None:
    for column in state["relations"][_FAILURES]["columns"]:
        if column["name"] == "runtime_ms":
            return int(column["position"])
    return None


def _without_failure_column_positions(state: Any) -> str:
    """The structural state with the failure ledger's column ORDINALS blanked out."""
    import copy

    stripped = copy.deepcopy(state)
    for column in stripped["relations"][_FAILURES]["columns"]:
        column["position"] = None
    return json.dumps(stripped, sort_keys=True, default=str)


def _failure_rows(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(f"SELECT job_key, failure_code, runtime_ms FROM {_FAILURES} ORDER BY job_key")  # noqa: S608
            ).mappings()
        ]


# --------------------------------------------------------------------------- #
# migration lifecycle
# --------------------------------------------------------------------------- #
def test_empty_lifecycle_0013_0014_0013_0014(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0013 = _state(engine)
            assert _runtime_column(engine) is None
            assert _signatures(engine) == [
                "p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
                "p_exit_code integer, p_stderr_sha256 text"
            ]
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            at_0014 = _state(engine)
            assert _revision(engine) == _HEAD
            assert _runtime_column(engine) == "NO", "the measurement is mandatory"
            # the narrower writer is GONE: no caller can persist a failure without a runtime.
            assert _signatures(engine) == [
                "p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
                "p_exit_code integer, p_stderr_sha256 text, p_runtime_ms bigint"
            ]
            with engine.connect() as conn:
                definition = conn.execute(
                    text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                    {"n": _RUNTIME_CHECK},
                ).scalar_one()
            assert "runtime_ms >= 0" in str(definition)
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _state(engine) == at_0013, "downgrade did not restore 0013"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            # add -> drop -> add leaves the column at a later attnum. That ordinal is PostgreSQL
            # bookkeeping with no bearing on the contract, and it is the ONLY tolerated
            # difference — everything else must match the first upgrade exactly.
            again = _state(engine)
            assert _runtime_position(again) != _runtime_position(at_0014)
            assert _without_failure_column_positions(again) == _without_failure_column_positions(
                at_0014
            ), "re-upgrade did not restore 0014"
        finally:
            engine.dispose()


def test_0014_changes_only_the_failure_ledger_and_its_writer(isolated_pg_base_url: str) -> None:
    """Additive and narrow: one column, one CHECK, one widened writer — no role or grant moves."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            before = _state(engine)
        finally:
            engine.dispose()
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            after = _state(engine)
        finally:
            engine.dispose()

    for section in ("roles", "role_memberships", "schema_security", "default_acls", "triggers"):
        assert before.get(section) == after.get(section), f"0014 altered {section!r}"
    changed = sorted(
        name
        for name in set(before["relations"]) | set(after["relations"])
        if json.dumps(before["relations"].get(name), sort_keys=True, default=str)
        != json.dumps(after["relations"].get(name), sort_keys=True, default=str)
    )
    assert changed == [_FAILURES], changed

    # exactly one function differs, and it is the failure writer — with the SAME privilege shape.
    def _by_name(state: Any) -> dict[str, Any]:
        return {
            f"{row['schema']}.{row['name']}({row['identity_arguments']})": row
            for row in state["functions"]
        }

    moved = sorted(
        name
        for name in set(_by_name(before)) | set(_by_name(after))
        if json.dumps(_by_name(before).get(name), sort_keys=True, default=str)
        != json.dumps(_by_name(after).get(name), sort_keys=True, default=str)
    )
    assert moved == [
        "experiments.minos_l2f_fail_job(p_plan_hash text, p_job_id uuid, p_worker_id text, "
        "p_failure_code text, p_exit_code integer, p_stderr_sha256 text)",
        "experiments.minos_l2f_fail_job(p_plan_hash text, p_job_id uuid, p_worker_id text, "
        "p_failure_code text, p_exit_code integer, p_stderr_sha256 text, p_runtime_ms bigint)",
    ], moved
    old = _by_name(before)[moved[0]]
    new = _by_name(after)[moved[1]]
    # a SECURITY DEFINER function executes as its OWNER: the widened writer must keep 0008's
    # minos_admin ownership, not acquire the migration login's (superuser) authority.
    assert new["owner"] == "minos_admin"
    assert new["security_definer"] is True
    for field in ("owner", "security_definer", "volatility", "language", "strict", "parallel"):
        assert old[field] == new[field], field
    # the same privilege shape, on the new signature: the runner may EXECUTE and nothing more.
    assert {k: v for k, v in old.items() if k.startswith("acl")} == {
        k: v for k, v in new.items() if k.startswith("acl")
    }


def test_the_upgrade_refuses_a_store_that_already_holds_a_failure(
    isolated_pg_base_url: str, service: Any, l2f2: Any
) -> None:
    """A pre-0014 failure row has no authoritative runtime, so 0014 refuses rather than invent one.

    The row is created by the REAL runner at 0014 and the store is then downgraded, which is the
    only way to obtain a genuine failure row that predates the measurement.
    """
    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-runner-legacy",
        runner=FakeGatkRunner(exit_code=9),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        gatk_executable_sha256="0" * 64,
        gatk_runtime_bundle_sha256="1" * 64,
        gatk_version="fake-gatk-4.5.0.0",
    )
    assert dispatched is not None and dispatched.status == "FAILED"
    l2f2.engine.dispose()

    alembic_downgrade(l2f2.url, _PRIOR)
    with pytest.raises(Exception, match="already exist") as excinfo:
        alembic_upgrade(l2f2.url, _HEAD)
    assert "fabricated" in str(excinfo.value)

    engine = _engine(l2f2.url)
    try:
        # refused, and nothing was half-applied.
        assert _revision(engine) == _PRIOR
        assert _runtime_column(engine) is None
        assert (
            int(
                engine.connect().execute(text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one()  # noqa: S608
            )
            == 1
        )
    finally:
        engine.dispose()
    l2f2.engine = _engine(l2f2.url)


# --------------------------------------------------------------------------- #
# the runner's measurement
# --------------------------------------------------------------------------- #
def _run(service: Any, l2f2: Any, runner: FakeGatkRunner, *, worker: str) -> tuple[Any, int]:
    """Execute one job and return (dispatch result, wall-clock bound on the whole call in ms)."""
    started = time.monotonic_ns()
    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id=worker,
        runner=runner,
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        gatk_executable_sha256="0" * 64,
        gatk_runtime_bundle_sha256="1" * 64,
        gatk_version="fake-gatk-4.5.0.0",
    )
    return dispatched, (time.monotonic_ns() - started) // 1_000_000


@pytest.mark.parametrize(
    ("runner", "code"),
    [
        (FakeGatkRunner(exit_code=4), "GATK_NONZERO_EXIT"),
        (FakeGatkRunner(raise_timeout=True), "GATK_TIMEOUT"),
        (FakeGatkRunner(override_bytes=b"not a vcf\n"), "GATK_OUTPUT_INVALID"),
    ],
)
def test_every_bounded_gatk_failure_persists_a_measured_runtime(
    service: Any, l2f2: Any, runner: FakeGatkRunner, code: str
) -> None:
    """Each bounded GATK failure records the attempt's own elapsed monotonic duration."""
    dispatched, bound = _run(service, l2f2, runner, worker=f"ci-{code.lower().replace('_', '-')}")

    assert dispatched is not None
    assert dispatched.status == "FAILED"
    assert dispatched.failure_code == code
    rows = _failure_rows(l2f2.engine)
    assert len(rows) == 1
    persisted = int(rows[0]["runtime_ms"])
    # a real measurement, not a placeholder: non-negative and no longer than the call itself.
    assert 0 <= persisted <= bound, (persisted, bound)
    assert l2f2.count(f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608


def test_a_successful_execution_still_records_the_runner_s_own_runtime(
    service: Any, l2f2: Any
) -> None:
    """0014 does not touch the success path: the result's runtime is still GATK's own."""
    dispatched, _bound = _run(service, l2f2, FakeGatkRunner(runtime_ms=4242), worker="ci-ok")

    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    with l2f2.engine.connect() as conn:
        runtime = conn.execute(
            text(f"SELECT runtime_ms FROM {_RESULTS} WHERE job_id = :j"),  # noqa: S608
            {"j": dispatched.job_id},
        ).scalar_one()
    assert int(runtime) == 4242
    assert _failure_rows(l2f2.engine) == []


def test_a_preparation_failure_records_no_runtime_because_no_attempt_elapsed(
    service: Any, l2f2: Any, tmp_path: Any
) -> None:
    """Before RUNNING there is no attempt to measure, so the job returns to PENDING instead.

    This is why 0014 does not have to invent a runtime for preparation: a failure that never
    started GATK never becomes a failure row at all.
    """
    from minos_engine.storage.l2f_execution_inputs import DatasetRoot

    empty = tmp_path / "empty_datasets"
    (empty / "practice").mkdir(parents=True)
    (empty / "reference").mkdir(parents=True)

    with pytest.raises(Exception):  # noqa: B017, PT011 - any preparation error, before RUNNING
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id="ci-prep",
            runner=FakeGatkRunner(),
            dataset_root=DatasetRoot.from_path(empty),
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            gatk_executable_sha256="0" * 64,
            gatk_runtime_bundle_sha256="1" * 64,
            gatk_version="fake-gatk-4.5.0.0",
        )

    assert _failure_rows(l2f2.engine) == []
    assert l2f2.count(f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert (
        l2f2.count("SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status = 'PENDING'")
        == 2
    ), "a preparation failure must leave the job claimable again"


def test_a_negative_runtime_is_refused_before_it_reaches_the_database(
    service: Any, l2f2: Any
) -> None:
    """The measurement guard is in the runner, not only in the CHECK."""
    from minos_engine.storage.l2f2_runner import L2F2ExecutionError

    with pytest.raises(L2F2ExecutionError, match="not a measurement"):
        _fail(
            service,
            plan_hash=l2f2.authority.plan_hash,
            job_id="00000000-0000-0000-0000-000000000000",
            job_key="k" * 64,
            worker_id="ci-negative",
            failure_code="EXECUTION_ERROR",
            exit_code=None,
            stderr_sha256=None,
            runtime_ms=-1,
        )
    assert _failure_rows(l2f2.engine) == []


def test_the_writer_itself_refuses_a_negative_or_missing_runtime(service: Any, l2f2: Any) -> None:
    """Independently of the runner: the SECURITY DEFINER writer rejects a non-measurement."""
    dispatched, _bound = _run(service, l2f2, FakeGatkRunner(exit_code=3), worker="ci-writer")
    assert dispatched is not None and dispatched.status == "FAILED"

    for runtime in (None, -1):
        with pytest.raises(DatabaseError) as excinfo, service.connect() as conn, conn.begin():
            conn.execute(
                text(
                    "SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, NULL, NULL, :rt)"
                ),
                {
                    "h": l2f2.authority.plan_hash,
                    "j": dispatched.job_id,
                    "w": "ci-writer",
                    "c": "EXECUTION_ERROR",
                    "rt": runtime,
                },
            )
        assert "non-negative elapsed measurement" in str(excinfo.value)


def test_the_runner_principal_still_cannot_write_the_failure_ledger_directly(
    service: Any, l2f2: Any
) -> None:
    """0014 widens a function signature; it grants no table DML anywhere."""
    with service.connect() as conn:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert (
                conn.execute(
                    text("SELECT has_table_privilege('minos_runner', :t, :p)"),
                    {"t": _FAILURES, "p": privilege},
                ).scalar_one()
                is False
            ), privilege
    with pytest.raises(DatabaseError) as excinfo, service.connect() as conn, conn.begin():
        conn.execute(
            text(
                f"INSERT INTO {_FAILURES} (job_id, runtime_ms) "  # noqa: S608
                "VALUES ('00000000-0000-0000-0000-000000000000', 0)"
            )
        )
    assert "permission denied" in str(excinfo.value).lower()


def test_the_recorded_runtime_reaches_the_frozen_observation_unchanged(
    service: Any, l2f2: Any
) -> None:
    """End of the chain: what the runner measured is what a BaselineObservation would carry.

    The Phase-A reader is exercised against the frozen plan elsewhere; here the point is narrower
    and just as load-bearing — the ledger hands back the exact persisted integer, with no
    substitution, rounding or default.
    """
    dispatched, bound = _run(service, l2f2, FakeGatkRunner(exit_code=5), worker="ci-chain")
    assert dispatched is not None and dispatched.status == "FAILED"

    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT f.failure_code, f.runtime_ms FROM {_FAILURES} f "  # noqa: S608
                    " WHERE f.job_id = :j"
                ),
                {"j": dispatched.job_id},
            )
            .mappings()
            .one()
        )
    from minos_engine.baseline.objective import BaselineObservation, classify_failure_code

    observation = BaselineObservation(
        config_hash="a" * 64,
        dataset_id="minos-chr18-028662fb934529d7",
        chromosome="chr18",
        minos_score=None,
        admitted=False,
        failure_code=str(row["failure_code"]),
        gatk_runtime_ms=int(row["runtime_ms"]),
    )
    assert observation.gatk_runtime_ms == int(row["runtime_ms"]) <= bound
    assert observation.minos_score is None, "a failure is never a score of zero"
    assert classify_failure_code(str(row["failure_code"])) == "CANDIDATE_FAILURE"
