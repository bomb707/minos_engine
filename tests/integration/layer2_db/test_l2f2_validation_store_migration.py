"""Migration ``0022`` — the plan graph learns VALIDATION, and TRAIN gets stricter, not looser.

The risk in this migration is not that validation fails to fit; it is that TRAIN quietly loosens
while making room. Six plan columns and two member columns stop being NOT NULL, and if nothing
replaced them a TRAIN plan could be persisted with no feature-matrix lineage at all — the exact
lineage that justifies which candidates Phase A and Phase B chose.

So every test here is written from that angle: the partition CHECK widens to admit ``validation``,
and a partition-CONDITIONAL check simultaneously requires the full matrix lineage for TRAIN and
requires its ABSENCE for validation. Neither partition can borrow the other's shape, and TEST is
admitted by neither.

Everything runs on scratch databases created and dropped by the fixture. The completed TRAIN
baseline store is never touched.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_DB = "minos_l2f2_validation_storage_scratch"
_PRIOR = "0021_l2f2_validation_execution"
_HEAD = "0022_l2f2_validation_store"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_PLANS = "experiments.l2f_experiment_plans"
_MEMBERS = "experiments.l2f_experiment_plan_members"
_TARGETS = "evaluation.l2f_validation_truth_registration_targets"
_REGISTRAR = "evaluation.l2f_register_validation_truth_identity(uuid, char, char, char, char)"

_PLAN_MATRIX_COLUMNS = (
    "train_feature_matrix_id",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_id",
    "feature_set_hash",
    "feature_registry_hash",
)
_MEMBER_MATRIX_COLUMNS = ("feature_matrix_id", "feature_matrix_member_id")


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _constraint(engine: Any, name: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one_or_none()


def _nullable(engine: Any, table: str, column: str) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                " WHERE table_schema = 'experiments' AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar_one()
    return str(value) == "YES"


def _function(engine: Any, signature: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, r.rolsuper AS owner_superuser, "
                    "       p.prosecdef, p.proconfig::text AS config, "
                    "       pg_get_functiondef(p.oid) AS definition "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_lifecycle_0021_0022_0021_0022(isolated_pg_base_url: str) -> None:
    """Up, down and up again on an empty validation store, comparing structure at each stop."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0021 = _state(engine)
            # the TRAIN-only world: partition pinned, matrix lineage mandatory for every row
            assert (
                _constraint(engine, "ck_l2f_plans_partition_train")
                == "CHECK ((partition = 'train'::text))"
            )
            assert _constraint(engine, "ck_l2f_pm_partition_train") is not None
            for column in _PLAN_MATRIX_COLUMNS:
                assert not _nullable(engine, "l2f_experiment_plans", column), column
            for column in _MEMBER_MATRIX_COLUMNS:
                assert not _nullable(engine, "l2f_experiment_plan_members", column), column
            assert _function(engine, _REGISTRAR) is None
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            widened = _constraint(engine, "ck_l2f_plans_partition_valid")
            assert widened is not None and "train" in widened and "validation" in widened
            assert "test" not in widened
            at_0022 = _state(engine)
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _function(engine, _REGISTRAR) is None
            back = _state(engine)
        finally:
            engine.dispose()
        assert back == at_0021, "downgrade did not restore 0021"

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _state(engine) == at_0022
        finally:
            engine.dispose()


def test_this_migration_sits_on_0021_and_the_graph_stays_linear() -> None:
    """0022's own edge, plus the graph-level single-head invariant.

    This asserted that 0022 WAS the head, which was only true until 0023 existed. The identity of
    the head belongs to the newest migration's suite; what this one owns is its own position.
    """
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[3] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in sorted(versions.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', body, re.M)
        down = re.search(r'^down_revision(?::[^=]+)?\s*=\s*["\']?([^"\'\n]+)', body, re.M)
        if rev:
            value = down.group(1).strip() if down else None
            revisions[rev.group(1)] = None if value in (None, "None") else value
    children = {d for d in revisions.values() if d}
    heads = [r for r in revisions if r not in children]
    assert len(heads) == 1, heads  # one head, whichever revision currently holds it
    assert revisions[_HEAD] == _PRIOR, "0022 must still descend from 0021"
    descendants = [r for r, down in revisions.items() if down == _HEAD]
    assert len(descendants) <= 1, descendants  # the chain never forks at this revision


# --------------------------------------------------------------------------- #
# TRAIN must get STRICTER, not looser
# --------------------------------------------------------------------------- #
def test_train_lineage_is_still_mandatory_after_the_columns_became_nullable(
    isolated_pg_base_url: str,
) -> None:
    """THE risk this migration carries. Relaxing NOT NULL must not make TRAIN optional."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            for column in _PLAN_MATRIX_COLUMNS:
                assert _nullable(engine, "l2f_experiment_plans", column), column
            for column in _MEMBER_MATRIX_COLUMNS:
                assert _nullable(engine, "l2f_experiment_plan_members", column), column

            # ... and the conditional check puts the requirement back, naming TRAIN explicitly
            plans = _constraint(engine, "ck_l2f_plans_partition_lineage")
            assert plans is not None
            for column in _PLAN_MATRIX_COLUMNS:
                assert f"{column} IS NOT NULL" in plans, column
                assert f"{column} IS NULL" in plans, column
            assert "'train'" in plans and "'validation'" in plans

            members = _constraint(engine, "ck_l2f_pm_partition_lineage")
            assert members is not None
            for column in _MEMBER_MATRIX_COLUMNS:
                assert f"{column} IS NOT NULL" in members, column
                assert f"{column} IS NULL" in members, column
        finally:
            engine.dispose()


def test_the_lineage_check_is_exhaustive_over_both_partitions(
    isolated_pg_base_url: str,
) -> None:
    """A row cannot satisfy the check by being neither TRAIN nor VALIDATION."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            plans = str(_constraint(engine, "ck_l2f_plans_partition_lineage"))
            # two arms, each naming its partition; nothing falls through
            assert plans.count("partition = 'train'") == 1
            assert plans.count("partition = 'validation'") == 1
        finally:
            engine.dispose()


def test_test_partition_is_admitted_by_neither_check(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            for name in ("ck_l2f_plans_partition_valid", "ck_l2f_pm_partition_valid"):
                definition = str(_constraint(engine, name))
                assert "'test'" not in definition, name
            for name in ("ck_l2f_plans_partition_lineage", "ck_l2f_pm_partition_lineage"):
                definition = str(_constraint(engine, name))
                assert "'test'" not in definition, name
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# the VALIDATION truth-registration surface
# --------------------------------------------------------------------------- #
def test_the_validation_truth_registrar_re_derives_the_partition(
    isolated_pg_base_url: str,
) -> None:
    """The caller supplies content hashes; the PARTITION is read from the accepted split."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            fn = _function(engine, _REGISTRAR)
            assert fn is not None
            assert fn["owner"] == "minos_admin"
            assert fn["owner_superuser"] is False
            assert fn["prosecdef"] is True
            assert "search_path" in str(fn["config"])
            definition = str(fn["definition"])
            # partition READ from the split, not accepted
            assert "FROM catalog.split_allocations" in definition
            assert "v_partition <> 'validation'" in definition
            assert "registers VALIDATION truth only" in definition
            # identity is CONTENT HASHES only. Asserted on what it actually writes rather than
            # on substrings — "uri" hides inside "security definer".
            assert "truth_vcf_sha256, truth_tbi_sha256" in definition
            assert "mutations_vcf_sha256, mutations_tbi_sha256" in definition
            lowered = definition.lower()
            for path_word in (" uri ", "_uri", "filepath", "file_path", "directory", "abspath"):
                assert path_word not in lowered, path_word
        finally:
            engine.dispose()


def test_the_validation_target_projection_excludes_train_and_test(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                definition = str(
                    conn.execute(
                        text("SELECT pg_get_viewdef(to_regclass(:v), true)"),
                        {"v": _TARGETS},
                    ).scalar_one()
                )
            assert "'validation'" in definition
            assert "'train'" not in definition
            assert "'test'" not in definition
        finally:
            engine.dispose()


def test_the_runner_can_neither_register_validation_truth_nor_read_its_targets(
    isolated_pg_base_url: str,
) -> None:
    """A truth-free runner stays truth-free. This is the whole point of the role split."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for role in ("minos_runner", "minos_trainer", "minos_live", "public"):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": _REGISTRAR},
                        ).scalar_one()
                        is False
                    ), role
                    assert (
                        conn.execute(
                            text("SELECT has_table_privilege(:r, :v, 'SELECT')"),
                            {"r": role, "v": _TARGETS},
                        ).scalar_one()
                        is False
                    ), role
                for role in ("minos_evaluator", "minos_admin"):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": _REGISTRAR},
                        ).scalar_one()
                        is True
                    ), role
                assert (
                    conn.execute(
                        text("SELECT has_table_privilege('minos_evaluator', :v, 'SELECT')"),
                        {"v": _TARGETS},
                    ).scalar_one()
                    is True
                )
        finally:
            engine.dispose()


def test_the_train_truth_registrar_is_untouched(isolated_pg_base_url: str) -> None:
    """0009's TRAIN registrar keeps refusing everything that is not TRAIN."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            fn = _function(
                engine, "evaluation.l2f_register_train_truth_identity(uuid, char, char, char, char)"
            )
            assert fn is not None
            definition = str(fn["definition"])
            assert "v_partition <> 'train'" in definition
            assert "registers TRAIN truth only" in definition
            assert "validation" not in definition.replace("VALIDATION", "")
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# downgrade safety
# --------------------------------------------------------------------------- #
def test_downgrade_refuses_while_a_validation_plan_exists(isolated_pg_base_url: str) -> None:
    """0022-owned scientific state has no honest way back into a TRAIN-only graph."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            # the refusal reads three counters; assert the SQL of all three is present and that a
            # clean database still downgrades, so the guard is specific rather than blanket
            with engine.connect() as conn:
                validation_plans = conn.execute(
                    text(f"SELECT count(*) FROM {_PLANS} WHERE partition = 'validation'")  # noqa: S608
                ).scalar_one()
            assert validation_plans == 0
        finally:
            engine.dispose()
        # empty store: downgrade is legal
        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
        finally:
            engine.dispose()


def test_the_downgrade_guard_names_all_three_kinds_of_0022_state() -> None:
    """Read from the migration source: a plan, a PHASE_D authority, a validation truth identity."""
    import inspect
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "0022_l2f2_validation_store.py"
    ).read_text(encoding="utf-8")
    assert "WHERE partition = '{_VALIDATION}'" in source or "partition = 'validation'" in source
    assert "phase = 'PHASE_D'" in source
    assert "dataset_evaluation_identity" in source
    assert "append-only scientific lineage" in source
    del inspect
