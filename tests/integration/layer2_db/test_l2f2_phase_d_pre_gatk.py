"""A scratch Phase-D job reaches the exact pre-GATK prepared state, and stops there.

This is the proof that the TRAIN-only rejection in the shared byte verifier is gone. Before the
correction, a Phase-D job resolved cleanly out of the database — ``0021``'s resolver returns
``partition = 'validation'`` by design — and was then refused by the verifier it was handed to,
so the campaign could never start. Here the same job walks the whole runner sequence:

    bootstrap → claim ownership → Phase-D resolver → validation partition gate →
    input byte verification → CONFIG artifact verification → logical invocation

and the test ends there. ``GatkRunner.run`` is never called, by a fake or otherwise: a
FakeGatkRunner returning a green result would prove that a stub can return, not that a Phase-D
job can be prepared. Nothing here writes an execution result, an execution failure or an
evaluation, and every one of those counts is asserted to be zero afterwards.

The provisioned bytes are written first and the campaign metadata records THEIR digests, so the
verifier's PASS is a real comparison rather than one invented constant matching another. No truth
file is created anywhere under the scratch dataset root, so the pre-GATK path has no truth bytes
available to open even by mistake.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.validation_members import build_validation_schedule
from minos_engine.storage.l2f2_runner import _resolve_prepared
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
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f2_operational_seed import (
    provision_scratch_dataset_root,
    scratch_root_under_minos,
    seed_operational_store,
)
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
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
_WORKER = "minos-worker-pre-gatk-0001"


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
    requested, effective_root = scratch_root_under_minos(
        "pre_gatk_", fallback=tmp_path_factory.mktemp("minos_scratch")
    )
    print(f"scratch requested: {requested}")
    print(f"scratch realpath : {requested.resolve()}")
    print(f"effective root   : {effective_root}")
    assert requested.resolve().is_relative_to(effective_root.resolve())
    assert not requested.resolve().is_relative_to(Path(__file__).resolve().parents[3])
    try:
        yield requested
    finally:
        shutil.rmtree(requested, ignore_errors=True)
        with contextlib.suppress(OSError):
            requested.parent.rmdir()


class _Campaign:
    def __init__(self, target: Any, root: Path, dataset_root: DatasetRoot) -> None:
        self.target = target
        self.root = root
        self.dataset_root = dataset_root

    def outcome_counts(self) -> dict[str, int]:
        with self.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))

            def n(sql: str) -> int:
                return int(conn.execute(text(sql)).scalar_one())

            return {
                "execution_results": n("SELECT count(*) FROM experiments.l2f_execution_results"),
                "execution_failures": n("SELECT count(*) FROM experiments.l2f_execution_failures"),
                "evaluations": n("SELECT count(*) FROM evaluation.l2f_evaluation_results"),
                "jobs": n("SELECT count(*) FROM experiments.l2f_experiment_jobs"),
                "claimed": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs  WHERE status = 'CLAIMED'"
                ),
                "pending": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs  WHERE status = 'PENDING'"
                ),
            }


@pytest.fixture
def campaign(isolated_pg_base_url: str, scratch_root: Path, authority: Any) -> Any:
    """A fully activated scratch Phase-D campaign: 40 PENDING jobs and provisioned inputs."""
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
        try:
            # 1. provision the inputs FIRST, then record their real digests upstream.
            dataset_root_path = root / "datasets"
            digests = provision_scratch_dataset_root(dataset_root_path, authority.schedule.members)
            with operational.connect() as conn, conn.begin():
                seed_operational_store(conn, field_overrides=digests, profile_overrides={})
            with baseline.connect() as conn, conn.begin():
                seed_source_configs(
                    conn,
                    authority.ordered_config_hashes,
                    authority.parameter_space_hash,
                    config_root=root / "baseline_configs",
                )
            # 2. the accepted seams, in order.
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
            truth_root = root / "validation_truth"
            seed_truth_bundles(truth_root, authority.schedule.members)
            _activate_truth_with_trust(
                target=target,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                dataset_root=truth_root,
                expected_database=_TARGET_DB,
                expected_revision=_TARGET_REVISION,
            )
            _materialize_with_trust(
                target=target,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                expected_database=_TARGET_DB,
                expected_revision=_TARGET_REVISION,
            )
            yield _Campaign(target, root, DatasetRoot(root=dataset_root_path))
        finally:
            for eng in (operational, baseline, target):
                eng.dispose()


# --------------------------------------------------------------------------------------------
# THE proof
# --------------------------------------------------------------------------------------------
def test_a_claimed_phase_d_job_reaches_the_prepared_state(campaign: Any) -> None:
    before = campaign.outcome_counts()
    assert before["jobs"] == 40
    assert before["pending"] == 40
    assert before["claimed"] == 0

    with campaign.target.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        # the ARGUMENT-FREE bootstrap: the runner learns which plan it may execute.
        bootstrap = conn.execute(
            text(
                "SELECT plan_hash, execution_environment_hash "
                "  FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"
            )
        ).one()
        assert (str(bootstrap[0]), str(bootstrap[1])) == (_PLAN_HASH, _ENVIRONMENT)

        # the ACCEPTED claim interface, not a hand-written UPDATE.
        claimed = (
            conn.execute(
                text("SELECT job_id, job_key FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": _PLAN_HASH, "w": _WORKER},
            )
            .mappings()
            .one()
        )
        job_id, job_key = str(claimed["job_id"]), str(claimed["job_key"])

        # the Phase-D resolver, the validation partition gate, byte verification, CONFIG
        # verification and the logical invocation — the whole pre-GATK sequence.
        prepared = _resolve_prepared(
            conn,
            phase="PHASE_D",
            plan_hash=_PLAN_HASH,
            job_id=job_id,
            job_key=job_key,
            worker_id=_WORKER,
            dataset_root=campaign.dataset_root,
            gatk_executable_sha256="a" * 64,
            gatk_runtime_bundle_sha256="b" * 64,
            gatk_version="4.5.0.0",
        )

    # the resolved member IS one of the frozen ten VALIDATION members. ExecutionInput carries no
    # partition field — the partition was the GATE, checked before these bytes were hashed.
    frozen = {m.dataset_id: m for m in build_validation_schedule().members}
    assert prepared.inputs.dataset_id in frozen
    member = frozen[prepared.inputs.dataset_id]
    assert prepared.inputs.round_id == member.round_id
    assert prepared.inputs.chromosome == member.chromosome
    assert prepared.paths.bam.is_file()
    assert prepared.paths.reference.is_file()
    assert prepared.config.config_hash in set(
        # the four frozen finalists, resolved through the plan graph
        conn_hashes := {
            "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
            "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
            "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
            "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
        }
    )
    assert len(conn_hashes) == 4
    assert prepared.invocation is not None

    # ---- and it stopped there. No runner.run, and nothing written. --------------------------
    after = campaign.outcome_counts()
    assert after["execution_results"] == 0
    assert after["execution_failures"] == 0
    assert after["evaluations"] == 0
    assert after["jobs"] == 40
    assert after["claimed"] == 1
    assert after["pending"] == 39


def test_the_proof_never_reaches_the_gatk_runner() -> None:
    """A source-level guarantee: this module cannot call run(), by a fake or otherwise."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "run" not in called
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        f"{n.module}.{a.name}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
        for a in n.names
    }
    assert not any("FakeGatkRunner" in name for name in names)
    assert not any("SubprocessGatkRunner" in name for name in names)


def test_the_scratch_dataset_root_holds_no_truth(campaign: Any) -> None:
    """The pre-GATK path has no truth bytes available under the dataset root, by construction."""
    root = campaign.dataset_root.root
    for pattern in ("truth.vcf.gz", "truth.vcf.gz.tbi", "mutations.vcf.gz"):
        assert list(root.rglob(pattern)) == []
