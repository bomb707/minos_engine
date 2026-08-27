"""Control-plane canary preparation and the 0011 migration lifecycle, against real PostgreSQL.

Preparation is exercised on an EPHEMERAL database only. Nothing here touches the real baseline
store, and no GATK, hap.py, truth or score is involved.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.storage.l2f2_canary_prepare import (
    CanaryPreparationError,
    prepare_l2f2_phase_a_canary,
)
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine, _provisioned_root

_BASELINE_DB = "minos_l2f2_baseline"
_RUNNER_BOUNDARY = "0011_l2f2_runner_boundary"
_CORRECTIVE = "0010_l2f2_evaluation_corrective"
#: the revision Phase-A preparation and the production runner now require EXACTLY. 0011 cannot
#: represent a subset plan's two index namespaces; 0013 is where the shared baseline store now
#: sits, because the evaluator needs it.
_SOURCE_INDEX = "0018_l2f2_eval_owner_fix"
_AUTHORITIES = "experiments.l2f2_execution_authorities"


def _inventory(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "revision": conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            "authority_table": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        " WHERE table_schema='experiments' "
                        "   AND table_name='l2f2_execution_authorities'"
                    )
                ).scalar_one()
            ),
            "l2f2_functions": sorted(
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT p.proname FROM pg_proc p "
                        "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                        " WHERE n.nspname='experiments' AND p.proname LIKE 'l2f2_%'"
                    )
                )
            ),
            "plan_composite": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        " WHERE conname = 'uq_l2f_experiment_plans_id_hash'"
                    )
                ).scalar_one()
            ),
            "runner_reads_revision": bool(
                conn.execute(
                    text(
                        "SELECT has_table_privilege('minos_runner', 'public.alembic_version', "
                        "'SELECT')"
                    )
                ).scalar_one()
            ),
        }


# --------------------------------------------------------------------------- #
# migration lifecycle
# --------------------------------------------------------------------------- #
def test_0011_downgrades_to_exactly_0010_and_upgrades_back(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            at_0011 = _inventory(engine)
            assert at_0011["revision"] == _RUNNER_BOUNDARY
            assert at_0011["authority_table"] == 1
            assert at_0011["l2f2_functions"] == [
                "l2f2_register_execution_artifact",
                "l2f2_resolve_claimed_execution",
            ]
            assert at_0011["plan_composite"] == 1
            assert at_0011["runner_reads_revision"] is True
        finally:
            engine.dispose()

        alembic_downgrade(url, _CORRECTIVE)
        engine = _engine(url)
        try:
            at_0010 = _inventory(engine)
            assert at_0010 == {
                "revision": _CORRECTIVE,
                "authority_table": 0,
                "l2f2_functions": [],
                "plan_composite": 0,
                "runner_reads_revision": False,
            }
        finally:
            engine.dispose()

        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            assert _inventory(engine) == at_0011, "re-upgrade did not restore the 0011 inventory"
        finally:
            engine.dispose()


def test_the_runner_role_gains_no_table_privilege_from_0011(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    granted = conn.execute(
                        text("SELECT has_table_privilege('minos_runner', :t, :p)"),
                        {"t": _AUTHORITIES, "p": privilege},
                    ).scalar_one()
                    assert granted is False, f"minos_runner has {privilege} on the authority table"
                for role in ("minos_evaluator", "minos_trainer", "minos_live"):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert (
                            conn.execute(
                                text("SELECT has_table_privilege(:r, :t, :p)"),
                                {"r": role, "t": _AUTHORITIES, "p": privilege},
                            ).scalar_one()
                            is False
                        )
                # the runner CAN execute exactly the two narrow functions
                for function in (
                    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
                    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
                ):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege('minos_runner', :f, 'EXECUTE')"),
                            {"f": function},
                        ).scalar_one()
                        is True
                    )
                    for role in ("minos_evaluator", "minos_trainer", "minos_live"):
                        assert (
                            conn.execute(
                                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                {"r": role, "f": function},
                            ).scalar_one()
                            is False
                        )
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# control-plane preparation
# --------------------------------------------------------------------------- #
@pytest.fixture
def baseline(isolated_pg_base_url: str) -> Any:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _SOURCE_INDEX)
        engine = _engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "revision",
    [
        _CORRECTIVE,
        _RUNNER_BOUNDARY,
        "0012_l2f_plan_member_source_idx",
        "0013_l2f2_upstream_score_oracle",
        "0014_l2f2_exec_failure_runtime",
        "0015_l2f2_exec_environment",
        "0016_l2f2_phase_b_execution",
        "0017_l2f2_owner_corrective",
    ],
)
def test_preparation_refuses_a_database_at_the_wrong_revision(
    isolated_pg_base_url: str, tmp_path: Path, revision: str
) -> None:
    """EXACT-revision, fail-closed — including 0011, which is only one revision behind.

    0011 is not "close enough": it stores one ordinal for both index namespaces, so the Phase-A
    subset plan has no representation there at all.
    """
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, revision)
        engine = _engine(url)
        try:
            with pytest.raises(CanaryPreparationError, match="revision"):
                prepare_l2f2_phase_a_canary(engine, config_artifact_root=tmp_path)
        finally:
            engine.dispose()


def test_preparation_refuses_a_plan_it_did_not_create(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """It never enqueues the canary beside state it did not create.

    A genuinely persisted synthetic plan stands in for "unexplained state": it is a real,
    well-formed plan that is simply not the frozen Phase-A plan.
    """
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
    from tests.integration.layer2_db.test_l2f_execution import _prepare_env
    from tests.integration.layer2_db.test_l2f_plan_store import (
        _CS,
        _SNAPSHOT_A,
        _provisioned_root,
        _publisher,
    )

    plan, identity, _dataset_root = _prepare_env(
        isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=1
    )
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, "0006_l2f_experiment_plan")
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            engine.dispose()
            alembic_upgrade(url, _SOURCE_INDEX)
            engine = _engine(url)

            with pytest.raises(CanaryPreparationError, match="unexplained experiment plan"):
                prepare_l2f2_phase_a_canary(engine, config_artifact_root=tmp_path / "cfg")
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# the frozen Phase-A SUBSET against the REAL accepted 50-member TRAIN closure
#
# Phase A is five members of a fifty-member accepted TRAIN closure. Its plan-local indices are
# 0..4, but the matrix rows those members point at sit at 0/10/20/30/40 — one per chromosome
# batch. Everything below seeds that REAL topology (50 matrix rows at 0..49), never a contiguous
# five-row synthetic shortcut, because the contiguous shape cannot distinguish the two indices.
# --------------------------------------------------------------------------- #
#: (plan-local index, dataset, source feature_matrix_members.member_index) — derived here from
#: committed repository authority in the fixture below, and additionally pinned literally so a
#: silent change to either namespace is caught.
_PHASE_A_TOPOLOGY: tuple[tuple[int, str, int], ...] = (
    (0, "minos-chr18-028662fb934529d7", 0),
    (1, "minos-chr19-0de906231aa96ade", 10),
    (2, "minos-chr20-42bdea88e6242d37", 20),
    (3, "minos-chr21-0279a3b8042f848b", 30),
    (4, "minos-chr22-19a4002faeaacbdf", 40),
)
_PHASE_A_PLAN_HASH = "97ba598778a5fc634345ded0901e4975af9c6b875c5b70fc7e76f2ae482e1b9a"


def _accepted_plan() -> Any:
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan

    return build_accepted_experiment_plan()


@contextlib.contextmanager
def _accepted_upstream_baseline(base_url: str, **seed: Any) -> Iterator[Any]:
    """A 0011 baseline seeded with the COMPLETE accepted 50-member TRAIN upstream closure."""
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan

    with scratch_database(base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _SOURCE_INDEX)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _accepted_plan(), **seed)
            yield engine
        finally:
            engine.dispose()


def _persisted_topology(engine: Any) -> list[tuple[int, str, int]]:
    """(plan-local member_index, dataset_id, SOURCE feature_matrix_members.member_index)."""
    with engine.connect() as conn:
        return [
            (int(r.member_index), str(r.dataset_id), int(r.source_index))
            for r in conn.execute(
                text(
                    "SELECT pm.member_index AS member_index, dr.dataset_id AS dataset_id, "
                    "       fmm.member_index AS source_index "
                    "  FROM experiments.l2f_experiment_plan_members pm "
                    "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                    "  JOIN profiling.feature_matrix_members fmm "
                    "    ON fmm.id = pm.feature_matrix_member_id "
                    " ORDER BY pm.member_index"
                )
            )
        ]


def _baseline_counts(engine: Any) -> dict[str, int]:
    with engine.connect() as conn:
        counts = {
            name: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            for name, table in (
                ("plans", "experiments.l2f_experiment_plans"),
                ("members", "experiments.l2f_experiment_plan_members"),
                ("configs", "experiments.l2f_experiment_plan_configs"),
                ("payloads", "experiments.l2f_config_payloads"),
                ("jobs", "experiments.l2f_experiment_jobs"),
                ("authorities", _AUTHORITIES),
                ("results", "experiments.l2f_execution_results"),
                ("failures", "experiments.l2f_execution_failures"),
            )
        }
        counts["pending_jobs"] = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE status = 'PENDING'"
                )
            ).scalar_one()
        )
        return counts


def test_the_frozen_phase_a_topology_is_derived_from_committed_authority() -> None:
    """The 0/10/20/30/40 mapping is repository authority, not a value any caller supplies."""
    from minos_engine.baseline.phase_a import build_phase_a_plan

    plan = build_phase_a_plan()
    accepted = _accepted_plan()
    source_index = {m.dataset_id: m.member_index for m in accepted.members}
    assert plan.plan_hash == _PHASE_A_PLAN_HASH
    assert accepted.train_member_count == 50
    assert [(m.member_index, m.dataset_id, source_index[m.dataset_id]) for m in plan.members] == [
        *_PHASE_A_TOPOLOGY
    ]
    # the two namespaces genuinely differ for four of the five members.
    assert sum(1 for local, _, source in _PHASE_A_TOPOLOGY if local != source) == 4


def test_phase_a_preparation_persists_the_subset_against_the_full_closure(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """§21/§22: the real-shaped 50-row upstream, five local members, source indices preserved."""
    from minos_engine.baseline.phase_a import build_phase_a_authority

    authority = build_phase_a_authority()
    root = _provisioned_root(tmp_path)
    with _accepted_upstream_baseline(isolated_pg_base_url) as engine:
        result = prepare_l2f2_phase_a_canary(engine, config_artifact_root=root)

        assert (result.plan_created, result.authority_created, result.job_created) == (
            True,
            True,
            True,
        )
        assert result.plan_hash == _PHASE_A_PLAN_HASH == authority.plan_hash
        assert result.canary_job_key == authority.canary.job_key
        assert (result.member_count, result.candidate_count, result.logical_job_count) == (
            5,
            39,
            195,
        )
        assert result.enqueued_job_count == 1

        # PLAN-LOCAL 0..4, SOURCE 0/10/20/30/40, exact dataset association, no cross-linking.
        assert _persisted_topology(engine) == [*_PHASE_A_TOPOLOGY]
        assert _baseline_counts(engine) == {
            "plans": 1,
            "members": 5,
            "configs": 39,
            "payloads": 39,
            "jobs": 1,
            "authorities": 1,
            "results": 0,
            "failures": 0,
            "pending_jobs": 1,
        }
        assert len(sorted(root.glob("*.json"))) == 39
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT job_key FROM experiments.l2f_experiment_jobs")
                ).scalar_one()
                == authority.canary.job_key
            )
            # the 45 unselected accepted TRAIN members remain live and unreferenced.
            assert (
                int(
                    conn.execute(
                        text("SELECT count(*) FROM profiling.feature_matrix_members")
                    ).scalar_one()
                )
                == 50
            )


def test_phase_a_preparation_is_idempotent(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """§25: replay converges — no duplicate plan, member, config, payload, authority or job."""
    root = _provisioned_root(tmp_path)
    with _accepted_upstream_baseline(isolated_pg_base_url) as engine:
        first = prepare_l2f2_phase_a_canary(engine, config_artifact_root=root)
        after_first = _baseline_counts(engine)
        second = prepare_l2f2_phase_a_canary(engine, config_artifact_root=root)

        assert (first.plan_created, first.authority_created, first.job_created) == (
            True,
            True,
            True,
        )
        assert (second.plan_created, second.authority_created, second.job_created) == (
            False,
            False,
            False,
        )
        assert (second.plan_id, second.authority_id, second.plan_hash) == (
            first.plan_id,
            first.authority_id,
            first.plan_hash,
        )
        assert _baseline_counts(engine) == after_first
        assert _baseline_counts(engine)["plans"] == 1
        assert _persisted_topology(engine) == [*_PHASE_A_TOPOLOGY]
        assert len(sorted(root.glob("*.json"))) == 39


# --------------------------------------------------------------------------- #
# negative controls (§24)
# --------------------------------------------------------------------------- #
#: (label, seed kwargs). Corruption indices are indices into the ACCEPTED 50-member plan:
#: 0/10/20/30/40 are the five SELECTED Phase-A members; 7 and 47 are UNSELECTED members whose
#: defects must still block Phase-A persistence via the full-closure proof.
_PHASE_A_UPSTREAM_NEGATIVES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("A/selected vector_hash", {"corrupt": "matrix_member_vector_hash", "corrupt_index": 10}),
    (
        "B/selected feature_values_hash",
        {"corrupt": "matrix_member_feature_values_hash", "corrupt_index": 20},
    ),
    ("C/selected profile_id", {"corrupt": "bam_profile_id", "corrupt_index": 30}),
    ("C/selected content_hash", {"corrupt": "bam_content_hash", "corrupt_index": 0}),
    ("D/selected source matrix index", {"corrupt": "matrix_member_index", "corrupt_index": 40}),
    ("E/unselected vector_hash", {"corrupt": "matrix_member_vector_hash", "corrupt_index": 47}),
    ("E/unselected profile lineage", {"corrupt": "bam_dataset_registry_id", "corrupt_index": 7}),
    ("E/missing unselected member", {"set_defect": "missing_member"}),
    ("E/extra train matrix member", {"set_defect": "extra_matrix_member"}),
    ("E/extra train snapshot member", {"set_defect": "extra_snapshot_member"}),
)


@pytest.mark.parametrize(
    ("label", "seed"), _PHASE_A_UPSTREAM_NEGATIVES, ids=[n for n, _ in _PHASE_A_UPSTREAM_NEGATIVES]
)
def test_phase_a_preparation_rejects_a_defective_upstream(
    isolated_pg_base_url: str, tmp_path: Path, label: str, seed: dict[str, Any]
) -> None:
    """A defect in ANY accepted TRAIN member — selected or not — blocks Phase-A persistence.

    Upstream validation is never reduced to "the five selected rows exist": the complete accepted
    50-member closure is proven first, so the five unselected-member cases here reject exactly as
    the five selected-member cases do, and nothing is published either way.
    """
    from minos_engine.storage.l2f_plan_store import UpstreamIdentityError

    root = _provisioned_root(tmp_path)
    with _accepted_upstream_baseline(isolated_pg_base_url, **seed) as engine:
        with pytest.raises(UpstreamIdentityError):
            prepare_l2f2_phase_a_canary(engine, config_artifact_root=root)
        # §26: no partial rows, no stray artifacts.
        assert sorted(root.glob("*.json")) == []
        counts = _baseline_counts(engine)
        assert counts["plans"] == 0
        assert counts["members"] == 0
        assert counts["payloads"] == 0
        assert counts["configs"] == 0
        assert counts["authorities"] == 0
        assert counts["jobs"] == 0


def test_the_historical_full_resolver_still_rejects_the_phase_a_subset(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """The observed defect, reproduced: Phase A's LOCAL index is not its SOURCE matrix index.

    Resolving the Phase-A plan with the historical full-inventory resolver looks for matrix
    member_index 1 for chr19, whose real row is at 10 — member 0 happens to succeed because 0 == 0
    — and, had that resolved, the full train-set equality proof would then have rejected the plan
    for covering 5 of the 50 live TRAIN members. The dedicated Phase-A boundary exists precisely
    because neither behavior is a defect in the historical resolver.
    """
    from minos_engine.baseline.phase_a import build_phase_a_plan
    from minos_engine.storage import l2f_plan_store as PS

    plan = build_phase_a_plan()
    with _accepted_upstream_baseline(isolated_pg_base_url) as engine, engine.connect() as conn:
        with pytest.raises(PS.UpstreamIdentityError, match="feature_matrix_member"):
            PS._resolve_plan_upstream(conn, plan)
        # the SELECTED members do exist upstream — only their index namespace differs.
        resolved = PS._resolve_phase_a_upstream(conn, plan)
        assert [
            (m["member_index"], m["dataset_id"], m["source_matrix_member_index"])
            for m in resolved["members"]
        ] == [*_PHASE_A_TOPOLOGY]
    assert tmp_path.exists()


def test_no_production_api_persists_an_arbitrary_subset(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """§24-F: the dedicated boundary takes no member selection and refuses any other plan."""
    import inspect

    from minos_engine.storage import l2f_plan_store as PS
    from tests.integration.layer2_db.test_l2f_plan_store import _synthetic_plan

    signature = inspect.signature(PS._persist_l2f2_phase_a_plan_with_trust)
    assert sorted(signature.parameters) == ["engine", "publisher"]
    # structural, not textual: no FUNCTION in the module takes a subset-bypass parameter.
    # (a substring scan would match the prose that documents their absence.)
    bypass_names = {"allow_subset", "skip_full_equality", "ignore_member_index", "subset"}
    for name, obj in vars(PS).items():
        if not inspect.isfunction(obj):
            continue
        offending = bypass_names & set(inspect.signature(obj).parameters)
        assert not offending, f"{name} exposes a generic subset bypass parameter {offending}"

    # an arbitrary well-formed five-member plan is refused by the Phase-A resolver itself.
    arbitrary = _synthetic_plan([("dsX1", "train", "chr18"), ("dsX2", "train", "chr19")])
    assert arbitrary.plan_hash != _PHASE_A_PLAN_HASH
    with (
        _accepted_upstream_baseline(isolated_pg_base_url) as engine,
        engine.connect() as conn,
        pytest.raises(PS.UpstreamIdentityError, match="ONLY the frozen Phase-A plan"),
    ):
        PS._resolve_phase_a_upstream(conn, arbitrary)
    assert tmp_path.exists()


def test_renumbering_the_phase_a_local_indices_is_unconstructable() -> None:
    """§24-G: local indices 0..4 are part of the plan's identity, not a free choice."""
    from pydantic import ValidationError

    from minos_engine.baseline.phase_a import build_phase_a_plan
    from minos_engine.experiments.plan import ExperimentPlanMember, _assemble_experiment_plan

    plan = build_phase_a_plan()
    renumbered = [
        ExperimentPlanMember(
            dataset_id=m.dataset_id,
            profile_id=m.profile_id,
            content_hash=m.content_hash,
            feature_values_hash=m.feature_values_hash,
            vector_hash=m.vector_hash,
            member_index=source,  # the SOURCE matrix index, used as the plan-local index
        )
        for m, (_local, _dataset, source) in zip(plan.members, _PHASE_A_TOPOLOGY, strict=True)
    ]
    with pytest.raises(ValidationError, match="contiguous"):
        _assemble_experiment_plan(
            epoch=plan.epoch,
            snapshot_hash=plan.snapshot_hash,
            split_manifest_hash=plan.split_manifest_hash,
            registry_snapshot_hash=plan.registry_snapshot_hash,
            train_matrix_hash=plan.train_matrix_hash,
            train_feature_view_hash=plan.train_feature_view_hash,
            feature_set_hash=plan.feature_set_hash,
            feature_registry_hash=plan.feature_registry_hash,
            candidate_set=generate_accepted_candidate_set(),
            ordered_members=renumbered,
        )


@pytest.mark.parametrize(
    ("conflation", "expected"),
    [
        # writing the SOURCE ordinal into the plan-local column satisfies the lineage FK (the
        # source ordinal it stores is still correct), so the READ-BACK is what catches it.
        ("source_written_as_local", "verification"),
        # writing the PLAN-LOCAL ordinal into the source column is refused one level lower: from
        # 0012 the FK binds that column to the referenced matrix row's own ordinal, so the
        # DATABASE rejects it before the read-back ever runs.
        ("local_written_as_source", "foreign_key"),
    ],
)
def test_conflating_the_two_index_namespaces_is_rejected(
    isolated_pg_base_url: str, tmp_path: Path, conflation: str, expected: str
) -> None:
    """§24-H: both namespaces are enforced, and neither conflation direction can be persisted.

    This also exercises the second rollback shape: the failure happens AFTER the 39 CONFIG
    payloads have been published, so the created artifacts must be removed and no row may survive.
    """
    from minos_engine.baseline.phase_a import build_phase_a_plan
    from minos_engine.storage import l2f_plan_store as PS
    from minos_engine.storage.l2f_config_publisher import ConfigPayloadPublisher

    plan = build_phase_a_plan()
    candidate_set = generate_accepted_candidate_set()
    root = _provisioned_root(tmp_path)

    def _conflating_resolver(conn: Any, resolving: Any) -> dict[str, Any]:
        upstream = PS._resolve_phase_a_upstream(conn, resolving)
        members = [dict(m) for m in upstream["members"]]
        for member in members:
            if conflation == "source_written_as_local":
                member["member_index"] = member["source_matrix_member_index"]
            else:
                member["source_matrix_member_index"] = member["member_index"]
        upstream["members"] = members
        return upstream

    from sqlalchemy.exc import IntegrityError

    raises: Any = PS.PlanVerificationError if expected == "verification" else IntegrityError
    with _accepted_upstream_baseline(isolated_pg_base_url) as engine:
        with pytest.raises(raises):
            PS._execute_persistence_txn(
                engine,
                verify_identity=False,
                build_inputs=lambda _conn: (
                    plan,
                    candidate_set,
                    ConfigPayloadPublisher(root),
                ),
                upstream_resolver=_conflating_resolver,
            )
        assert sorted(root.glob("*.json")) == [], "published artifacts survived a failed persist"
        counts = _baseline_counts(engine)
        assert counts["plans"] == 0
        assert counts["members"] == 0
        assert counts["payloads"] == 0


# --------------------------------------------------------------------------- #
# the least-privilege runner resolves the prepared canary at the required revision
# --------------------------------------------------------------------------- #
_CI_ROLE = "minos_runner_ci_svc"


def test_the_least_privilege_runner_resolves_the_prepared_canary(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """End of the chain: a real ``minos_runner``-only LOGIN resolves the canary it must execute.

    The resolution function is unchanged by 0012 — it joins by persisted lineage ids
    (``pm.id = j.plan_member_id``, ``dr.id = pm.dataset_registry_id``) and returns the PLAN-LOCAL
    ordinal, which is exactly what ``job_key`` is computed over. This proves that stays true once
    the plan member's source ordinal is a separate column pointing at matrix row 0.

    No GATK: the job is claimed and resolved, not executed.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from minos_engine.baseline.phase_a import build_phase_a_authority, build_phase_a_plan
    from minos_engine.storage.l2f2_runner import (
        BASELINE_REVISION,
        authorize_baseline_runner_connection,
    )
    from minos_engine.storage.l2f_job_claim import _claim_next_job_with_trust

    assert BASELINE_REVISION == _SOURCE_INDEX
    authority = build_phase_a_authority()
    plan = build_phase_a_plan()
    root = _provisioned_root(tmp_path)

    with _accepted_upstream_baseline(isolated_pg_base_url) as engine:
        prepare_l2f2_phase_a_canary(engine, config_artifact_root=root)
        claimed = _claim_next_job_with_trust(engine, plan, worker_id="ci-worker-0")
        assert claimed is not None
        assert claimed.job_key == authority.canary.job_key

        url = make_url(str(engine.url))
        with engine.connect() as conn, conn.begin():
            conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))
            conn.execute(
                text(
                    f"CREATE ROLE {_CI_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOBYPASSRLS INHERIT"
                )
            )
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {_CI_ROLE}'))
            conn.execute(text(f"GRANT minos_runner TO {_CI_ROLE}"))
        service = create_engine(url.set(username=_CI_ROLE, password=""))
        try:
            with service.connect() as conn:
                # exact database + exact revision + exact membership, all fail-closed.
                authorize_baseline_runner_connection(conn)
                resolved = (
                    conn.execute(
                        text(
                            "SELECT * FROM experiments.l2f2_resolve_claimed_execution(:h, :j, :w)"
                        ),
                        {"h": plan.plan_hash, "j": claimed.job_id, "w": "ci-worker-0"},
                    )
                    .mappings()
                    .one()
                )
            # the PLAN-LOCAL ordinal is what the runner sees, and what job_key was computed over.
            assert int(resolved["member_index"]) == 0 == authority.canary.member_index
            assert resolved["dataset_id"] == _PHASE_A_TOPOLOGY[0][1]
            assert resolved["chromosome"] == "chr18"
            assert resolved["config_hash"] == authority.canary.config_hash
        finally:
            service.dispose()
            with engine.connect() as conn, conn.begin():
                conn.execute(text(f'REVOKE ALL ON DATABASE "{url.database}" FROM {_CI_ROLE}'))
                conn.execute(text(f"REVOKE minos_runner FROM {_CI_ROLE}"))
                conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))
