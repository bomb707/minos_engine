"""Migration ``0016`` — the runner boundary admits PHASE_B, and nothing else moves.

``0011`` said it plainly: *"the ONLY phase 0011 admits. A later phase is a later migration, never
a looser CHECK."* This is that migration, and these controls exist to prove it stayed as narrow as
that sentence — one more phase, one more resolver, one nullable column governed by a
phase-semantic rule, and no change to any role, grant, trigger or scientific table.

Two asymmetries are deliberate and are pinned here:

* ``0015`` REFUSES a populated store; ``0016`` must NOT. The real baseline holds a complete
  Phase-A campaign and nothing here reinterprets a single row of it.
* The upgrade is always allowed; the DOWNGRADE refuses once a Phase-B authority exists, because
  fitting that row back into a Phase-A-only CHECK would mean deleting or relabelling append-only
  scientific lineage.

No GATK and no scoring: outcomes come from ``FakeGatkRunner`` through the private test seam.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_baseline"
_PRIOR = "0015_l2f2_exec_environment"
_HEAD = "0016_l2f2_phase_b_execution"

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_RESOLVE_A = "l2f2_resolve_claimed_execution"
_RESOLVE_B = "l2f2_resolve_claimed_phase_b_execution"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_SCIENTIFIC_TABLES = (
    "experiments.l2f_experiment_plans",
    "experiments.l2f_experiment_plan_members",
    "experiments.l2f_experiment_plan_configs",
    "experiments.l2f_experiment_jobs",
    "experiments.l2f_execution_results",
    "experiments.l2f_execution_failures",
    _AUTHORITIES,
)


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


def _canary_nullable(engine: Any) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    " WHERE table_schema='experiments' AND table_name='l2f2_execution_authorities'"
                    "   AND column_name='canary_job_key'"
                )
            ).scalar_one()
        )


def _functions(engine: Any, proname: str) -> list[str]:
    with engine.connect() as conn:
        return sorted(
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
                    "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                    " WHERE n.nspname='experiments' AND p.proname=:n"
                ),
                {"n": proname},
            )
        )


def _counts(engine: Any) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            for table in _SCIENTIFIC_TABLES
        }


def _authority_rows(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM {_AUTHORITIES} ORDER BY created_at, id")  # noqa: S608
            ).mappings()
        ]


def _insert_phase_b_authority(engine: Any, *, plan_hash: str) -> None:
    """A minimal PHASE_B authority row, inserted as the control plane may.

    This suite tests the SCHEMA's phase boundary, so the row's scientific content is irrelevant
    and deliberately synthetic; the derived Phase-B authority has its own production boundary and
    its own tests.
    """
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count) "
                "SELECT a.baseline_protocol_hash, 'PHASE_B', a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, a.parameter_space_hash, "
                "       10, 48, 480 "
                f"  FROM {_AUTHORITIES} a WHERE a.plan_hash = :h AND a.phase = 'PHASE_A'"
            ),
            {"h": plan_hash},
        )


# --------------------------------------------------------------------------- #
# lifecycle on an EMPTY store
# --------------------------------------------------------------------------- #
def test_empty_lifecycle_0015_0016_0015_0016(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0015 = _state(engine)
            assert _canary_nullable(engine) == "NO"
            assert (
                _constraint(engine, "ck_l2f2_authority_phase")
                == "CHECK ((phase = 'PHASE_A'::text))"
            )
            assert _constraint(engine, "ck_l2f2_authority_canary_phase") is None
            assert _functions(engine, _RESOLVE_B) == []
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            at_0016 = _state(engine)
            assert _revision(engine) == _HEAD
            assert _canary_nullable(engine) == "YES"
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            assert phase is not None and "PHASE_A" in phase and "PHASE_B" in phase
            canary = _constraint(engine, "ck_l2f2_authority_canary_phase")
            assert canary is not None and "canary_job_key" in canary
            assert _functions(engine, _RESOLVE_B) == [
                "p_plan_hash text, p_job_id uuid, p_worker_id text"
            ]
            # the Phase-A resolver is byte-identically the one 0011 created.
            assert _functions(engine, _RESOLVE_A) == [
                "p_plan_hash text, p_job_id uuid, p_worker_id text"
            ]
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _state(engine) == at_0015, "downgrade did not restore 0015"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert json.dumps(_state(engine), sort_keys=True, default=str) == json.dumps(
                at_0016, sort_keys=True, default=str
            )
        finally:
            engine.dispose()


def test_0016_touches_only_the_authority_table_and_adds_exactly_one_function(
    isolated_pg_base_url: str,
) -> None:
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

    for section in ("roles", "role_memberships", "schema_security", "default_acls", "triggers"):
        assert before.get(section) == after.get(section), f"0016 altered {section!r}"

    changed = sorted(
        name
        for name in set(before["relations"]) | set(after["relations"])
        if json.dumps(before["relations"].get(name), sort_keys=True, default=str)
        != json.dumps(after["relations"].get(name), sort_keys=True, default=str)
    )
    assert changed == [_AUTHORITIES], changed

    def _by_name(state: Any) -> dict[str, Any]:
        return {f"{r['name']}({r['identity_arguments']})": r for r in state["functions"]}

    moved = sorted(set(_by_name(after)) ^ set(_by_name(before)))
    assert moved == [f"{_RESOLVE_B}(p_plan_hash text, p_job_id uuid, p_worker_id text)"]
    unchanged = sorted(
        key
        for key in set(_by_name(before)) & set(_by_name(after))
        if json.dumps(_by_name(before)[key], sort_keys=True, default=str)
        != json.dumps(_by_name(after)[key], sort_keys=True, default=str)
    )
    assert unchanged == [], f"0016 redefined {unchanged}"

    added = _by_name(after)[moved[0]]
    assert added["owner"] == "minos_admin", "a SECURITY DEFINER function runs as its OWNER"
    assert added["security_definer"] is True


# --------------------------------------------------------------------------- #
# what the widened CHECKs admit, and what they still refuse
# --------------------------------------------------------------------------- #
def test_0016_itself_admitted_exactly_two_phases_and_never_pre_admitted_the_third(
    isolated_pg_base_url: str,
) -> None:
    """``0016`` widened the vocabulary by exactly ONE value, and left the third to a migration.

    Pinned at ``0016``, not at head. ``0020`` has since delivered that third phase, so a store at
    head legitimately admits ``PHASE_C`` — which is the promise being kept, not broken. The claim
    worth keeping testable forever is the one about ``0016`` itself: on the day it shipped, the
    column was not quietly widened to hold a phase nothing could yet execute.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            canary = _constraint(engine, "ck_l2f2_authority_canary_phase")
        finally:
            engine.dispose()

    assert phase is not None and canary is not None
    assert "PHASE_A" in phase and "PHASE_B" in phase
    assert "PHASE_C" not in phase, "0016 pre-admitted a phase it could not execute"
    assert "VALIDATION" not in phase.upper() and "TEST" not in phase.upper()
    assert "PHASE_C" not in canary, "the canary rule pre-admitted a phase 0016 did not have"


@pytest.mark.parametrize("phase", ["PHASE_D", "validation", "phase_b", "TEST", ""])
def test_an_unknown_phase_is_still_refused_twice_over(l2f2: Any, phase: str) -> None:
    """The vocabulary is a closed set, not a free-text column — at 0016 and at every head since.

    An unknown phase is refused twice — by the phase vocabulary and by the canary rule, which
    names every admitted phase explicitly — so the assertion is on the refusal rather than on
    which of the two fires first. ``PHASE_C`` is deliberately absent from this list: it is a real
    phase now, and :mod:`tests.integration.layer2_db.test_l2f2_phase_c_execution_migration` owns
    the equivalent control for the next one.
    """
    from sqlalchemy.exc import IntegrityError

    definition = _constraint(l2f2.engine, "ck_l2f2_authority_phase")
    assert definition is not None
    assert "PHASE_A" in definition and "PHASE_B" in definition
    assert "VALIDATION" not in definition.upper()

    with (
        pytest.raises(IntegrityError, match="ck_l2f2_authority_(phase|canary_phase)"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count) "
                "SELECT a.baseline_protocol_hash, :phase, a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, "
                "       a.parameter_space_hash, 10, 48, 480 "
                f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'"
            ),
            {"phase": phase},
        )


def test_phase_a_must_carry_a_canary_and_phase_b_must_not(l2f2: Any) -> None:
    """The canary is a Phase-A concept. Neither direction is left to convention."""
    from sqlalchemy.exc import IntegrityError

    plan_hash = l2f2.plan.plan_hash

    # PHASE_B with a canary: refused.
    with (
        pytest.raises(IntegrityError, match="ck_l2f2_authority_canary_phase"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count, canary_job_key) "
                "SELECT a.baseline_protocol_hash, 'PHASE_B', a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, "
                "       a.parameter_space_hash, 10, 48, 480, a.canary_job_key "
                f"  FROM {_AUTHORITIES} a WHERE a.plan_hash = :h AND a.phase = 'PHASE_A'"
            ),
            {"h": plan_hash},
        )

    # PHASE_A without one: refused, exactly as the old NOT NULL did. (It cannot be tested by
    # UPDATE: the table is append-only and no role holds UPDATE on it at all.)
    with (
        pytest.raises(IntegrityError, match="ck_l2f2_authority_canary_phase"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count) "
                "SELECT a.baseline_protocol_hash, 'PHASE_A', a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, "
                "       a.parameter_space_hash, 5, 39, 195 "
                f"  FROM {_AUTHORITIES} a WHERE a.plan_hash = :h AND a.phase = 'PHASE_A'"
            ),
            {"h": plan_hash},
        )

    # and the legitimate Phase-B shape is accepted.
    _insert_phase_b_authority(l2f2.engine, plan_hash=plan_hash)
    rows = [r for r in _authority_rows(l2f2.engine) if r["phase"] == "PHASE_B"]
    assert len(rows) == 1
    assert rows[0]["canary_job_key"] is None


def test_one_authority_per_plan_and_phase_remains_enforced(l2f2: Any) -> None:
    from sqlalchemy.exc import IntegrityError

    _insert_phase_b_authority(l2f2.engine, plan_hash=l2f2.plan.plan_hash)
    with pytest.raises(IntegrityError, match="uq_l2f2_authority_plan"):
        _insert_phase_b_authority(l2f2.engine, plan_hash=l2f2.plan.plan_hash)


# --------------------------------------------------------------------------- #
# the POPULATED store — the shape the real baseline is in
# --------------------------------------------------------------------------- #
def test_a_populated_phase_a_store_migrates_and_nothing_scientific_moves(
    service: Any, l2f2: Any
) -> None:
    """Unlike 0015, 0016 must not refuse execution evidence. The real store is exactly this."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    for worker, runner in (
        ("ci-populated-success", FakeGatkRunner()),
        ("ci-populated-failure", FakeGatkRunner(exit_code=127)),
    ):
        dispatched = _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id=worker,
            runner=runner,
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )
        assert dispatched is not None

    before_counts = _counts(l2f2.engine)
    before_authorities = _authority_rows(l2f2.engine)
    assert before_counts["experiments.l2f_execution_results"] == 1
    assert before_counts["experiments.l2f_execution_failures"] == 1
    l2f2.engine.dispose()

    # down to 0015 with execution evidence present, then back up. The upgrade must succeed.
    alembic_downgrade(l2f2.url, _PRIOR)
    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _PRIOR
    at_0015_counts = _counts(l2f2.engine)
    l2f2.engine.dispose()

    alembic_upgrade(l2f2.url, _HEAD)
    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _HEAD
    assert _counts(l2f2.engine) == at_0015_counts == before_counts
    assert _authority_rows(l2f2.engine) == before_authorities, "a Phase-A authority row moved"


# --------------------------------------------------------------------------- #
# THE downgrade refusal
# --------------------------------------------------------------------------- #
def test_the_downgrade_refuses_while_a_phase_b_authority_exists(l2f2: Any) -> None:
    """Append-only lineage is not squeezed back into a Phase-A-only CHECK."""
    _insert_phase_b_authority(l2f2.engine, plan_hash=l2f2.plan.plan_hash)
    before = _authority_rows(l2f2.engine)
    assert len(before) == 2
    l2f2.engine.dispose()

    with pytest.raises(Exception, match="PHASE_B") as excinfo:
        alembic_downgrade(l2f2.url, _PRIOR)
    assert "append-only" in str(excinfo.value)

    l2f2.engine = _engine(l2f2.url)
    # refused BEFORE any schema mutation: the store is still a complete 0016.
    assert _revision(l2f2.engine) == _HEAD
    assert _canary_nullable(l2f2.engine) == "YES"
    assert _functions(l2f2.engine, _RESOLVE_B) != []
    assert _authority_rows(l2f2.engine) == before, "a Phase-B authority row was touched"


# --------------------------------------------------------------------------- #
# least privilege
# --------------------------------------------------------------------------- #
def test_only_the_runner_and_the_control_plane_may_execute_the_phase_b_resolver(
    l2f2: Any,
) -> None:
    signature = f"experiments.{_RESOLVE_B}(text, uuid, text)"
    with l2f2.engine.connect() as conn:
        allowed = {
            role: bool(
                conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": role, "f": signature},
                ).scalar_one()
            )
            for role in _ROLES
        }
        public = bool(
            conn.execute(
                text("SELECT has_function_privilege('public', :f, 'EXECUTE')"), {"f": signature}
            ).scalar_one()
        )
        table_privileges = {
            role: sorted(
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": _AUTHORITIES, "p": privilege},
                ).scalar_one()
            )
            for role in _ROLES
        }

    assert allowed == {
        "minos_admin": True,
        "minos_runner": True,
        "minos_evaluator": False,
        "minos_trainer": False,
        "minos_live": False,
    }
    assert public is False
    # 0016 grants no table privilege to anyone: the runner still cannot read the authority it is
    # checked against, and only the control plane may write one.
    assert table_privileges["minos_runner"] == []
    assert table_privileges["minos_evaluator"] == []
    assert table_privileges["minos_trainer"] == []
    assert table_privileges["minos_live"] == []
    assert table_privileges["minos_admin"] == ["INSERT", "SELECT"]
