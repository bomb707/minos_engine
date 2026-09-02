"""Migration ``0025`` — the one fact a least-privilege evaluator was missing.

``minos_evaluator`` can see 138 columns across every schema and ``plan_hash`` is in none of them.
The identity is bound cryptographically inside ``job_key`` and the execution result hash, but
recomputing either needs ``profile_id``, ``content_hash`` and the plan-local ``member_index`` —
fields the evaluator may not read and must not be given. So the strongest test available was
"validation partition, frozen member, frozen config", which two different validation plans over
the same ten members and four configurations both pass.

This migration adds one view with two columns and nothing else. The tests below hold it to that:
what it exposes, what it structurally cannot expose, who may read it, who may not, and that a
downgrade removes exactly it.
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

_DB = "minos_l2f2_phase_d_eval_auth_scratch"
_PRIOR = "0024_l2f2_phase_d_anchor"
_HEAD = "0025_l2f2_phase_d_eval_auth"
_VIEW = "evaluation.l2f_phase_d_execution_authority"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _viewdef(engine: Any) -> str:
    with engine.connect() as conn:
        return " ".join(
            str(
                conn.execute(
                    text("SELECT pg_get_viewdef(to_regclass(:v), true)"), {"v": _VIEW}
                ).scalar_one()
            ).split()
        )


# --------------------------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------------------------
def test_lifecycle_0024_0025_0024_0025(isolated_pg_base_url: str) -> None:
    """0024 -> 0025 -> 0024 -> 0025, with the view appearing and disappearing exactly once."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0024 = _state(engine)
            with engine.connect() as conn:
                assert (
                    conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one() is None
                ), "the view exists before 0025"

            alembic_upgrade(url, _HEAD)
            assert _revision(engine) == _HEAD
            first = _state(engine)
            with engine.connect() as conn:
                assert (
                    conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one()
                    is not None
                )

            alembic_downgrade(url, _PRIOR)
            assert _revision(engine) == _PRIOR
            assert _state(engine) == at_0024, "downgrade did not restore the accepted 0024 state"
            with engine.connect() as conn:
                assert (
                    conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one() is None
                )

            alembic_upgrade(url, _HEAD)
            assert _revision(engine) == _HEAD
            assert _state(engine) == first, "the second upgrade is not identical to the first"
        finally:
            engine.dispose()


def test_this_migration_is_the_head_and_its_name_fits(isolated_pg_base_url: str) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tests.conftest import REPO_ROOT

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert list(heads) == [_HEAD], heads
    # alembic_version.version_num is varchar(32); an exact-limit name is asking for truncation.
    assert len(_HEAD) < 32
    assert script.get_revision(_HEAD).down_revision == _PRIOR


def test_the_accepted_migrations_are_untouched() -> None:
    """0025 is additive. Nothing from 0001..0024 may be retrofitted."""
    import subprocess

    from tests.conftest import REPO_ROOT

    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "migrations/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert changed == [], changed


# --------------------------------------------------------------------------------------------
# what the view is
# --------------------------------------------------------------------------------------------
def test_the_view_exposes_exactly_two_columns(isolated_pg_base_url: str) -> None:
    """The smallest disclosure that can prove the missing invariant, and no larger."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                columns = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            " WHERE table_schema='evaluation' "
                            "   AND table_name='l2f_phase_d_execution_authority' "
                            " ORDER BY ordinal_position"
                        )
                    )
                ]
            assert columns == ["execution_result_id", "plan_hash"], columns
        finally:
            engine.dispose()


def test_the_view_discloses_no_truth_config_or_feature_identity(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _viewdef(engine).lower()
        finally:
            engine.dispose()
    for forbidden in (
        "truth",
        "mutations",
        "dataset_evaluation_identity",
        "config_payload",
        "artifact",
        "feature_values",
        "feature_matrix",
        "profile_document",
        "uri",
    ):
        assert forbidden not in definition, forbidden


def test_the_view_is_validation_only_by_construction(isolated_pg_base_url: str) -> None:
    """TRAIN and TEST are not rows a caller could filter badly — they are not rows."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _viewdef(engine)
        finally:
            engine.dispose()
    assert "partition = 'validation'" in definition
    assert "'train'" not in definition
    assert "'test'" not in definition


def test_plan_hash_comes_from_the_persisted_plan_not_a_parameter(
    isolated_pg_base_url: str,
) -> None:
    """No caller, session parameter or argument may contribute the plan identity."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _viewdef(engine)
        finally:
            engine.dispose()
    assert "p.id = r.plan_id" in definition or "r.plan_id = p.id" in definition
    for forbidden in ("current_setting", "$1", "coalesce(", "::text ="):
        assert forbidden not in definition, forbidden


# --------------------------------------------------------------------------------------------
# who may read it
# --------------------------------------------------------------------------------------------
def test_only_the_evaluator_may_read_the_authority_view(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for role, expected in (
                    ("minos_evaluator", True),
                    ("minos_runner", False),
                    ("minos_trainer", False),
                    ("minos_live", False),
                    ("public", False),
                ):
                    granted = conn.execute(
                        text("SELECT has_table_privilege(:r, :v, 'SELECT')"),
                        {"r": role, "v": _VIEW},
                    ).scalar_one()
                    assert granted is expected, (role, granted)

                # no DML surface for anyone but the owner.
                for role in ("minos_evaluator", "minos_runner", "minos_trainer", "minos_live"):
                    for privilege in ("INSERT", "UPDATE", "DELETE"):
                        assert (
                            conn.execute(
                                text("SELECT has_table_privilege(:r, :v, :p)"),
                                {"r": role, "v": _VIEW, "p": privilege},
                            ).scalar_one()
                            is False
                        ), (role, privilege)
        finally:
            engine.dispose()


def test_the_view_is_owned_by_the_non_superuser_control_plane(
    isolated_pg_base_url: str,
) -> None:
    """Owner-defined: the evaluator reads through minos_admin's authority, not its own."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT pg_get_userbyid(c.relowner) AS owner, r.rolsuper, "
                            "       r.rolcanlogin "
                            "  FROM pg_class c "
                            "  JOIN pg_roles r ON r.oid = c.relowner "
                            " WHERE c.oid = to_regclass(:v)"
                        ),
                        {"v": _VIEW},
                    )
                    .mappings()
                    .one()
                )
            assert row["owner"] == "minos_admin"
            assert row["rolsuper"] is False
            assert row["rolcanlogin"] is False
        finally:
            engine.dispose()


def test_the_evaluator_still_has_no_direct_experiment_table_access(
    isolated_pg_base_url: str,
) -> None:
    """The whole point: expose the FACT, not the tables it is derived from."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for table in (
                    "experiments.l2f_experiment_plans",
                    "experiments.l2f_execution_results",
                    "experiments.l2f_experiment_jobs",
                    "experiments.l2f_experiment_plan_members",
                    "experiments.l2f_experiment_plan_configs",
                ):
                    assert (
                        conn.execute(
                            text("SELECT has_table_privilege('minos_evaluator', :t, 'SELECT')"),
                            {"t": table},
                        ).scalar_one()
                        is False
                    ), table
        finally:
            engine.dispose()


def test_no_security_definer_function_was_added(isolated_pg_base_url: str) -> None:
    """A view sufficed. 0025 adds no privileged function surface."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                before = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                            "  ON n.oid = p.pronamespace "
                            " WHERE p.prosecdef AND n.nspname IN ('evaluation','experiments')"
                        )
                    )
                }
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                after = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                            "  ON n.oid = p.pronamespace "
                            " WHERE p.prosecdef AND n.nspname IN ('evaluation','experiments')"
                        )
                    )
                }
            assert after == before, after ^ before
        finally:
            engine.dispose()


def test_the_historical_shared_projection_is_unchanged(isolated_pg_base_url: str) -> None:
    """0025 adds a view; it does not widen the TRAIN/shared evaluator projection."""
    shared = "evaluation.l2f_completed_execution_inputs"
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                before = str(
                    conn.execute(
                        text("SELECT pg_get_viewdef(to_regclass(:v), true)"), {"v": shared}
                    ).scalar_one()
                )
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                after = str(
                    conn.execute(
                        text("SELECT pg_get_viewdef(to_regclass(:v), true)"), {"v": shared}
                    ).scalar_one()
                )
            assert after == before
            assert "plan_hash" not in after
        finally:
            engine.dispose()


def test_the_evaluator_may_read_the_revision_it_is_pinned_to(isolated_pg_base_url: str) -> None:
    """A boundary cannot enforce a revision pin it may not read.

    ``0011`` granted exactly this to ``minos_runner`` for the same reason. alembic_version carries
    no scientific data, so SELECT is the whole grant — and the downgrade takes it back.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT has_table_privilege('minos_evaluator', "
                            "'public.alembic_version', 'SELECT')"
                        )
                    ).scalar_one()
                    is False
                )
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                for role, expected in (
                    ("minos_evaluator", True),
                    ("minos_runner", True),  # already granted by 0011
                    ("minos_trainer", False),
                    ("minos_live", False),
                    ("public", False),
                ):
                    assert (
                        conn.execute(
                            text(
                                "SELECT has_table_privilege(:r, 'public.alembic_version', 'SELECT')"
                            ),
                            {"r": role},
                        ).scalar_one()
                        is expected
                    ), role
                # read-only: the pin is a fact to read, never one to assert.
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    assert (
                        conn.execute(
                            text(
                                "SELECT has_table_privilege('minos_evaluator', "
                                "'public.alembic_version', :p)"
                            ),
                            {"p": privilege},
                        ).scalar_one()
                        is False
                    ), privilege

            alembic_downgrade(url, _PRIOR)
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT has_table_privilege('minos_evaluator', "
                            "'public.alembic_version', 'SELECT')"
                        )
                    ).scalar_one()
                    is False
                ), "downgrade left the grant behind"
        finally:
            engine.dispose()
