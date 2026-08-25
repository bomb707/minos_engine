"""Migration ``0012`` — separating the plan-local ordinal from the source feature-matrix ordinal.

Every control here runs against real PostgreSQL on an ephemeral database. Nothing touches the
real baseline store, and no GATK, hap.py, truth or score is involved.

The property under test is representational, not cosmetic. Before ``0012`` one column carried
both index namespaces and the composite lineage FK forced them equal, so an authorized SUBSET
plan — Phase A at local ``0..4`` over source ``0/10/20/30/40`` — could not be stored at all.
After ``0012`` it can, which in turn means a database holding one can no longer be downgraded,
and that refusal is proven here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_DB = "minos_l2f_source_index"
_PRIOR = "0011_l2f2_runner_boundary"
_HEAD = "0012_l2f_plan_member_source_idx"

_MEMBERS = "experiments.l2f_experiment_plan_members"
_SOURCE = "source_matrix_member_index"
_MATRIX_FK = "fk_l2f_pm_matrix_member"
_LOCAL_UNIQUE = "uq_l2f_pm_plan_member_index"

#: the roles the structural-inventory control compares across the revision boundary.
_ROLES = ["minos_admin", "minos_runner", "minos_evaluator", "minos_trainer", "minos_live"]


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _member_columns(engine: Any) -> list[str]:
    with engine.connect() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    " WHERE table_schema='experiments' "
                    "   AND table_name='l2f_experiment_plan_members' ORDER BY column_name"
                )
            )
        ]


def _constraint_def(engine: Any, name: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one_or_none()


def _plan_member_columns(state: dict[str, Any]) -> list[dict[str, Any]]:
    relations: Any = state["relations"]
    return list(relations["experiments.l2f_experiment_plan_members"]["columns"])


def _column_positions(state: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(str(c["name"]), c["position"]) for c in _plan_member_columns(state)]


def _without_column_positions(state: dict[str, Any]) -> str:
    """The structural state with plan-member column ORDINAL POSITIONS blanked out."""
    import copy
    import json

    stripped = copy.deepcopy(state)
    relations: Any = stripped["relations"]
    for column in relations["experiments.l2f_experiment_plan_members"]["columns"]:
        column["position"] = None
    return json.dumps(stripped, sort_keys=True, default=str)


def _structural_state(engine: Any, dbname: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=dbname)


# --------------------------------------------------------------------------- #
# A. empty lifecycle
# --------------------------------------------------------------------------- #
def test_empty_lifecycle_0011_0012_0011_0012(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0011 = _structural_state(engine, _DB)
            assert _SOURCE not in _member_columns(engine)
            # at 0011 the lineage FK binds the PLAN-LOCAL ordinal — the conflation itself.
            fk_0011 = _constraint_def(engine, _MATRIX_FK) or ""
            assert fk_0011.startswith(
                "FOREIGN KEY (feature_matrix_member_id, feature_matrix_id, "
                "dataset_registry_id, member_index, feature_values_hash)"
            )
            assert _SOURCE not in fk_0011
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            at_0012 = _structural_state(engine, _DB)
            assert _revision(engine) == _HEAD
            assert _SOURCE in _member_columns(engine)
            fk = _constraint_def(engine, _MATRIX_FK) or ""
            assert fk.startswith(
                "FOREIGN KEY (feature_matrix_member_id, feature_matrix_id, "
                f"dataset_registry_id, {_SOURCE}, feature_values_hash)"
            ), f"the lineage FK does not bind the source ordinal: {fk}"
            # the referenced side is unchanged: still the matrix row's own ordinal.
            assert fk.split("REFERENCES")[1].strip() == (
                "profiling.feature_matrix_members(id, feature_matrix_id, "
                "dataset_registry_id, member_index, feature_values_hash)"
            )
            # the plan-local ordinal keeps its own unaltered uniqueness rule.
            assert _constraint_def(engine, _LOCAL_UNIQUE) == "UNIQUE (plan_id, member_index)"
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _SOURCE not in _member_columns(engine)
            assert _structural_state(engine, _DB) == at_0011, "downgrade did not restore 0011"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            # add -> drop -> add leaves the column at a later attnum; that ordinal position is a
            # PostgreSQL bookkeeping artifact with no bearing on the contract, and it is the ONLY
            # tolerated difference — everything else must match the first upgrade exactly.
            again = _structural_state(engine, _DB)
            assert _column_positions(again) != _column_positions(at_0012)
            assert _without_column_positions(again) == _without_column_positions(at_0012), (
                "re-upgrade did not restore 0012"
            )
        finally:
            engine.dispose()


def test_0012_changes_only_the_plan_member_lineage(isolated_pg_base_url: str) -> None:
    """0012 grants nothing and touches no other object: the delta is confined to one table."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            before = _structural_state(engine, _DB)
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            after = _structural_state(engine, _DB)
        finally:
            engine.dispose()

    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    # alembic's own revision marker necessarily moves; everything else that differs must be
    # explainable by the plan-member table alone.
    for section in changed:
        if section == "alembic_version":
            continue
        rendered = f"{before.get(section)!r}{after.get(section)!r}"
        assert "l2f_experiment_plan_members" in rendered, (
            f"0012 changed structural section {section!r} unrelated to the plan-member lineage"
        )
    # explicitly unchanged: roles, memberships, and the 0011 runner authority surface.
    for section in ("roles", "role_memberships", "functions", "schema_security", "default_acls"):
        if section in before or section in after:
            assert before.get(section) == after.get(section), f"0012 altered {section!r}"


# --------------------------------------------------------------------------- #
# B. legacy rows backfill to source == local, and stay downgradable
# --------------------------------------------------------------------------- #
def test_legacy_full_plan_backfills_and_remains_downgradable(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """A plan persisted under 0011 backfills to ``source == local`` and can go back to 0011.

    The backfill is a restatement, not a guess: the 0011 FK made the two ordinals equal for every
    row it ever accepted. The seeded plan reaches ``member_index = 7`` so the highest ordinal is
    genuinely non-trivial.
    """
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
    from tests.integration.layer2_db.test_l2f_plan_store import (
        _CS,
        _provisioned_root,
        _publisher,
        _synthetic_plan,
    )

    spec = [(f"dsL{i}", "train", "chr18") for i in range(8)]
    plan = _synthetic_plan(spec)
    assert plan.train_member_count == 8
    assert [m.member_index for m in plan.members] == list(range(8))

    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            with engine.connect() as conn:
                legacy = [
                    int(r[0])
                    for r in conn.execute(
                        text(f"SELECT member_index FROM {_MEMBERS} ORDER BY member_index")  # noqa: S608
                    )
                ]
            assert legacy == list(range(8))
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                rows = [
                    (int(r.member_index), int(r.source_matrix_member_index))
                    for r in conn.execute(
                        text(  # noqa: S608
                            f"SELECT member_index, {_SOURCE} FROM {_MEMBERS} ORDER BY member_index"
                        )
                    )
                ]
            assert rows == [(i, i) for i in range(8)]
            assert rows[-1] == (7, 7)
            # the append-only trigger the backfill suspended is restored and still refuses DML.
            with (
                engine.connect() as conn,
                conn.begin(),
                pytest.raises(Exception, match="append-only|immutable|reject"),
            ):
                conn.execute(text("SET LOCAL ROLE minos_admin"))
                conn.execute(text(f"UPDATE {_MEMBERS} SET member_index = member_index"))  # noqa: S608
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _SOURCE not in _member_columns(engine)
            with engine.connect() as conn:
                assert [
                    int(r[0])
                    for r in conn.execute(
                        text(f"SELECT member_index FROM {_MEMBERS} ORDER BY member_index")  # noqa: S608
                    )
                ] == list(range(8))
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# C/D/E. the subset row the new FK admits, the one it rejects, and the refused downgrade
# --------------------------------------------------------------------------- #
@pytest.fixture
def prepared_phase_a(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    """A 0012 database holding the frozen Phase-A subset plan (local 0..4 / source 0/10/20/30/40)."""
    from minos_engine.storage.l2f2_canary_prepare import prepare_l2f2_phase_a_canary
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
    from tests.integration.layer2_db.test_l2f2_canary_prepare import _accepted_plan
    from tests.integration.layer2_db.test_l2f_plan_store import _provisioned_root

    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _accepted_plan())
            prepare_l2f2_phase_a_canary(engine, config_artifact_root=_provisioned_root(tmp_path))
            yield url, engine
        finally:
            engine.dispose()


def _unselected_matrix_member(engine: Any, source_index: int) -> dict[str, Any]:
    """The COMPLETE live lineage of an accepted TRAIN member at ``source_index``.

    Both plan-member FKs have to be satisfiable, so this carries the snapshot member and bam
    profile alongside the matrix row — only the ordinal is what the controls below vary.
    """
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT fmm.id, fmm.feature_matrix_id, fmm.dataset_registry_id, "
                    "       fmm.member_index, fmm.feature_values_hash, "
                    "       psm.id AS psm_id, psm.profile_snapshot_id, psm.bam_profile_id "
                    "  FROM profiling.feature_matrix_members fmm "
                    "  JOIN profiling.profile_snapshot_members psm "
                    "    ON psm.dataset_registry_id = fmm.dataset_registry_id "
                    "   AND psm.feature_values_hash = fmm.feature_values_hash "
                    "   AND psm.partition = 'train' "
                    " WHERE fmm.member_index = :i"
                ),
                {"i": source_index},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _insert_plan_member(engine: Any, fmm: dict[str, Any], *, local: int, source: int) -> None:
    """Insert one plan-member row binding a chosen local ordinal to a chosen source ordinal."""
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        plan_id = conn.execute(
            text(f"SELECT plan_id FROM {_MEMBERS} ORDER BY member_index LIMIT 1")  # noqa: S608
        ).scalar_one()
        conn.execute(
            text(
                f"INSERT INTO {_MEMBERS} (plan_id, profile_snapshot_id, feature_matrix_id, "  # noqa: S608
                " profile_snapshot_member_id, feature_matrix_member_id, bam_profile_id, "
                " dataset_registry_id, partition, feature_values_hash, member_index, "
                f" {_SOURCE}) "
                "VALUES (:plan_id, :snapshot, :matrix, :psm, :fmm, :bam, :dsr, 'train', "
                "        :fvh, :local, :source)"
            ),
            {
                "plan_id": plan_id,
                "snapshot": fmm["profile_snapshot_id"],
                "matrix": fmm["feature_matrix_id"],
                "psm": fmm["psm_id"],
                "fmm": fmm["id"],
                "bam": fmm["bam_profile_id"],
                "dsr": fmm["dataset_registry_id"],
                "fvh": fmm["feature_values_hash"],
                "local": local,
                "source": source,
            },
        )


def test_the_new_fk_admits_a_row_whose_namespaces_differ(prepared_phase_a: Any) -> None:
    """C: local 33 ≠ source 33 is not the point — a row may bind ANY local ordinal to the source
    ordinal its referenced matrix row actually has."""
    _url, engine = prepared_phase_a
    fmm = _unselected_matrix_member(engine, 33)
    assert int(fmm["member_index"]) == 33
    _insert_plan_member(engine, fmm, local=99, source=33)
    with engine.connect() as conn:
        stored = conn.execute(
            text(f"SELECT {_SOURCE} FROM {_MEMBERS} WHERE member_index = 99")  # noqa: S608
        ).scalar_one()
    assert int(stored) == 33


def test_the_new_fk_rejects_a_wrong_source_ordinal(prepared_phase_a: Any) -> None:
    """D: claiming a source ordinal the referenced matrix row does not have is a FK violation."""
    from sqlalchemy.exc import IntegrityError

    _url, engine = prepared_phase_a
    fmm = _unselected_matrix_member(engine, 33)
    with pytest.raises(IntegrityError, match=_MATRIX_FK):
        _insert_plan_member(engine, fmm, local=99, source=1)


def test_a_persisted_subset_plan_refuses_to_downgrade(prepared_phase_a: Any) -> None:
    """E: 0011 cannot represent local 0..4 over source 0/10/20/30/40, so the downgrade fails
    closed — no rows deleted, no ordinal rewritten, no namespace collapsed."""
    url, engine = prepared_phase_a
    with engine.connect() as conn:
        before = [
            (int(r.member_index), int(r.source_matrix_member_index))
            for r in conn.execute(
                text(  # noqa: S608
                    f"SELECT member_index, {_SOURCE} FROM {_MEMBERS} ORDER BY member_index"
                )
            )
        ]
    assert before == [(0, 0), (1, 10), (2, 20), (3, 30), (4, 40)]

    with pytest.raises(RuntimeError, match="cannot downgrade"):
        alembic_downgrade(url, _PRIOR)

    # the database is untouched: same revision, same column, same FK, same rows.
    assert _revision(engine) == _HEAD
    assert _SOURCE in _member_columns(engine)
    assert _SOURCE in str(_constraint_def(engine, _MATRIX_FK))
    with engine.connect() as conn:
        after = [
            (int(r.member_index), int(r.source_matrix_member_index))
            for r in conn.execute(
                text(  # noqa: S608
                    f"SELECT member_index, {_SOURCE} FROM {_MEMBERS} ORDER BY member_index"
                )
            )
        ]
    assert after == before


def test_generic_upgrade_head_reaches_0012(isolated_pg_base_url: str) -> None:
    """A plain ``alembic upgrade head`` — what CI runs — lands on 0012."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert list(ScriptDirectory.from_config(Config("alembic.ini")).get_heads()) == [_HEAD]
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, "head")
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
        finally:
            engine.dispose()
