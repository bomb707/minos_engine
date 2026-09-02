"""The Phase-D evaluator refuses before it opens the answer key.

The decisive test here is the WRONG-PLAN attack. A second validation plan over the same ten
frozen members and the same four frozen configurations passes every check the evaluator could
previously make — partition, member, config, parameter space — and is nonetheless a different
campaign. ``0025`` exposes the one fact that separates them, and this proves the production entry
uses it: the impostor is refused with zero truth opens, zero scoring, zero artifacts and zero
ledger rows of any kind.

Every negative below is instrumented the same way. An authorization refusal is an operator error,
not a candidate's scientific outcome, so it must leave no evaluation FAILURE row either.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.validation_members import build_validation_schedule
from minos_engine.evaluation.phase_d_service import (
    PhaseDEvaluatorAuthorityError,
    _evaluate_with_trust,
    authorize_validation_evaluator_connection,
)
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.test_l2f_plan_store import _engine
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

_DB = "minos_l2f2_validation"
_HEAD = "0025_l2f2_phase_d_eval_auth"
_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"
_EVALUATOR_ROLE = "ci_phase_d_evaluator_svc"
_VIEW = "evaluation.l2f_phase_d_execution_authority"


class _TruthSpy:
    """Records every file open. Silence is the assertion, so the spy is proven to work first."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def __enter__(self) -> _TruthSpy:
        self._open, self._os_open = builtins.open, os.open
        self._rb, self._rt = Path.read_bytes, Path.read_text
        spy = self

        def record(target: Any) -> None:
            with contextlib.suppress(Exception):
                spy.opened.append(str(target))

        builtins.open = lambda f, *a, **k: (record(f), spy._open(f, *a, **k))[1]
        os.open = lambda p, *a, **k: (record(p), spy._os_open(p, *a, **k))[1]
        Path.read_bytes = lambda s: (record(s), spy._rb(s))[1]  # type: ignore[method-assign]
        Path.read_text = lambda s, *a, **k: (record(s), spy._rt(s, *a, **k))[1]  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: Any) -> None:
        builtins.open, os.open = self._open, self._os_open
        Path.read_bytes, Path.read_text = self._rb, self._rt  # type: ignore[method-assign]

    @property
    def truth_opens(self) -> list[str]:
        return [
            p
            for p in self.opened
            if "truth.vcf" in p or "mutations.vcf" in p or p.endswith(".vcf.gz.tbi")
        ]


@pytest.fixture(autouse=True)
def _frozen_freeze_env(monkeypatch: Any) -> None:
    """The plan hash is DERIVED from the frozen artifact, so its location is provisioning."""
    from minos_engine.evaluation.phase_d_service import ENV_FINALIST_FREEZE_PATH

    monkeypatch.setenv(ENV_FINALIST_FREEZE_PATH, str(FIXTURE_FREEZE_PATH))


@pytest.fixture(scope="module")
def authority() -> Any:
    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


class _Store:
    def __init__(self, admin: Any, service: Any, url: str) -> None:
        self.admin = admin
        self.service = service
        self.url = url

    def ledger_counts(self) -> dict[str, int]:
        with self.admin.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            return {
                "evaluations": int(
                    conn.execute(
                        text("SELECT count(*) FROM evaluation.l2f_evaluation_results")
                    ).scalar_one()
                ),
                "failures": int(
                    conn.execute(
                        text("SELECT count(*) FROM evaluation.l2f_evaluation_failures")
                    ).scalar_one()
                ),
            }

    def failure_detail(self) -> list[dict[str, Any]]:
        """Why a supposedly-good run failed. Used only to make an assertion message useful."""
        with self.admin.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            return [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT failure_code, tool_exit_code, stderr_sha256 "
                        "  FROM evaluation.l2f_evaluation_failures "
                        "ORDER BY created_at"
                    )
                ).mappings()
            ]

    def evaluate(self, execution_result_id: str, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "engine": self.service,
            "execution_result_id": execution_result_id,
            "expected_database": _DB,
            "expected_revision": _HEAD,
        }
        kwargs.update(overrides)
        return _evaluate_with_trust(**kwargs)


@pytest.fixture
def store(isolated_pg_base_url: str, tmp_path: Path, authority: Any) -> Any:
    """A scratch validation store at 0025 with two validation plans and one execution each."""
    from tests.integration.layer2_db.l2f2_phase_d_eval_seed import seed_two_validation_campaigns

    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        admin = _engine(url)
        service = None
        try:
            seeded = seed_two_validation_campaigns(admin, authority, tmp_path)
            parsed = make_url(url)
            with admin.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_EVALUATOR_ROLE}"))
                conn.execute(
                    text(
                        f"CREATE ROLE {_EVALUATOR_ROLE} LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOBYPASSRLS INHERIT"
                    )
                )
                conn.execute(
                    text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_EVALUATOR_ROLE}')
                )
                conn.execute(text(f"GRANT minos_evaluator TO {_EVALUATOR_ROLE}"))
            service = create_engine(parsed.set(username=_EVALUATOR_ROLE, password=""))
            s = _Store(admin, service, url)
            s.seeded = seeded  # type: ignore[attr-defined]
            yield s
        finally:
            if service is not None:
                service.dispose()
            with contextlib.suppress(Exception), admin.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_EVALUATOR_ROLE}"))
            admin.dispose()
            shutil.rmtree(tmp_path / "eval", ignore_errors=True)


# --------------------------------------------------------------------------------------------
# the authority view, read as the evaluator
# --------------------------------------------------------------------------------------------
def test_the_evaluator_resolves_exactly_one_plan_hash_per_execution(store: Any) -> None:
    with store.service.connect() as conn:
        rows = conn.execute(
            text(f"SELECT execution_result_id, plan_hash FROM {_VIEW} ORDER BY plan_hash")  # noqa: S608
        ).all()
    assert len(rows) == 2, rows
    hashes = {str(r[1]) for r in rows}
    assert _PLAN_HASH in hashes
    assert len(hashes) == 2, "the impostor campaign must be distinguishable"


def test_the_evaluator_still_cannot_read_the_experiment_tables(store: Any) -> None:
    for table in (
        "experiments.l2f_experiment_plans",
        "experiments.l2f_execution_results",
        "experiments.l2f_experiment_jobs",
        "experiments.l2f_experiment_plan_members",
    ):
        with store.service.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))  # noqa: S608


def test_the_authority_view_offers_no_write_path(store: Any) -> None:
    with store.service.connect() as conn, pytest.raises(Exception) as excinfo:
        conn.execute(
            text(f"INSERT INTO {_VIEW} (execution_result_id, plan_hash) VALUES (:i, :h)"),  # noqa: S608
            {"i": "00000000-0000-0000-0000-000000000000", "h": "0" * 64},
        )
    message = str(excinfo.value).lower()
    assert "permission denied" in message or "cannot insert" in message, message


# --------------------------------------------------------------------------------------------
# THE decisive negative
# --------------------------------------------------------------------------------------------
def test_a_wrong_plan_execution_is_refused_before_any_truth_is_opened(store: Any) -> None:
    """Same ten members, same four configs, different campaign. Everything else would pass."""
    impostor = store.seeded["impostor_execution_result_id"]
    before = store.ledger_counts()

    with _TruthSpy() as spy, pytest.raises(PhaseDEvaluatorAuthorityError, match="belongs to plan"):
        store.evaluate(impostor)

    assert spy.truth_opens == [], spy.truth_opens
    assert store.ledger_counts() == before == {"evaluations": 0, "failures": 0}


def test_the_impostor_would_have_passed_the_old_partition_and_config_checks(store: Any) -> None:
    """Without 0025 the impostor is indistinguishable — which is why 0025 exists."""
    with store.admin.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        row = (
            conn.execute(
                text(
                    "SELECT m.partition, dr.dataset_id, r.config_hash, r.parameter_space_hash "
                    "  FROM experiments.l2f_execution_results r "
                    "  JOIN experiments.l2f_experiment_jobs j ON j.id = r.job_id "
                    "  JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
                    "  JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
                    " WHERE r.id = :e"
                ),
                {"e": store.seeded["impostor_execution_result_id"]},
            )
            .mappings()
            .one()
        )

    frozen_members = {m.dataset_id for m in build_validation_schedule().members}
    assert str(row["partition"]) == "validation"
    assert str(row["dataset_id"]) in frozen_members
    assert str(row["config_hash"]) in set(store.seeded["ordered_config_hashes"])
    assert str(row["parameter_space_hash"]) == store.seeded["parameter_space_hash"]


# --------------------------------------------------------------------------------------------
# connection / principal negatives — all before truth
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"expected_database": "minos_l2f2_baseline"}, "refuses database"),
        ({"expected_database": "minos_engine_db"}, "refuses database"),
        ({"expected_revision": "0024_l2f2_phase_d_anchor"}, "revision"),
        ({"expected_revision": "0099_nope"}, "revision"),
    ],
)
def test_store_negatives_refuse_before_truth(store: Any, override: dict, match: str) -> None:
    genuine = store.seeded["genuine_execution_result_id"]
    before = store.ledger_counts()
    with _TruthSpy() as spy, pytest.raises(PhaseDEvaluatorAuthorityError, match=match):
        store.evaluate(genuine, **override)
    assert spy.truth_opens == []
    assert store.ledger_counts() == before


def test_an_unknown_execution_is_refused_before_truth(store: Any) -> None:
    before = store.ledger_counts()
    with (
        _TruthSpy() as spy,
        pytest.raises(PhaseDEvaluatorAuthorityError, match="resolves to 0 Phase-D authority rows"),
    ):
        store.evaluate("00000000-0000-0000-0000-000000000000")
    assert spy.truth_opens == []
    assert store.ledger_counts() == before


def test_an_over_privileged_principal_is_refused(store: Any) -> None:
    """Authenticating as admin and assuming the evaluator role does not pass."""
    with store.admin.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        # fails closed. minos_admin cannot even read the revision pin, so the refusal may come
        # from postgres before the membership check is reached — either way it does not pass.
        with pytest.raises(Exception) as excinfo:
            authorize_validation_evaluator_connection(conn)
        message = str(excinfo.value).lower()
        assert "membership" in message or "permission denied" in message, message


def test_the_evaluator_principal_is_accepted(store: Any) -> None:
    with store.service.connect() as conn:
        authorize_validation_evaluator_connection(conn)


def test_a_principal_with_an_extra_membership_is_refused(store: Any) -> None:
    extra = "ci_phase_d_eval_extra_svc"
    parsed = make_url(store.url)
    engine = None
    try:
        with store.admin.connect() as conn, conn.begin():
            conn.execute(text(f"DROP ROLE IF EXISTS {extra}"))
            conn.execute(
                text(
                    f"CREATE ROLE {extra} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOBYPASSRLS INHERIT"
                )
            )
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {extra}'))
            conn.execute(text(f"GRANT minos_evaluator TO {extra}"))
            conn.execute(text(f"GRANT minos_runner TO {extra}"))
        engine = create_engine(parsed.set(username=extra, password=""))
        with (
            engine.connect() as conn,
            pytest.raises(PhaseDEvaluatorAuthorityError, match="memberships"),
        ):
            authorize_validation_evaluator_connection(conn)
    finally:
        if engine is not None:
            engine.dispose()
        with contextlib.suppress(Exception), store.admin.connect() as conn, conn.begin():
            conn.execute(text(f"DROP ROLE IF EXISTS {extra}"))


# --------------------------------------------------------------------------------------------
# the positive: ONE scratch Phase-D execution, all the way through the production path
#
# The oracle and the pinned scorer here are the SYNTHETIC ones. No real validation truth is
# opened, no real MINOS_SUBNET score is produced, and no real metrics artifact is written; what
# is proven is that the authorized path completes and that a second attempt does not repeat it.
# --------------------------------------------------------------------------------------------
def _synthetic_scoring(tmp_path: Path) -> tuple[Any, Any]:
    """The synthetic pinned checkout, and the authority that checkout actually satisfies."""
    from minos_engine.evaluation.scoring_contract import load_scoring_authority
    from tests.integration.layer2_db.l2f2_fake_upstream import (
        authority_for,
        build_fake_upstream,
        oracle_for,
    )

    base = tmp_path / "fake_upstream_base"
    base.mkdir(parents=True, exist_ok=True)
    upstream = build_fake_upstream(base)
    upstream.set_mode("metrics")
    authority = authority_for(upstream, load_scoring_authority())
    return authority, oracle_for(upstream, authority)


def _runtime_env(store: Any, tmp_path: Path, monkeypatch: Any) -> Path:
    """Provision the four roots the service REQUIRES and refuses to create for itself."""
    from minos_engine.evaluation.artifact_publisher import ENV_EVALUATION_ARTIFACT_ROOT
    from minos_engine.evaluation.phase_d_service import (
        ENV_EVALUATION_PRACTICE_ROOT,
        ENV_EVALUATION_REFERENCE_ROOT,
        ENV_EVALUATION_WORK_ROOT,
    )
    from tests.integration.layer2_db.l2f2_phase_d_eval_seed import provision_validation_truth

    practice = tmp_path / "practice"
    registered = provision_validation_truth(store.admin, practice)
    assert registered == 10, registered

    # laid down by the seeder, whose ledger rows record THESE bytes' digests
    reference = tmp_path / "reference"
    work = tmp_path / "evaluation_work"
    work.mkdir(parents=True, exist_ok=True)
    artifacts = tmp_path / "evaluation_artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    os.chmod(artifacts, 0o2750)

    monkeypatch.setenv(ENV_EVALUATION_PRACTICE_ROOT, str(practice))
    monkeypatch.setenv(ENV_EVALUATION_REFERENCE_ROOT, str(reference))
    monkeypatch.setenv(ENV_EVALUATION_WORK_ROOT, str(work))
    monkeypatch.setenv(ENV_EVALUATION_ARTIFACT_ROOT, str(artifacts))
    return artifacts


def test_the_authorized_execution_evaluates_and_never_evaluates_twice(
    store: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """§22 and §23 together: one success, then a replay that produces nothing new.

    Asserting them in ONE test is deliberate. Replay-safety is a property of a store that has
    already succeeded, so a separate test would have to re-run the first evaluation to set it up
    and would then be proving replay against its own fresh work rather than against durable state.
    """
    artifacts = _runtime_env(store, tmp_path, monkeypatch)
    authority, oracle = _synthetic_scoring(tmp_path)
    genuine = store.seeded["genuine_execution_result_id"]

    before = store.ledger_counts()
    assert before == {"evaluations": 0, "failures": 0}

    outcome = store.evaluate(genuine, oracle=oracle, authority=authority)
    assert outcome.status == "EVALUATED", (outcome, store.failure_detail())
    assert outcome.execution_result_id == genuine
    assert outcome.metrics_artifact_sha256 is not None
    assert store.ledger_counts() == {"evaluations": 1, "failures": 0}

    first = {
        "ledger": store.ledger_counts(),
        "artifacts": sorted(p.name for p in artifacts.rglob("*") if p.is_file()),
        "metrics_sha": outcome.metrics_artifact_sha256,
        "oracle_calls": _oracle_call_count(tmp_path),
    }
    assert first["artifacts"], "the success published no metrics artifact"

    # ---- §23 replay: same execution, same everything --------------------------------------
    with _TruthSpy() as spy:
        replay = store.evaluate(genuine, oracle=oracle, authority=authority)
    assert replay.status == "EVALUATED", replay
    assert replay.metrics_artifact_sha256 == first["metrics_sha"]
    assert store.ledger_counts() == first["ledger"]
    assert sorted(p.name for p in artifacts.rglob("*") if p.is_file()) == first["artifacts"]
    assert _oracle_call_count(tmp_path) == first["oracle_calls"], "the replay re-ran the scorer"
    assert not spy.truth_opens, spy.opened


def _oracle_call_count(tmp_path: Path) -> int:
    """How many times the synthetic upstream has actually been invoked."""
    import json as _json

    seen = tmp_path / "fake_upstream_base" / "upstream-seen.json"
    if not seen.is_file():
        return 0
    payload = _json.loads(seen.read_text())
    return len(payload) if isinstance(payload, list) else int(payload.get("calls", 0))


# --------------------------------------------------------------------------------------------
# §25 — the PINNED SCORER negatives, through the production loader
#
# These do NOT inject an authority. They perturb what ``load_scoring_authority`` returns and let
# the production pin decide, which is the only way to prove the pin is load-bearing.
# --------------------------------------------------------------------------------------------
def _pin_negative(monkeypatch: Any, **update: Any) -> None:
    """Make the PRODUCTION loader return a perturbed authority and let the pin decide.

    ``_require_scoring_authority`` imports the loader at call time, so patching the module
    attribute reaches it — and nothing about the pin itself is patched.
    """
    from minos_engine.evaluation.scoring_contract import load_scoring_authority

    perturbed = load_scoring_authority().model_copy(update=update)
    monkeypatch.setattr(
        "minos_engine.evaluation.scoring_contract.load_scoring_authority", lambda: perturbed
    )


@pytest.mark.parametrize(
    ("update", "match"),
    [
        # the commit is INSIDE contract_content, so the contract hash catches it first; the
        # commit check itself is isolated and proven separately below.
        pytest.param(
            {"upstream_commit": "0" * 40}, "scoring contract", id="wrong-minos-subnet-commit"
        ),
        pytest.param({"scoring_py_sha256": "1" * 64}, "scoring contract", id="modified-scoring-py"),
        pytest.param(
            {"validator_py_sha256": "2" * 64}, "scoring contract", id="modified-validator-py"
        ),
        pytest.param(
            {"tool_params_py_sha256": "3" * 64}, "scoring contract", id="modified-tool-params-py"
        ),
    ],
)
def test_a_perturbed_pinned_scorer_is_refused(
    store: Any, tmp_path: Path, monkeypatch: Any, update: dict, match: str
) -> None:
    """A scorer that is not byte-identical to the one that SELECTED the finalists is refused."""
    _runtime_env(store, tmp_path, monkeypatch)
    _, oracle = _synthetic_scoring(tmp_path)
    _pin_negative(monkeypatch, **update)

    with _TruthSpy() as spy, pytest.raises(PhaseDEvaluatorAuthorityError, match=match):
        store.evaluate(store.seeded["genuine_execution_result_id"], oracle=oracle)
    assert not spy.truth_opens, spy.opened
    assert store.ledger_counts() == {"evaluations": 0, "failures": 0}


@pytest.mark.parametrize("image", ["happy", "bcftools"])
def test_a_wrong_runtime_image_digest_is_refused(
    store: Any, tmp_path: Path, monkeypatch: Any, image: str
) -> None:
    """The containers are part of the scoring contract: a different digest is a different scorer."""
    from minos_engine.evaluation.scoring_contract import (
        RuntimeImageIdentity,
        load_scoring_authority,
    )

    _runtime_env(store, tmp_path, monkeypatch)
    _, oracle = _synthetic_scoring(tmp_path)
    current = getattr(load_scoring_authority(), image)
    _pin_negative(
        monkeypatch,
        **{
            image: RuntimeImageIdentity(
                upstream_ref=current.upstream_ref,
                resolved_digest=current.resolved_digest.rsplit(":", 1)[0] + ":" + "c" * 64,
            )
        },
    )

    with _TruthSpy() as spy, pytest.raises(PhaseDEvaluatorAuthorityError, match="scoring contract"):
        store.evaluate(store.seeded["genuine_execution_result_id"], oracle=oracle)
    assert not spy.truth_opens, spy.opened
    assert store.ledger_counts() == {"evaluations": 0, "failures": 0}


def test_the_commit_pin_is_load_bearing_on_its_own(
    store: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Defence in depth: the commit check refuses even when the contract hash agrees.

    ``upstream_commit`` is inside ``contract_content``, so in practice the contract hash catches a
    swapped commit first — which means the separate commit check is never observed to fire and
    could rot into a no-op unnoticed. Neutralising only the contract hash isolates it and proves
    it still refuses on its own.
    """
    from minos_engine.evaluation import scoring_contract
    from minos_engine.evaluation.phase_d_service import ACCEPTED_SCORING_CONTRACT_HASH

    _runtime_env(store, tmp_path, monkeypatch)
    _, oracle = _synthetic_scoring(tmp_path)
    _pin_negative(monkeypatch, upstream_commit="0" * 40)
    monkeypatch.setattr(
        scoring_contract, "compute_scoring_contract_hash", lambda _a: ACCEPTED_SCORING_CONTRACT_HASH
    )

    with _TruthSpy() as spy, pytest.raises(PhaseDEvaluatorAuthorityError, match="MINOS_SUBNET"):
        store.evaluate(store.seeded["genuine_execution_result_id"], oracle=oracle)
    assert not spy.truth_opens, spy.opened
    assert store.ledger_counts() == {"evaluations": 0, "failures": 0}


# --------------------------------------------------------------------------------------------
# §25 — TRAIN and TEST executions
#
# These are proven where they are actually decided rather than by seeding a TRAIN campaign. A
# TRAIN plan requires the full feature-matrix lineage that ``ck_l2f_plans_partition_lineage``
# demands and a VALIDATION plan is forbidden to carry, so a validation plan cannot be relabelled
# into one; TEST cannot be spelled at all. Both facts are enforced by the database, not by the
# service, which is why they hold for every caller including one that never runs this code.
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("partition", ["train", "test"])
def test_a_phase_d_plan_cannot_be_relabelled_into_another_partition(
    store: Any, partition: str
) -> None:
    """Relabelling is refused by the store itself, as the control-plane role, with no service."""
    with store.admin.connect() as conn, conn.begin():
        conn.execute(text("SET ROLE minos_admin"))
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text(
                    "UPDATE experiments.l2f_experiment_plans SET partition = :q "
                    " WHERE plan_hash = :h"
                ),
                {"q": partition, "h": _PLAN_HASH},
            )
    message = str(excinfo.value).lower()
    assert "check constraint" in message or "append-only" in message or "immutable" in message, (
        message
    )


def test_no_execution_outside_the_validation_partition_is_visible_to_the_evaluator(
    store: Any,
) -> None:
    """The view's admission is the plan's partition, and only ``validation`` satisfies it."""
    with store.admin.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        partitions = set(
            conn.execute(
                text(
                    "SELECT DISTINCT p.partition "
                    "  FROM experiments.l2f_execution_results r "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = r.plan_id "
                    f" WHERE r.id IN (SELECT execution_result_id FROM {_VIEW})"
                )
            ).scalars()
        )
    assert partitions == {"validation"}, partitions
