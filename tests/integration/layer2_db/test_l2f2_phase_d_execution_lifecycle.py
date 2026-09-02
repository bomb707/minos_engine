"""The whole Phase-D execution lifecycle, on the VALIDATION store, in scratch.

The shared execution core authorized the TRAIN baseline on every step — claim, prepare, start,
release, fail, complete — while the Phase-D public entry and the ``0021`` resolver were already
validation-aware. Phase D could be activated and never executed.

This drives the real core against a scratch validation database and proves each step now
authorizes the store the job actually lives in:

    PENDING -> CLAIMED -> RUNNING -> SUCCEEDED      (claim, prepare, start, complete)
    PENDING -> CLAIMED -> PENDING                    (release, on a pre-RUNNING refusal)
    PENDING -> CLAIMED -> RUNNING -> FAILED          (fail, after a deterministic GATK failure)

``FakeGatkRunner`` is used deliberately and its output is NOT scientific evidence. The question
here is whether the execution-control lifecycle can reach and write the validation store; what
GATK would actually call is a different question, answered by the real campaign under the real
runtime. Nothing here touches the real database.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.storage.l2f2_runner import _execute_l2f2_job, _PhaseBRunnerAuthority
from minos_engine.storage.l2f2_validation_activate import (
    _activate_truth_with_trust,
    _materialize_with_trust,
)
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
    _prepare_with_trust,
)
from minos_engine.storage.l2f2_validation_provision import (
    OPERATIONAL_REVISION,
    _provision_with_trust,
)
from minos_engine.storage.l2f_execution_inputs import DatasetRoot
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_result_publisher import ResultArtifactPublisher
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f2_operational_seed import (
    provision_scratch_dataset_root,
    scratch_root_under_minos,
    seed_operational_store,
)
from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
from tests.integration.layer2_db.l2f2_validation_seed import (
    seed_source_configs,
    seed_truth_bundles,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

_OPERATIONAL_DB = "minos_engine_db"
_BASELINE_DB = "minos_l2f2_baseline"
_TARGET_DB = "minos_l2f2_validation"
_BASELINE_REVISION = "0020_l2f2_phase_c_execution"
_TARGET_REVISION = "0024_l2f2_phase_d_anchor"
_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"
_RUNNER_ROLE = "ci_phase_d_runner_svc"


@pytest.fixture(scope="module")
def authority() -> Any:
    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


@pytest.fixture(scope="module")
def scratch_root(tmp_path_factory: Any) -> Any:
    from tests.minos_scratch import prune_scratch_parent

    scratch, effective_root = scratch_root_under_minos(
        "phase_d_exec_", fallback=tmp_path_factory.mktemp("minos_scratch")
    )
    assert scratch.resolve().is_relative_to(effective_root.resolve())
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        prune_scratch_parent(scratch)


class _Campaign:
    """An activated scratch Phase-D campaign plus a least-privilege runner login."""

    def __init__(self, admin: Any, service: Any, root: Path, dataset_root: DatasetRoot) -> None:
        self.admin = admin
        self.service = service
        self.root = root
        self.dataset_root = dataset_root
        import os

        result_root = root / "result_artifacts"
        result_root.mkdir(parents=True, exist_ok=True)
        os.chmod(result_root, 0o2750)  # the accepted content-addressed store's root contract
        self.publisher = ResultArtifactPublisher(result_root)
        self.work_root = root / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.runner_authority = _PhaseBRunnerAuthority(plan_hash=_PLAN_HASH, phase="PHASE_D")

    def execute(self, *, runner: Any, worker_id: str, authority: Any = None) -> Any:
        return _execute_l2f2_job(
            self.service,
            authority or self.runner_authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=self.dataset_root,
            publisher=self.publisher,
            work_root=self.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )

    def counts(self) -> dict[str, int]:
        with self.admin.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))

            def n(sql: str) -> int:
                return int(conn.execute(text(sql)).scalar_one())

            return {
                "PENDING": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status='PENDING'"
                ),
                "CLAIMED": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status='CLAIMED'"
                ),
                "RUNNING": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status='RUNNING'"
                ),
                "SUCCEEDED": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status='SUCCEEDED'"
                ),
                "FAILED": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status='FAILED'"
                ),
                "results": n("SELECT count(*) FROM experiments.l2f_execution_results"),
                "failures": n("SELECT count(*) FROM experiments.l2f_execution_failures"),
            }


@pytest.fixture
def campaign(isolated_pg_base_url: str, scratch_root: Path, authority: Any) -> Any:
    import tempfile

    with (
        scratch_database(isolated_pg_base_url, _OPERATIONAL_DB) as operational_url,
        scratch_database(isolated_pg_base_url, _BASELINE_DB) as baseline_url,
        scratch_database(isolated_pg_base_url, _TARGET_DB) as target_url,
    ):
        alembic_upgrade(operational_url, OPERATIONAL_REVISION)
        alembic_upgrade(baseline_url, _BASELINE_REVISION)
        alembic_upgrade(target_url, _TARGET_REVISION)
        operational, baseline, target = (
            _engine(operational_url),
            _engine(baseline_url),
            _engine(target_url),
        )
        root = Path(tempfile.mkdtemp(prefix="run_", dir=scratch_root))
        service = None
        try:
            digests = provision_scratch_dataset_root(root / "datasets", authority.schedule.members)
            with operational.connect() as conn, conn.begin():
                seed_operational_store(conn, field_overrides=digests)
            with baseline.connect() as conn, conn.begin():
                seed_source_configs(
                    conn,
                    authority.ordered_config_hashes,
                    authority.parameter_space_hash,
                    config_root=root / "baseline_configs",
                )
            _provision_with_trust(
                source=operational,
                target=target,
                expected_source_database=_OPERATIONAL_DB,
                expected_source_revision=OPERATIONAL_REVISION,
                expected_target_database=_TARGET_DB,
                expected_target_revision=_TARGET_REVISION,
            )
            _prepare_with_trust(
                target=target,
                baseline=baseline,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                config_artifact_root=root / "target_configs",
                expected_database=_TARGET_DB,
                expected_revision=_TARGET_REVISION,
            )
            seed_truth_bundles(root / "truth", authority.schedule.members)
            _activate_truth_with_trust(
                target=target,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                dataset_root=root / "truth",
                expected_database=_TARGET_DB,
                expected_revision=_TARGET_REVISION,
            )
            _materialize_with_trust(
                target=target,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                expected_database=_TARGET_DB,
                expected_revision=_TARGET_REVISION,
            )
            # a LOGIN whose only MINOS membership is minos_runner — the real runner principal.
            parsed = make_url(target_url)
            with target.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_RUNNER_ROLE}"))
                conn.execute(
                    text(
                        f"CREATE ROLE {_RUNNER_ROLE} LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOBYPASSRLS INHERIT"
                    )
                )
                conn.execute(
                    text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_RUNNER_ROLE}')
                )
                conn.execute(text(f"GRANT minos_runner TO {_RUNNER_ROLE}"))
            service = create_engine(parsed.set(username=_RUNNER_ROLE, password=""))
            yield _Campaign(target, service, root, DatasetRoot(root=root / "datasets"))
        finally:
            if service is not None:
                service.dispose()
            with contextlib.suppress(Exception), target.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_RUNNER_ROLE}"))
            for eng in (operational, baseline, target):
                eng.dispose()


# --------------------------------------------------------------------------------------------
# success: claim -> prepare -> start -> complete, all on the validation store
# --------------------------------------------------------------------------------------------
def test_a_phase_d_job_completes_through_the_validation_store(campaign: Any) -> None:
    before = campaign.counts()
    assert (before["PENDING"], before["SUCCEEDED"], before["results"]) == (40, 0, 0)

    dispatched = campaign.execute(runner=FakeGatkRunner(), worker_id="ci-phase-d-success-0001")

    assert dispatched is not None
    assert dispatched.status == "SUCCEEDED"
    after = campaign.counts()
    assert after["SUCCEEDED"] == 1
    assert after["PENDING"] == 39
    assert after["CLAIMED"] == 0 and after["RUNNING"] == 0 and after["FAILED"] == 0
    assert after["results"] == 1 and after["failures"] == 0

    # the result artifacts were registered in the VALIDATION store, not the baseline.
    with campaign.admin.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        kinds = {
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT a.media_type FROM experiments.l2f_execution_results r "
                    "  JOIN catalog.artifacts a "
                    "    ON a.id IN (r.vcf_artifact_id, r.result_manifest_artifact_id)"
                )
            )
        }
    assert kinds, "no result artifact was registered"


# --------------------------------------------------------------------------------------------
# release: a pre-RUNNING refusal returns the job to PENDING
# --------------------------------------------------------------------------------------------
def test_a_pre_running_refusal_releases_the_job_on_the_validation_store(campaign: Any) -> None:
    """The recovery path must reach the same store the claim did, or the job stays CLAIMED."""
    # a dataset root with nothing in it: preparation refuses AFTER the claim, BEFORE RUNNING.
    empty = campaign.root / "empty_datasets"
    (empty / "practice").mkdir(parents=True, exist_ok=True)
    (empty / "reference").mkdir(parents=True, exist_ok=True)
    campaign.dataset_root = DatasetRoot(root=empty)

    with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal; the state is what matters
        campaign.execute(runner=FakeGatkRunner(), worker_id="ci-phase-d-release-0001")

    after = campaign.counts()
    assert after["PENDING"] == 40, "the job was not released back to PENDING"
    assert after["CLAIMED"] == 0
    assert after["RUNNING"] == 0
    assert after["FAILED"] == 0
    assert after["results"] == 0 and after["failures"] == 0


# --------------------------------------------------------------------------------------------
# failure: a RUNNING job fails durably on the validation store
# --------------------------------------------------------------------------------------------
def test_a_running_phase_d_job_fails_durably_on_the_validation_store(campaign: Any) -> None:
    dispatched = campaign.execute(
        runner=FakeGatkRunner(exit_code=1, write_output=False, stderr_sha256="e" * 64),
        worker_id="ci-phase-d-failure-0001",
    )
    assert dispatched is not None
    assert dispatched.status == "FAILED"

    after = campaign.counts()
    assert after["FAILED"] == 1
    assert after["PENDING"] == 39
    assert after["CLAIMED"] == 0 and after["RUNNING"] == 0 and after["SUCCEEDED"] == 0
    assert after["failures"] == 1, "the failure was not persisted"
    assert after["results"] == 0, "a failure must never write a success row"


# --------------------------------------------------------------------------------------------
# the store is fixed by phase
# --------------------------------------------------------------------------------------------
def test_a_train_phase_cannot_execute_against_the_validation_store(campaign: Any) -> None:
    """The correction is not "either store is acceptable" — it is "store is fixed by phase"."""
    for phase in ("PHASE_A", "PHASE_B", "PHASE_C"):
        with pytest.raises(Exception, match="minos_l2f2_baseline|revision"):
            campaign.execute(
                runner=FakeGatkRunner(),
                worker_id="ci-phase-d-wrongphase-01",
                authority=_PhaseBRunnerAuthority(plan_hash=_PLAN_HASH, phase=phase),
            )
    after = campaign.counts()
    assert after["PENDING"] == 40, "a refused phase must not have consumed a job"
    assert after["results"] == 0 and after["failures"] == 0


def test_an_unknown_phase_is_refused_before_any_job_is_touched(campaign: Any) -> None:
    with pytest.raises(Exception, match="no L2-F2 execution store is accepted"):
        campaign.execute(
            runner=FakeGatkRunner(),
            worker_id="ci-phase-d-unknown-0001",
            authority=_PhaseBRunnerAuthority(plan_hash=_PLAN_HASH, phase="PHASE_E"),
        )
    after = campaign.counts()
    assert after["PENDING"] == 40
    assert after["CLAIMED"] == 0 and after["results"] == 0 and after["failures"] == 0


def test_the_old_baseline_bound_core_would_have_failed_here(
    campaign: Any, monkeypatch: Any
) -> None:
    """The regression this correction exists to prevent, pinned.

    Routing PHASE_D back to the baseline authorizer reproduces the original defect exactly: the
    campaign is perfectly valid, the bootstrap resolves, and the very first lifecycle connection
    is refused because it demands ``minos_l2f2_baseline``. This proves the phase routing is what
    makes Phase-D execution possible, rather than something that merely happens to be present.
    """
    from minos_engine.storage import l2f2_runner

    monkeypatch.setitem(
        l2f2_runner._STORE_AUTHORIZER_BY_PHASE,
        "PHASE_D",
        l2f2_runner.authorize_baseline_runner_connection,
    )
    with pytest.raises(Exception, match="minos_l2f2_baseline|revision"):
        campaign.execute(runner=FakeGatkRunner(), worker_id="ci-phase-d-oldcore-001")

    after = campaign.counts()
    assert after["PENDING"] == 40, "the refused run must not have consumed a job"
    assert after["results"] == 0 and after["failures"] == 0

    # and with the correction restored, the identical campaign completes.
    monkeypatch.undo()
    dispatched = campaign.execute(runner=FakeGatkRunner(), worker_id="ci-phase-d-newcore-001")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    assert campaign.counts()["SUCCEEDED"] == 1
