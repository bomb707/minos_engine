"""A REAL Phase-A baseline store: the frozen 5x39 plan, its canary, and outcomes on demand.

The expansion boundary and the observation reader both read the frozen Phase-A screen out of the
immutable ledgers, so neither can be tested against a synthetic plan or hand-inserted ledger rows.
This module builds the genuine thing on an EPHEMERAL database: the accepted 50-member TRAIN
upstream closure, the frozen Phase-A subset persisted by the production preparation path, jobs
enqueued by the production enqueue path, executions driven through the least-privilege runner
under a ``minos_runner``-only LOGIN, and evaluations persisted through the production evaluator.

What is NOT real here is deliberately confined to two seams that no production code path can
reach: the GATK runner is ``FakeGatkRunner`` (no variant caller runs, and the bytes it writes are
deterministic), and the upstream score is a recorded ``MinosSubnetOracleResult`` rather than a
live MINOS_SUBNET invocation. Everything between those seams — every plan, index, job key,
privilege, function, trigger and ledger row — is the production one.

Nothing here touches the real baseline store.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from minos_engine.experiments.execution_environment import GatkExecutionEnvironment
from minos_engine.storage.l2f_execution_inputs import DatasetRoot
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_result_publisher import ResultArtifactPublisher
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.test_l2f_plan_store import _engine

BASELINE_DB = "minos_l2f2_baseline"
REQUIRED_REVISION = "0018_l2f2_eval_owner_fix"
_CI_ROLE = "minos_phase_a_ci_svc"
_EVAL_ROLE = "minos_phase_a_eval_svc"

#: the pinned GATK identity the runner records. No GATK runs; these are the fixed test values
#: already used by the runner-boundary suite.
GATK_EXECUTABLE_SHA256 = "0" * 64
GATK_RUNTIME_BUNDLE_SHA256 = "1" * 64
GATK_VERSION = "fake-gatk-4.5.0.0"


#: the runtime identity the deterministic test runner stands in for. It is a real, fully-formed
#: GatkExecutionEnvironment — FakeGatkRunner starts no interpreter and no JVM, so these are fixed
#: test values rather than measurements, and they are never presented as production provenance.
TEST_EXECUTION_ENVIRONMENT = GatkExecutionEnvironment(
    gatk_launcher_sha256="0" * 64,
    gatk_runtime_bundle_sha256="1" * 64,
    gatk_version=GATK_VERSION,
    launcher_python_sha256="2" * 64,
    launcher_python_version="3.12.3",
    java_sha256="3" * 64,
    java_version="17.0.11",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split(dataset_id: str) -> tuple[str, str]:
    """``minos-chr18-028662fb934529d7`` -> ``('028662fb934529d7', 'chr18')``."""
    _prefix, chromosome, round_id = dataset_id.split("-", 2)
    return round_id, chromosome


def _provision_inputs(root: Path, *, round_id: str, chromosome: str) -> None:
    practice = root / "practice" / f"round_{round_id}"
    reference = root / "reference" / chromosome
    practice.mkdir(parents=True, exist_ok=True)
    reference.mkdir(parents=True, exist_ok=True)
    (practice / "input.bam").write_bytes(b"BAM\x01" + round_id.encode())
    (practice / "input.bam.bai").write_bytes(b"BAI\x01" + round_id.encode())
    (reference / f"{chromosome}.fa").write_bytes(b">" + chromosome.encode() + b"\nACGT\n")
    (reference / f"{chromosome}.fa.fai").write_bytes(chromosome.encode() + b"\t4\t5\t4\t5\n")
    (reference / f"{chromosome}.dict").write_text(
        f"@HD\tVN:1.6\n@SQ\tSN:{chromosome}\tLN:4\n", encoding="utf-8"
    )


def _dataset_identity(members: Any, root: Path) -> dict[str, dict[str, Any]]:
    """Provision real input bytes for the frozen members and return their REAL identities."""
    identity: dict[str, dict[str, Any]] = {}
    for member in members:
        round_id, chromosome = _split(member.dataset_id)
        _provision_inputs(root, round_id=round_id, chromosome=chromosome)
        practice = root / "practice" / f"round_{round_id}"
        reference = root / "reference" / chromosome
        identity[member.dataset_id] = {
            "round_id": round_id,
            "chromosome": chromosome,
            "bam_sha256": _sha(practice / "input.bam"),
            "bai_sha256": _sha(practice / "input.bam.bai"),
            "reference_sha256": _sha(reference / f"{chromosome}.fa"),
            "fai_sha256": _sha(reference / f"{chromosome}.fa.fai"),
            "bam_size_bytes": (practice / "input.bam").stat().st_size,
        }
    return identity


@dataclass
class PhaseAEnv:
    """The frozen Phase-A screen on a real, ephemeral baseline store."""

    url: str
    engine: Any
    service: Any
    authority: Any
    plan: Any
    tmp_path: Path
    config_root: Path
    dataset_root: DatasetRoot
    publisher: ResultArtifactPublisher
    work_root: Path
    #: lazily created by :meth:`evaluator_engine`; disposed with the store.
    _evaluator: Any = None

    # -- reads ------------------------------------------------------------------------- #
    def count(self, sql: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(sql)).scalar_one())

    def job_id(self, job_key: str) -> str:
        with self.engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT id FROM experiments.l2f_experiment_jobs WHERE job_key = :k"),
                    {"k": job_key},
                ).scalar_one()
            )

    def status(self, job_key: str) -> str:
        with self.engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT status FROM experiments.l2f_experiment_jobs WHERE job_key = :k"),
                    {"k": job_key},
                ).scalar_one()
            )

    def execution_result_id(self, job_key: str) -> str:
        with self.engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT id FROM experiments.l2f_execution_results WHERE job_key = :k"),
                    {"k": job_key},
                ).scalar_one()
            )

    # -- the production execution path ---------------------------------------------------- #
    def run(self, *, worker_id: str, runner: Any = None, environment: Any = None) -> Any:
        """Execute the next PENDING job through the least-privilege runner.

        ``environment`` exists so a test can deliberately produce a screen whose outcomes came
        from two different runtimes — the state the observation reader must refuse.
        """
        from minos_engine.storage.l2f2_runner import _execute_l2f2_job

        return _execute_l2f2_job(
            self.service,
            self.authority,
            worker_id=worker_id,
            runner=runner if runner is not None else FakeGatkRunner(),
            dataset_root=self.dataset_root,
            publisher=self.publisher,
            work_root=self.work_root,
            execution_environment=environment or TEST_EXECUTION_ENVIRONMENT,
        )

    # -- the production evaluation path ---------------------------------------------------- #
    def register_truth(self) -> None:
        """Publish and register synthetic TRAIN truth for every registered target."""
        from minos_engine.evaluation.truth_registration import register_train_truth_identities
        from tests.integration.layer2_db.test_l2f2_evaluation_ledger import _register_truth

        root = self.tmp_path / "truth"
        root.mkdir(exist_ok=True)
        _register_truth(self, root)
        register_train_truth_identities(self.engine, dataset_root=root)

    def evaluator_engine(self) -> Any:
        """A LOGIN principal whose ONLY MINOS membership is ``minos_evaluator``, created on demand.

        The evaluator writes exclusively through ``SECURITY DEFINER`` functions, so a test that
        persists an evaluation as this principal proves those functions carry enough authority of
        their own — which is precisely what an ownership corrective must not break.
        """
        if self._evaluator is None:
            from sqlalchemy.engine import make_url

            parsed = make_url(self.url)
            with self.engine.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_EVAL_ROLE}"))
                conn.execute(
                    text(
                        f"CREATE ROLE {_EVAL_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOBYPASSRLS INHERIT"
                    )
                )
                conn.execute(text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_EVAL_ROLE}'))
                conn.execute(text(f"GRANT minos_evaluator TO {_EVAL_ROLE}"))
            self._evaluator = create_engine(parsed.set(username=_EVAL_ROLE, password=""))
        return self._evaluator

    def evaluate(
        self,
        dispatched: Any,
        *,
        minos_score: float,
        admitted: bool = True,
        admission_code: str = "ADMITTED",
        as_evaluator: bool = False,
    ) -> Any:
        """Persist ONE evaluation for a successful execution, through the production path.

        ``as_evaluator`` routes the two privileged WRITES through a ``minos_evaluator``-only
        principal instead of the administrative connection, so a test can prove the evaluator
        boundary end to end without any direct table grant.
        """
        from minos_engine.evaluation.contracts import build_metrics_artifact_bytes
        from minos_engine.evaluation.evaluator import (
            EvaluationArtifactPublisher,
            build_evaluation_record,
            evaluate_metrics,
            record_evaluation_result,
            register_metrics_artifact,
        )
        from tests.integration.layer2_db.test_l2f2_evaluation_ledger import (
            _authority,
            _inputs_for,
            _oracle_result,
            _scoring_inputs,
        )

        inputs = _inputs_for(self, dispatched)
        artifact, admission, _contract = evaluate_metrics(
            inputs=inputs,
            oracle_result=_oracle_result(
                minos_score=minos_score,
                advanced_score_100=minos_score * 100.0,
                minos_score_accepted=admitted,
                admitted=admitted,
                admission_code=admission_code,
            ),
            scoring_inputs=_scoring_inputs(),
            authority=_authority(),
        )
        root = self.tmp_path / "evaluation_artifacts"
        root.mkdir(exist_ok=True)
        os.chmod(root, 0o2750)
        published = EvaluationArtifactPublisher(root).publish(
            build_metrics_artifact_bytes(artifact)
        )
        writer = self.evaluator_engine() if as_evaluator else self.engine
        artifact_id, _created = register_metrics_artifact(writer, published)
        return record_evaluation_result(
            writer,
            build_evaluation_record(
                execution_result_id=self.execution_result_id(dispatched.job_key),
                inputs=inputs,
                artifact=artifact,
                admission_code=admission,
                authority=_authority(),
                metrics_artifact_id=artifact_id,
                metrics=published,
            ),
        )

    def fail_evaluation(
        self, dispatched: Any, *, failure_code: str, as_evaluator: bool = False
    ) -> Any:
        """Persist ONE bounded evaluation failure for a successful execution."""
        from minos_engine.evaluation.evaluator import record_evaluation_failure
        from minos_engine.evaluation.scoring_contract import compute_scoring_contract_hash
        from tests.integration.layer2_db.test_l2f2_evaluation_ledger import _authority

        return record_evaluation_failure(
            self.evaluator_engine() if as_evaluator else self.engine,
            execution_result_id=self.execution_result_id(dispatched.job_key),
            scoring_contract_hash=compute_scoring_contract_hash(_authority()),
            failure_code=failure_code,
        )


def _create_service_login(engine: Any, url: str) -> Any:
    """An ephemeral LOGIN whose only MINOS membership is ``minos_runner``."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    with engine.connect() as conn, conn.begin():
        conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))
        conn.execute(
            text(
                f"CREATE ROLE {_CI_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOBYPASSRLS INHERIT"
            )
        )
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_CI_ROLE}'))
        conn.execute(text(f"GRANT minos_runner TO {_CI_ROLE}"))
    return create_engine(parsed.set(username=_CI_ROLE, password=""))


def _drop_login(engine: Any, url: str, role: str, *, group: str) -> None:
    from sqlalchemy.engine import make_url

    database = make_url(url).database
    with engine.connect() as conn, conn.begin():
        conn.execute(text(f'REVOKE ALL ON DATABASE "{database}" FROM {role}'))
        conn.execute(text(f"REVOKE {group} FROM {role}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


def _drop_service_login(engine: Any, url: str) -> None:
    _drop_login(engine, url, _CI_ROLE, group="minos_runner")


@contextlib.contextmanager
def phase_a_store(base_url: str, tmp_path: Path) -> Iterator[PhaseAEnv]:
    """The frozen Phase-A plan, its canary job and a runner principal, on a real database."""
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.storage.l2f2_canary_prepare import prepare_l2f2_phase_a_canary
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan

    authority = build_phase_a_authority()
    accepted = build_accepted_experiment_plan()

    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    identity = _dataset_identity(authority.plan.members, dataset_root)

    config_root = tmp_path / "cfgroot"
    config_root.mkdir()
    os.chmod(config_root, 0o2750)
    result_root = tmp_path / "resultroot"
    result_root.mkdir()
    os.chmod(result_root, 0o2750)
    work_root = tmp_path / "workroot"
    work_root.mkdir()

    with scratch_database(base_url, BASELINE_DB) as url:
        alembic_upgrade(url, REQUIRED_REVISION)
        engine = _engine(url)
        service = None
        env: PhaseAEnv | None = None
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, accepted, dataset_identity=identity)
            prepare_l2f2_phase_a_canary(engine, config_artifact_root=config_root)
            # the TRAIN truth projection reads catalog.split_allocations, which the upstream seed
            # does not populate. Every frozen Phase-A member is a TRAIN member by construction.
            with engine.connect() as conn, conn.begin():
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
            service = _create_service_login(engine, url)
            env = PhaseAEnv(
                url=url,
                engine=engine,
                service=service,
                authority=authority,
                plan=authority.plan,
                tmp_path=tmp_path,
                config_root=config_root,
                dataset_root=DatasetRoot.from_path(dataset_root),
                publisher=ResultArtifactPublisher(result_root),
                work_root=work_root,
            )
            yield env
        finally:
            if env is not None and env._evaluator is not None:
                env._evaluator.dispose()
                _drop_login(engine, url, _EVAL_ROLE, group="minos_evaluator")
            if service is not None:
                service.dispose()
                _drop_service_login(engine, url)
            engine.dispose()


def close_the_canary(env: PhaseAEnv, *, minos_score: float = 0.8625) -> Any:
    """Drive the frozen canary to the state the expansion gate requires: SUCCEEDED + evaluated."""
    dispatched = env.run(worker_id="ci-canary")
    assert dispatched is not None  # noqa: S101 - fixture invariant
    assert dispatched.job_key == env.authority.canary.job_key  # noqa: S101
    assert dispatched.status == "SUCCEEDED"  # noqa: S101
    env.register_truth()
    env.evaluate(dispatched, minos_score=minos_score)
    return dispatched
