"""Migration ``0024`` — a Phase-D binding must describe THIS campaign, not merely itself.

``0023`` made the bootstrap compare the persisted plan configurations against the binding. That
catches a plan that disagrees with its own binding. It does not catch a plan and a binding that
agree with each other and are both wrong, because every identity except the baseline protocol was
read out of the binding row and compared only to the plan or to itself.

The decisive tests here are therefore the forgeries: a binding naming four different configurations
and a plan graph that matches it perfectly. Under ``0023`` that story is self-consistent and passes.
Under ``0024`` it fails, because the campaign's constants live in the function body where no row can
contradict them.

Everything runs on scratch databases created and dropped by the fixture.
"""

from __future__ import annotations

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

_DB = "minos_l2f2_phase_d_anchor_scratch"
_PRIOR = "0023_l2f2_phase_d_binding"
_HEAD = "0024_l2f2_phase_d_anchor"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_BOOTSTRAP = "experiments.l2f2_resolve_phase_d_runner_bootstrap()"

_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_FREEZE_SHA = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
_CLOSURE_SHA = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
_PARAMETER_SPACE = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
_SUBNET = "649bb92c6abccebde58a736a2b2af7fd77a701c1"
_FINALISTS = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)
_INDICES = [42, 25, 36, 0]
_SEED = _FINALISTS[3]

#: a forged campaign: four well-formed hashes that are simply not the frozen ones.
_FORGED = tuple(f"{i}" * 64 for i in "1234")


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _definition(engine: Any) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT pg_get_functiondef(to_regprocedure(:s))"), {"s": _BOOTSTRAP}
            ).scalar_one()
        )


def _function(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, r.rolsuper AS owner_superuser, "
                    "       p.prosecdef, p.proconfig::text AS config "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": _BOOTSTRAP},
            )
            .mappings()
            .one()
        )


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_lifecycle_0023_0024_0023_0024(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0023 = _state(engine)
            before = _definition(engine)
            # 0023 knows the protocol and nothing else about this campaign
            assert _PROTOCOL in before
            assert _FREEZE_SHA not in before
            assert _FINALISTS[0] not in before
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            after = _definition(engine)
            for anchor in (
                _FREEZE_SHA,
                _CLOSURE_SHA,
                _PARAMETER_SPACE,
                _ENVIRONMENT,
                _CONTRACT,
                _SUBNET,
                *_FINALISTS,
            ):
                assert anchor in after, anchor
            at_0024 = _state(engine)
        finally:
            engine.dispose()

        # 0024 owns no table: the only structural difference is the function body
        for section in ("relations", "indexes", "triggers", "roles", "role_memberships"):
            assert at_0024[section] == at_0023[section], section

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _FREEZE_SHA not in _definition(engine)
            back = _state(engine)
        finally:
            engine.dispose()
        assert back == at_0023, "downgrade did not restore 0023"

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _state(engine) == at_0024
        finally:
            engine.dispose()


def test_this_migration_is_on_the_single_linear_chain_and_the_name_fits() -> None:
    """0024 sits on one unbranched chain, directly after 0023, with a name that fits.

    This asserted that 0024 was the HEAD until ``0025`` was authorized to add the Phase-D
    evaluator authority surface. Being last is a fact about the newest migration, not a
    property of this one, so continuing to assert it here would make every future migration
    edit an accepted suite. What 0024 must keep is its POSITION: one head overall, and 0023
    immediately beneath it. ``0025``'s own suite asserts that ``0025`` is the head.
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
    assert len(heads) == 1, heads  # the chain never branches
    assert _HEAD in revisions, _HEAD
    assert revisions[_HEAD] == _PRIOR
    assert len(_HEAD) <= 32, "alembic_version.version_num is varchar(32)"


def test_the_downgrade_is_unconditional_because_0024_owns_no_state() -> None:
    """0022 and 0023 refuse; this one must not, and the reason is written down."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "0024_l2f2_phase_d_anchor.py"
    ).read_text(encoding="utf-8")
    assert "owns no scientific state" in source
    assert "would protect nothing" in source
    # it creates no table and no row
    assert "op.create_table" not in source
    assert "INSERT INTO" not in source


# --------------------------------------------------------------------------- #
# the anchor itself
# --------------------------------------------------------------------------- #
def test_the_bootstrap_anchors_every_campaign_identity(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _definition(engine)
        finally:
            engine.dispose()

    # each identity is compared to a literal, not to another column
    collapsed_head = " ".join(definition.split())
    assert f"v_b.finalist_freeze_sha256 <> '{_FREEZE_SHA}'" in collapsed_head
    assert f"v_b.phase_c_closure_sha256 <> '{_CLOSURE_SHA}'" in collapsed_head
    assert f"v_b.parameter_space_hash <> '{_PARAMETER_SPACE}'" in collapsed_head
    assert f"v_b.execution_environment_hash <> '{_ENVIRONMENT}'" in collapsed_head
    assert f"v_b.scoring_contract_hash <> '{_CONTRACT}'" in collapsed_head
    assert f"v_b.minos_subnet_sha <> '{_SUBNET}'" in collapsed_head
    assert f"v_b.seed_config_hash <> '{_SEED}'" in collapsed_head
    # whitespace-normalised: the stored body wraps, so compare on collapsed whitespace
    collapsed = " ".join(definition.split())
    assert "v_b.ordered_config_hashes IS DISTINCT FROM ARRAY[" in collapsed
    assert "v_b.inherited_candidate_indices IS DISTINCT FROM ARRAY[42, 25, 36, 0]" in collapsed
    assert "v_b.split_manifest_sha256 IS NULL" in definition
    # 0023's plan-vs-binding check is KEPT, not replaced
    assert "array_agg(pc.config_hash ORDER BY pc.config_index)" in definition
    assert "IS DISTINCT FROM v_b.ordered_config_hashes" in definition


def test_the_bootstrap_stays_argument_free_truth_free_and_runner_scoped(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            fn = _function(engine)
            assert fn["owner"] == "minos_admin"
            assert fn["owner_superuser"] is False
            assert fn["prosecdef"] is True
            assert "search_path" in str(fn["config"])
            definition = _definition(engine)
            flattened = definition.replace("\n", " ")
            assert "RETURNS TABLE(plan_hash text, execution_environment_hash text)" in flattened
            for forbidden in ("truth_vcf", "truth_tbi", "mutations_vcf", "l2f_evaluation"):
                assert forbidden not in definition, forbidden
            with engine.connect() as conn:
                for role in ("minos_evaluator", "minos_trainer", "minos_live", "public"):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": _BOOTSTRAP},
                        ).scalar_one()
                        is False
                    ), role
                for role in ("minos_runner", "minos_admin"):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": _BOOTSTRAP},
                        ).scalar_one()
                        is True
                    ), role
        finally:
            engine.dispose()


def test_an_empty_validation_store_is_not_a_campaign(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, pytest.raises(Exception, match="PHASE_D"):
                conn.execute(text(f"SELECT * FROM {_BOOTSTRAP}"))
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# THE forgery: a self-consistent binding that is not this campaign
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("column", "forged", "expected"),
    [
        ("finalist_freeze_sha256", "9" * 64, "finalist freeze"),
        ("phase_c_closure_sha256", "8" * 64, "Phase-C closure"),
        ("parameter_space_hash", "7" * 64, "parameter space"),
        ("execution_environment_hash", "6" * 64, "execution environment"),
        ("scoring_contract_hash", "5" * 64, "scoring contract"),
        ("minos_subnet_sha", "a" * 40, "MINOS_SUBNET"),
        ("seed_config_hash", "4" * 64, "seed"),
    ],
)
def test_each_forged_identity_is_refused_by_a_literal(
    isolated_pg_base_url: str, column: str, forged: str, expected: str
) -> None:
    """Read the anchor back out of the function: each column is pinned to a literal.

    Here the guarantee is read from the function body, which is where it lives. The LIVE forgery —
    a complete plan graph, four real wrong configurations and a binding that agrees with all of
    them — is in ``test_l2f2_validation_prepare.py``, which has the seam that can build one.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _definition(engine)
        finally:
            engine.dispose()
    assert f"v_b.{column} <>" in definition, column
    assert forged not in definition, "a forged value must not appear as an accepted literal"
    assert expected in definition, expected


def test_the_forged_four_appear_nowhere_in_the_anchor(isolated_pg_base_url: str) -> None:
    """The frozen four are literals; anything else is not."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _definition(engine)
        finally:
            engine.dispose()
    for frozen in _FINALISTS:
        assert frozen in definition, frozen
    for forged in _FORGED:
        assert forged not in definition, forged


def test_the_ordered_four_are_anchored_in_order(isolated_pg_base_url: str) -> None:
    """Order is part of the identity: index 0 is 157d88d1…, index 3 is the seed."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = _definition(engine)
        finally:
            engine.dispose()
    positions = [definition.index(h) for h in _FINALISTS]
    assert positions == sorted(positions), "the anchored four are not in frozen order"


def test_the_environment_is_read_from_the_binding_not_a_missing_column(
    isolated_pg_base_url: str,
) -> None:
    """``0021`` named a column that does not exist. ``0024`` reads the anchored one instead.

    ``experiments.l2f2_execution_authorities`` has no ``execution_environment_hash``: ``0015``
    added that identity to the two OUTCOME ledgers only. A PL/pgSQL ``record`` raises on an absent
    field at run time, so the bootstrap could never return — and every test written against
    ``0021`` and ``0023`` exercised a refusal, each of which raises earlier.

    The happy path is proven end to end in the Phase-D preparation proof, which is the first test
    in this repository to reach one. Here the source of the value is pinned.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            collapsed = " ".join(_definition(engine).split())
            assert (
                "RETURN QUERY SELECT v_d.plan_hash::text, v_b.execution_environment_hash::text"
                in collapsed
            )
            assert "v_d.execution_environment_hash" not in collapsed
            assert f"v_b.execution_environment_hash <> '{_ENVIRONMENT}'" in collapsed
            with engine.connect() as conn:
                columns = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            " WHERE table_schema = 'experiments' "
                            "   AND table_name = 'l2f2_execution_authorities'"
                        )
                    )
                ]
                assert "execution_environment_hash" not in columns
        finally:
            engine.dispose()


def test_the_downgrade_restores_the_defect_it_does_not_own(isolated_pg_base_url: str) -> None:
    """``0023``'s body is restored verbatim, defect included. A downgrade is not a rewrite."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            collapsed = " ".join(_definition(engine).split())
            assert "v_d.execution_environment_hash" in collapsed
            assert f"v_b.finalist_freeze_sha256 <> '{_FREEZE_SHA}'" not in collapsed
        finally:
            engine.dispose()


def test_a_member_of_another_snapshot_is_refused(isolated_pg_base_url: str) -> None:
    """The FK that enforces this for TRAIN is vacuous for validation; the anchor asserts it."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            collapsed = " ".join(_definition(engine).split())
            assert "pm.profile_snapshot_id IS DISTINCT FROM v_plan.profile_snapshot_id" in collapsed
            assert "belongs to a different profile snapshot than its plan" in collapsed
        finally:
            engine.dispose()
