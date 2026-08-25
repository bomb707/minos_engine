"""Migration ``0013`` — storing exactly what the pinned upstream scorer exposes.

``0009`` required all four AdvancedScorer components ``NOT NULL`` because MINOS_ENGINE computed
them itself. The pinned upstream implementation returns only the combined score, so keeping them
mandatory would force a local recomputation of the very formula the row is meant to attest.

The controls here prove the change is exactly that and nothing more: four columns become
nullable, one CHECK becomes NULL-tolerant, and the downgrade refuses rather than inventing
scientific values it cannot obtain.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.evaluation.truth_registration import register_train_truth_identities
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_evaluation_ledger import _persist, _register_truth
from tests.integration.layer2_db.test_l2f2_evaluation_ledger import (
    evaluated as _evaluated_fixture,
)
from tests.integration.layer2_db.test_l2f_execution import env as _env_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

env = _env_fixture
evaluated = _evaluated_fixture

_DB = "minos_l2f2_score_oracle"
_PRIOR = "0012_l2f_plan_member_source_idx"
_HEAD = "0013_l2f2_upstream_score_oracle"

_RESULTS = "evaluation.l2f_evaluation_results"
_COMPONENTS = ("core_score", "completeness_score", "fp_score", "quality_score")
_RANGE_CHECK = "ck_l2f_eval_results_components_range"

_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _nullability(engine: Any) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            str(row.column_name): str(row.is_nullable)
            for row in conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    " WHERE table_schema='evaluation' AND table_name='l2f_evaluation_results' "
                    "   AND column_name = ANY(:cols)"
                ),
                {
                    "cols": list(_COMPONENTS)
                    + ["minos_score", "minos_score_100", "overcall_penalty"]
                },
            )
        }


def _check_definition(engine: Any) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": _RANGE_CHECK},
        ).scalar_one_or_none()


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_empty_lifecycle_0012_0013_0012_0013(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0012 = _state(engine)
            assert all(_nullability(engine)[column] == "NO" for column in _COMPONENTS)
            assert "IS NULL" not in str(_check_definition(engine))
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            at_0013 = _state(engine)
            assert _revision(engine) == _HEAD
            nullability = _nullability(engine)
            assert all(nullability[column] == "YES" for column in _COMPONENTS)
            # what upstream genuinely produces stays mandatory.
            for required in ("minos_score", "minos_score_100", "overcall_penalty"):
                assert nullability[required] == "NO", required
            definition = str(_check_definition(engine))
            for column in _COMPONENTS:
                assert f"{column} IS NULL" in definition, column
            assert "overcall_penalty >= (0)" in definition
            assert "overcall_penalty IS NULL" not in definition
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _state(engine) == at_0012, "downgrade did not restore 0012"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _state(engine) == at_0013, "re-upgrade did not restore 0013"
        finally:
            engine.dispose()


def test_0013_changes_only_the_component_columns(isolated_pg_base_url: str) -> None:
    """No table, function, grant or role moves: 0013 is a nullability and CHECK change."""
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

    for section in (
        "roles",
        "role_memberships",
        "functions",
        "schema_security",
        "default_acls",
        "triggers",
        "indexes",
    ):
        assert before.get(section) == after.get(section), f"0013 altered {section!r}"
    changed = sorted(
        name
        for name in set(before["relations"]) | set(after["relations"])
        if json.dumps(before["relations"].get(name), sort_keys=True, default=str)
        != json.dumps(after["relations"].get(name), sort_keys=True, default=str)
    )
    assert changed == [_RESULTS], changed
    constraint_delta = [
        row
        for row in after["constraints"]
        if row not in before["constraints"] or row.get("name") == _RANGE_CHECK
    ]
    assert all(row.get("table") == "l2f_evaluation_results" for row in constraint_delta)


def test_generic_upgrade_reaches_0013_and_then_the_repository_head(
    isolated_pg_base_url: str,
) -> None:
    """0013 is reachable by name, and a later revision may sit above it.

    The baseline store keeps advancing, so this asserts what 0013 owns — that upgrading to it
    lands exactly on it — rather than pinning it as the repository head.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = tuple(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    assert len(heads) == 1, heads
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
        finally:
            engine.dispose()
        alembic_upgrade(url, "head")
        engine = _engine(url)
        try:
            assert _revision(engine) == heads[0]
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# behaviour: NULL components are storable, out-of-range values still are not
# --------------------------------------------------------------------------- #
def test_a_component_out_of_range_is_still_refused(isolated_pg_base_url: str) -> None:
    """Nullable is not unconstrained: a present component must still be a valid proportion."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_check_definition(engine))
            # both halves survive for every component: absent is allowed, out-of-range is not.
            for column in _COMPONENTS:
                assert f"{column} IS NULL" in definition
                assert f"{column} >= (0)" in definition or f"{column} >= 0" in definition
                assert f"{column} <= (1)" in definition or f"{column} <= 1" in definition
        finally:
            engine.dispose()


def test_a_ledger_with_upstream_scored_rows_refuses_to_downgrade(
    env: Any, evaluated: Any, tmp_path: Any
) -> None:
    """0012 requires all four components NOT NULL; an upstream-scored row cannot supply them.

    Rather than inventing values, the downgrade refuses and the database stays at 0013 with its
    row intact.
    """
    root = tmp_path / "practice"
    root.mkdir(exist_ok=True)
    _register_truth(env, root)
    register_train_truth_identities(env.engine, dataset_root=root)
    persisted = _persist(env, evaluated, tmp_path)
    assert persisted.created is True

    with env.engine.connect() as conn:
        row = (
            conn.execute(
                text(  # noqa: S608
                    "SELECT core_score, completeness_score, fp_score, quality_score, "
                    f"       minos_score_100, minos_score, overcall_penalty FROM {_RESULTS}"
                )
            )
            .mappings()
            .one()
        )
    assert all(row[column] is None for column in _COMPONENTS)
    assert float(row["minos_score_100"]) == pytest.approx(86.25)
    assert float(row["minos_score"]) == pytest.approx(0.8625)

    url = str(env.engine.url.render_as_string(hide_password=False))
    env.engine.dispose()
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        alembic_downgrade(url, _PRIOR)

    from sqlalchemy import create_engine

    env.engine = create_engine(url)
    assert _revision(env.engine) == _HEAD
    assert all(_nullability(env.engine)[column] == "YES" for column in _COMPONENTS)
    with env.engine.connect() as conn:
        assert int(conn.execute(text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one()) == 1  # noqa: S608


def test_a_ledger_with_no_upstream_scored_rows_still_downgrades(isolated_pg_base_url: str) -> None:
    """The refusal is targeted: an empty (or fully-componented) ledger is still reversible."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                assert int(conn.execute(text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one()) == 0  # noqa: S608
        finally:
            engine.dispose()
        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert all(_nullability(engine)[column] == "NO" for column in _COMPONENTS)
        finally:
            engine.dispose()
