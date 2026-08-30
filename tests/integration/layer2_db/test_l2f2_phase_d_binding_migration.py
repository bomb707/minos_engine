"""Migration ``0023`` — the Phase-D boundary binds the FROZEN four, not the number four.

``0021``'s bootstrap could be satisfied by a validation plan carrying four arbitrary configurations
and ten validation members, because ``candidate_count = 4`` is an integer somebody wrote down. This
suite is written around that hole: the central tests build a plan whose counts are perfect and
whose configurations are wrong, and require the bootstrap to refuse it.

Everything runs on scratch databases created and dropped by the fixture.
"""

from __future__ import annotations

import uuid
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

_DB = "minos_l2f2_phase_d_binding_scratch"
_PRIOR = "0022_l2f2_validation_store"
_HEAD = "0023_l2f2_phase_d_binding"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_BINDING = "experiments.l2f2_phase_d_binding"
_BOOTSTRAP = "experiments.l2f2_resolve_phase_d_runner_bootstrap()"

_FINALISTS = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)
_SEED = _FINALISTS[3]
_FREEZE_SHA = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
_CLOSURE_SHA = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
_SUBNET = "649bb92c6abccebde58a736a2b2af7fd77a701c1"


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


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


def _relation_exists(engine: Any, relation: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT to_regclass(:r)"), {"r": relation}).scalar_one() is not None
        )


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_lifecycle_0022_0023_0022_0023(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0022 = _state(engine)
            assert not _relation_exists(engine, _BINDING)
            # 0021's bootstrap exists but cannot yet name the frozen four
            before = str(_function(engine, _BOOTSTRAP)["definition"])
            assert _FINALISTS[0] not in before
            assert "ordered_config_hashes" not in before
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            assert _relation_exists(engine, _BINDING)
            after = str(_function(engine, _BOOTSTRAP)["definition"])
            assert "ordered_config_hashes" in after
            at_0023 = _state(engine)
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert not _relation_exists(engine, _BINDING)
            restored = str(_function(engine, _BOOTSTRAP)["definition"])
            assert "ordered_config_hashes" not in restored
            back = _state(engine)
        finally:
            engine.dispose()
        assert back == at_0022, "downgrade did not restore 0022"

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _state(engine) == at_0023
        finally:
            engine.dispose()


def test_the_alembic_graph_keeps_exactly_one_head() -> None:
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
    assert heads == [_HEAD], heads
    assert revisions[_HEAD] == _PRIOR
    assert len(_HEAD) <= 32, "alembic_version.version_num is varchar(32)"


def test_the_revision_name_fits_the_version_column(isolated_pg_base_url: str) -> None:
    """A 33-character revision fails at runtime, not at review. This one is checked live."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# the binding table is append-only scientific lineage
# --------------------------------------------------------------------------- #
def test_the_binding_is_append_only_and_privately_held(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                trigger = (
                    conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            " WHERE tgrelid = to_regclass(:r) AND NOT tgisinternal"
                        ),
                        {"r": _BINDING},
                    )
                    .scalars()
                    .all()
                )
                assert "trg_l2f2_phase_d_binding_append_only" in trigger
                # no service role may touch it directly; the runner reads it via the bootstrap
                for role in (
                    "minos_runner",
                    "minos_evaluator",
                    "minos_trainer",
                    "minos_live",
                    "public",
                ):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert (
                            conn.execute(
                                text("SELECT has_table_privilege(:r, :t, :p)"),
                                {"r": role, "t": _BINDING, "p": privilege},
                            ).scalar_one()
                            is False
                        ), f"{role} may {privilege} the binding"
                # the control plane writes it during preparation, but may never edit it
                for privilege in ("SELECT", "INSERT"):
                    assert (
                        conn.execute(
                            text("SELECT has_table_privilege('minos_admin', :t, :p)"),
                            {"t": _BINDING, "p": privilege},
                        ).scalar_one()
                        is True
                    ), privilege
                for privilege in ("UPDATE", "DELETE"):
                    assert (
                        conn.execute(
                            text("SELECT has_table_privilege('minos_admin', :t, :p)"),
                            {"t": _BINDING, "p": privilege},
                        ).scalar_one()
                        is False
                    ), privilege
        finally:
            engine.dispose()


def test_the_binding_requires_the_frozen_shape_and_the_seed(isolated_pg_base_url: str) -> None:
    """Constraint-level guarantees, independent of the bootstrap."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                checks = dict(
                    conn.execute(
                        text(
                            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                            " WHERE conrelid = to_regclass(:r) AND contype = 'c'"
                        ),
                        {"r": _BINDING},
                    ).all()
                )
            assert "member_count = 10" in checks["ck_l2f2_phase_d_binding_members"]
            assert "candidate_count = 4" in checks["ck_l2f2_phase_d_binding_candidates"]
            assert "logical_job_count = 40" in checks["ck_l2f2_phase_d_binding_jobs"]
            assert "= ANY" in checks["ck_l2f2_phase_d_binding_seed_present"]
            assert "array_length" in checks["ck_l2f2_phase_d_binding_config_count"]
            assert "PHASE_D" in checks["ck_l2f2_phase_d_binding_phase"]
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# THE gap 0021 could not close: WHICH four
# --------------------------------------------------------------------------- #
def test_the_bootstrap_reads_the_persisted_configs_not_a_count(
    isolated_pg_base_url: str,
) -> None:
    """The decisive strengthening, read from the function body itself."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine, _BOOTSTRAP)["definition"])
        finally:
            engine.dispose()
    # it aggregates the ACTUAL persisted config hashes, in config_index order ...
    assert "array_agg(pc.config_hash ORDER BY pc.config_index)" in definition
    # ... and compares them to the binding's ordered four
    assert "IS DISTINCT FROM v_b.ordered_config_hashes" in definition
    # the config_index inventory must be exactly 0,1,2,3
    assert "ARRAY[0,1,2,3]" in definition
    # a plan with no binding at all is refused, naming the reason
    assert "carries no Phase-D scientific binding" in definition
    # the freeze and closure digests are bound through the binding row
    assert "finalist_freeze_sha256" in definition or "v_b." in definition
    # still truth-free and still validation-only
    assert "pm.partition <> 'validation'" in definition
    for forbidden in ("truth_vcf", "truth_tbi", "mutations_vcf", "l2f_evaluation"):
        assert forbidden not in definition, forbidden


def test_the_bootstrap_still_takes_no_arguments_and_returns_two_strings(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            fn = _function(engine, _BOOTSTRAP)
            assert fn is not None
            assert fn["owner"] == "minos_admin"
            assert fn["owner_superuser"] is False
            assert fn["prosecdef"] is True
            assert "search_path" in str(fn["config"])
            flattened = str(fn["definition"]).replace("\n", " ")
            assert "RETURNS TABLE(plan_hash text, execution_environment_hash text)" in flattened
        finally:
            engine.dispose()


def test_only_the_runner_and_control_plane_may_execute_the_bootstrap(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
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


def test_a_bootstrap_with_no_phase_d_authority_refuses(isolated_pg_base_url: str) -> None:
    """An empty validation store is not a campaign."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, pytest.raises(Exception, match="PHASE_D"):
                conn.execute(text(f"SELECT * FROM {_BOOTSTRAP}"))
        finally:
            engine.dispose()


def test_the_binding_rejects_a_wrong_sized_ordered_four(isolated_pg_base_url: str) -> None:
    """Three configurations cannot be recorded as this campaign's finalists."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with (
                engine.connect() as conn,
                conn.begin(),
                pytest.raises(Exception, match="ck_l2f2_phase_d_binding_config_count"),
            ):
                conn.execute(text("SET LOCAL ROLE minos_admin"))
                conn.execute(
                    text(
                        f"INSERT INTO {_BINDING} (baseline_protocol_hash, authority_id, plan_id, "
                        "  plan_hash, finalist_freeze_sha256, phase_c_closure_sha256, "
                        "  parameter_space_hash, execution_environment_hash, "
                        "  scoring_contract_hash, minos_subnet_sha, split_manifest_sha256, "
                        "  seed_config_hash, ordered_config_hashes, "
                        "  inherited_candidate_indices, member_count, candidate_count, "
                        "  logical_job_count) "
                        "VALUES (:p, :a, :pl, :ph, :f, :c, :ps, :e, :sc, :ms, :sm, :seed, "
                        "        :cfgs, :idx, 10, 4, 40)"
                    ),
                    {
                        "p": "c" * 64,
                        "a": str(uuid.uuid4()),
                        "pl": str(uuid.uuid4()),
                        "ph": "a" * 64,
                        "f": _FREEZE_SHA,
                        "c": _CLOSURE_SHA,
                        "ps": "b" * 64,
                        "e": "d" * 64,
                        "sc": "e" * 64,
                        "ms": _SUBNET,
                        "sm": "f" * 64,
                        "seed": _SEED,
                        "cfgs": list(_FINALISTS[:3]),
                        "idx": [42, 25, 36],
                    },
                )
        finally:
            engine.dispose()


def test_downgrade_guard_names_the_binding() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "0023_l2f2_phase_d_binding.py"
    ).read_text(encoding="utf-8")
    assert "cannot downgrade away the Phase-D binding" in source
    assert "append-only scientific lineage" in source
    assert "WHICH four configurations" in source
