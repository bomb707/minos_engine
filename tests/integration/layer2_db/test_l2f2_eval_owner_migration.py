"""Migration ``0018`` — the evaluator's definers stop executing as a SUPERUSER.

``0017`` did this for the runner; this is the same defect on the other side of the truth boundary.
``0008`` wrapped its whole upgrade in ``SET ROLE minos_admin``, so the execution ledgers and their
writers are control-plane-owned. ``0009`` and ``0010`` did not, so four ``SECURITY DEFINER``
functions the evaluator calls on every evaluation — and the two ledgers they append to — inherited
the migration principal.

Ownership is a privilege context rather than decoration, so structural equality is not enough:
these controls also run the truth registrar, the metrics registrar and both outcome writers under
a principal whose only MINOS membership is ``minos_evaluator``, and prove that principal still
holds no direct write anywhere.

No GATK and no MINOS_SUBNET: executions come from ``FakeGatkRunner`` and scores are recorded
upstream results, both through the existing private test seams.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_baseline"
_PRIOR = "0017_l2f2_owner_corrective"
_HEAD = "0018_l2f2_eval_owner_fix"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]
_EVAL_LOGIN = "minos_eval_owner_ci_svc"

_TRUTH_FN = "evaluation.l2f_register_train_truth_identity(uuid, char, char, char, char)"
_METRICS_FN = "evaluation.l2f_register_metrics_artifact(char, text, integer)"
_RESULT_FN = (
    "evaluation.l2f_record_evaluation_result(uuid, char, text, char, char, text, text, uuid, "
    "char, text, double precision, double precision, double precision, double precision, "
    "double precision, double precision, double precision, text, char)"
)
_FAILURE_FN = "evaluation.l2f_record_evaluation_failure(uuid, char, text, integer, char)"
_CORRECTED_FUNCTIONS = (_TRUTH_FN, _RESULT_FN, _FAILURE_FN, _METRICS_FN)

_RESULTS = "evaluation.l2f_evaluation_results"
_FAILURES = "evaluation.l2f_evaluation_failures"
_CORRECTED_TABLES = (_RESULTS, _FAILURES)

#: every SECURITY DEFINER function the accepted evaluator workflow reaches.
_EVALUATOR_FACING = _CORRECTED_FUNCTIONS
#: the 0017 guarantee, re-asserted so fixing one side cannot regress the other.
_RUNNER_FACING = (
    "experiments.minos_l2f_claim_next_job(text, text)",
    "experiments.minos_l2f_start_job(text, uuid, text)",
    "experiments.minos_l2f_release_job(text, uuid, text)",
    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
    "experiments.l2f2_resolve_claimed_phase_b_execution(text, uuid, text)",
    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
)

#: every role that is NOT the control plane. None of these may move by a single privilege.
_APPLICATION_ROLES = ("minos_evaluator", "minos_runner", "minos_trainer", "minos_live", "public")

_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
#: the relations 0018 re-owns, plus the one its registrar writes. No application role may write
#: any of them directly — every write goes through a SECURITY DEFINER function.
#:
#: ``evaluation.dataset_evaluation_identity`` is deliberately absent: ``minos_evaluator`` already
#: holds INSERT on it from the L2-E identity path, long before this migration. That grant is
#: pre-existing and untouched here, and the lifecycle control proves 0018 changes no grant at all.
_NO_DIRECT_WRITE = (
    _RESULTS,
    _FAILURES,
    "catalog.artifacts",
)


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _current_user(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT current_user")).scalar_one())


def _function_identity(engine: Any, signature: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT p.oid::text AS oid, p.proname, p.pronamespace::text AS namespace, "
                    "       p.proargtypes::text AS argtypes, p.prorettype::text AS rettype, "
                    "       p.prosecdef, p.proconfig::text AS config, "
                    "       pg_get_functiondef(p.oid) AS definition, "
                    "       pg_get_userbyid(p.proowner) AS owner, r.rolsuper AS owner_superuser "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            )
            .mappings()
            .one()
        )


def _table_identity(engine: Any, relation: str) -> dict[str, Any]:
    """Everything about a table that must not move — OID, shape, rules and contents."""
    schema, name = relation.split(".")
    with engine.connect() as conn:
        base = dict(
            conn.execute(
                text(
                    "SELECT c.oid::text AS oid, c.relkind::text AS relkind, "
                    "       pg_get_userbyid(c.relowner) AS owner, c.reltuples::bigint AS estimate "
                    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname = :s AND c.relname = :n"
                ),
                {"s": schema, "n": name},
            )
            .mappings()
            .one()
        )
        base["columns"] = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT column_name, ordinal_position, data_type, is_nullable, column_default "
                    "  FROM information_schema.columns "
                    " WHERE table_schema = :s AND table_name = :n ORDER BY ordinal_position"
                ),
                {"s": schema, "n": name},
            ).mappings()
        ]
        base["constraints"] = sorted(
            f"{r['conname']}::{r['definition']}"
            for r in conn.execute(
                text(
                    "SELECT con.conname, pg_get_constraintdef(con.oid) AS definition "
                    "  FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
                    "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname = :s AND c.relname = :n"
                ),
                {"s": schema, "n": name},
            ).mappings()
        )
        base["indexes"] = sorted(
            str(r[0])
            for r in conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :n"),
                {"s": schema, "n": name},
            )
        )
        base["triggers"] = sorted(
            f"{r['tgname']}->{r['proname']}"
            for r in conn.execute(
                text(
                    "SELECT t.tgname, p.proname FROM pg_trigger t "
                    "  JOIN pg_class c ON c.oid = t.tgrelid "
                    "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "  JOIN pg_proc p ON p.oid = t.tgfoid "
                    " WHERE n.nspname = :s AND c.relname = :n AND NOT t.tgisinternal"
                ),
                {"s": schema, "n": name},
            ).mappings()
        )
        base["row_count"] = int(
            conn.execute(text(f"SELECT count(*) FROM {relation}")).scalar_one()  # noqa: S608
        )
        base["row_digest"] = str(
            conn.execute(
                text(  # noqa: S608
                    f"SELECT COALESCE(md5(string_agg(t.id::text, ',' ORDER BY t.id)), '') "
                    f"  FROM {relation} t"
                )
            ).scalar_one()
        )
    return base


def _effective_grants(engine: Any) -> dict[str, dict[str, list[str]]]:
    """What every APPLICATION role and PUBLIC may ACTUALLY do, however the ACL is written.

    ``minos_admin`` is deliberately excluded: it necessarily gains owner authority over the two
    ledgers, and that is the point of the migration rather than a side effect. It is asserted
    separately, and no application role may move at all.
    """
    out: dict[str, dict[str, list[str]]] = {}
    with engine.connect() as conn:
        for role in _APPLICATION_ROLES:
            functions = [
                signature
                for signature in (*_EVALUATOR_FACING, *_RUNNER_FACING)
                if conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": role, "f": signature},
                ).scalar_one()
            ]
            tables = [
                f"{table}:{privilege}"
                for table in _NO_DIRECT_WRITE
                for privilege in ("SELECT", *_WRITE_PRIVILEGES)
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                ).scalar_one()
            ]
            out[role] = {"execute": sorted(functions), "tables": sorted(tables)}
    return out


# --------------------------------------------------------------------------- #
# the ownership move itself
# --------------------------------------------------------------------------- #
def test_lifecycle_0017_0018_0017_0018_moves_ownership_and_nothing_else(
    isolated_pg_base_url: str,
) -> None:
    """The downgrade returns everything to the principal that created it in 0009/0010.

    That assumption is asserted rather than assumed: at 0017 all six objects are owned by the
    principal running these migrations, and it is that exact name the downgrade restores.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            principal = _current_user(engine)
            functions_0017 = {
                signature: _function_identity(engine, signature)
                for signature in _CORRECTED_FUNCTIONS
            }
            tables_0017 = {table: _table_identity(engine, table) for table in _CORRECTED_TABLES}
            grants_0017 = _effective_grants(engine)
            for signature, identity in functions_0017.items():
                assert identity["owner"] == principal, signature
                assert identity["owner_superuser"] is True, (
                    f"{signature} is the defect this migration exists for"
                )
                assert identity["prosecdef"] is True
            for table, identity in tables_0017.items():
                assert identity["owner"] == principal, table
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            for signature, before in functions_0017.items():
                after = _function_identity(engine, signature)
                assert after["owner"] == "minos_admin", signature
                assert after["owner_superuser"] is False, signature
                ignored = ("owner", "owner_superuser")
                assert {k: v for k, v in after.items() if k not in ignored} == {
                    k: v for k, v in before.items() if k not in ignored
                }, f"{signature} moved by more than its owner"
            for table, before in tables_0017.items():
                after = _table_identity(engine, table)
                assert after["owner"] == "minos_admin", table
                assert {k: v for k, v in after.items() if k != "owner"} == {
                    k: v for k, v in before.items() if k != "owner"
                }, f"{table} moved by more than its owner"
            # no APPLICATION role moves by a single privilege ...
            assert _effective_grants(engine) == grants_0017
            # ... and the control plane gains exactly what the writers need: authority over the
            # ledgers they append to, by owning them, exactly as 0008's execution writers do.
            with engine.connect() as conn:
                admin = {
                    f"{table}:{privilege}": bool(
                        conn.execute(
                            text("SELECT has_table_privilege('minos_admin', :t, :p)"),
                            {"t": table, "p": privilege},
                        ).scalar_one()
                    )
                    for table in _CORRECTED_TABLES
                    for privilege in ("SELECT", "INSERT")
                }
            assert all(admin.values()), admin
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            for signature, before in functions_0017.items():
                assert _function_identity(engine, signature) == before
            for table, before in tables_0017.items():
                assert _table_identity(engine, table) == before
            assert _effective_grants(engine) == grants_0017
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            for signature in _CORRECTED_FUNCTIONS:
                assert _function_identity(engine, signature)["owner"] == "minos_admin"
            for table in _CORRECTED_TABLES:
                assert _table_identity(engine, table)["owner"] == "minos_admin"
            assert _effective_grants(engine) == grants_0017
        finally:
            engine.dispose()


def test_0018_touches_exactly_six_objects(isolated_pg_base_url: str) -> None:
    """Four functions and two tables. No role, membership, trigger, index or schema ACL moves."""
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
        "constraints",
        "indexes",
        "triggers",
        "roles",
        "role_memberships",
        "schema_security",
        "default_acls",
    ):
        assert json.dumps(before.get(section), sort_keys=True, default=str) == json.dumps(
            after.get(section), sort_keys=True, default=str
        ), f"0018 altered {section!r}"

    moved_relations = sorted(
        name
        for name in set(before["relations"]) | set(after["relations"])
        if json.dumps(before["relations"].get(name), sort_keys=True, default=str)
        != json.dumps(after["relations"].get(name), sort_keys=True, default=str)
    )
    assert moved_relations == sorted(_CORRECTED_TABLES), moved_relations

    def _by_name(state: Any) -> dict[str, Any]:
        return {f"{r['schema']}.{r['name']}": r for r in state["functions"]}

    moved_functions = sorted(
        key
        for key in set(_by_name(before)) | set(_by_name(after))
        if json.dumps(_by_name(before).get(key), sort_keys=True, default=str)
        != json.dumps(_by_name(after).get(key), sort_keys=True, default=str)
    )
    assert moved_functions == [
        "evaluation.l2f_record_evaluation_failure",
        "evaluation.l2f_record_evaluation_result",
        "evaluation.l2f_register_metrics_artifact",
        "evaluation.l2f_register_train_truth_identity",
    ], moved_functions
    for key in moved_functions:
        differing = sorted(
            field
            for field in _by_name(before)[key]
            if _by_name(before)[key][field] != _by_name(after)[key][field]
        )
        # the ACL's GRANTOR is rewritten by ALTER ... OWNER TO; PostgreSQL cannot do otherwise.
        assert differing == ["acl_effective", "acl_raw", "owner"], f"{key} moved by {differing}"
        assert _by_name(after)[key]["owner"] == "minos_admin"


def test_the_control_plane_role_is_unchanged_and_is_not_a_superuser(l2f2: Any) -> None:
    with l2f2.engine.connect() as conn:
        row = dict(
            conn.execute(
                text(
                    "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolbypassrls, "
                    "       rolreplication FROM pg_roles WHERE rolname = 'minos_admin'"
                )
            )
            .mappings()
            .one()
        )
    assert row == dict.fromkeys(row, False)


# --------------------------------------------------------------------------- #
# the hard regressions, both sides
# --------------------------------------------------------------------------- #
def test_no_evaluator_facing_definer_executes_as_a_superuser(l2f2: Any) -> None:
    """THE regression, asserted through pg_roles.rolsuper rather than by owner name."""
    with l2f2.engine.connect() as conn:
        rows = {
            signature: conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, r.rolsuper, p.prosecdef, "
                    "       p.proconfig::text AS config "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            )
            .mappings()
            .one_or_none()
            for signature in _EVALUATOR_FACING
        }
    assert sorted(s for s, row in rows.items() if row is None) == []
    assert sorted(s for s, row in rows.items() if row["rolsuper"]) == []
    assert all(row["prosecdef"] for row in rows.values())
    assert all("search_path" in str(row["config"]) for row in rows.values())
    assert {row["owner"] for row in rows.values()} == {"minos_admin"}


def test_the_runner_side_guarantee_from_0017_is_not_regressed(l2f2: Any) -> None:
    with l2f2.engine.connect() as conn:
        superuser_owned = sorted(
            signature
            for signature in _RUNNER_FACING
            if conn.execute(
                text(
                    "SELECT r.rolsuper FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            ).scalar_one()
        )
    assert superuser_owned == []


def test_no_application_role_gained_a_direct_write(l2f2: Any) -> None:
    """The objective was never to give the evaluator more table access. It gets none."""
    with l2f2.engine.connect() as conn:
        writes = {
            role: sorted(
                f"{table}:{privilege}"
                for table in _NO_DIRECT_WRITE
                for privilege in _WRITE_PRIVILEGES
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                ).scalar_one()
            )
            for role in _APPLICATION_ROLES
        }
        reads = sorted(
            table
            for table in (_RESULTS, _FAILURES)
            if conn.execute(
                text("SELECT has_table_privilege('minos_evaluator', :t, 'SELECT')"), {"t": table}
            ).scalar_one()
        )
    for role, granted in writes.items():
        assert granted == [], f"{role} holds a direct write: {granted}"
    assert reads == sorted([_FAILURES, _RESULTS]), "the evaluator's accepted SELECT was lost"


# --------------------------------------------------------------------------- #
# ownership is a privilege context, so it is proven at RUNTIME
# --------------------------------------------------------------------------- #
@pytest.fixture
def evaluator(l2f2: Any) -> Any:
    """A LOGIN principal whose ONLY MINOS membership is ``minos_evaluator``."""
    from sqlalchemy.engine import make_url

    parsed = make_url(l2f2.url)
    with l2f2.engine.connect() as conn, conn.begin():
        conn.execute(text(f"DROP ROLE IF EXISTS {_EVAL_LOGIN}"))
        conn.execute(
            text(
                f"CREATE ROLE {_EVAL_LOGIN} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOBYPASSRLS INHERIT"
            )
        )
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_EVAL_LOGIN}'))
        conn.execute(text(f"GRANT minos_evaluator TO {_EVAL_LOGIN}"))
    engine = create_engine(parsed.set(username=_EVAL_LOGIN, password=""))
    try:
        yield engine
    finally:
        engine.dispose()
        with l2f2.engine.connect() as conn, conn.begin():
            conn.execute(text(f'REVOKE ALL ON DATABASE "{parsed.database}" FROM {_EVAL_LOGIN}'))
            conn.execute(text(f"REVOKE minos_evaluator FROM {_EVAL_LOGIN}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {_EVAL_LOGIN}"))


def test_the_evaluator_principal_holds_exactly_one_minos_membership(evaluator: Any) -> None:
    with evaluator.connect() as conn:
        memberships = sorted(
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT g.rolname FROM pg_auth_members m "
                    "  JOIN pg_roles g ON g.oid = m.roleid "
                    "  JOIN pg_roles u ON u.oid = m.member "
                    " WHERE u.rolname = current_user AND g.rolname LIKE 'minos%'"
                )
            )
        )
        assert memberships == ["minos_evaluator"]
        assert conn.execute(text("SELECT current_setting('is_superuser')")).scalar_one() == "off"


def test_the_re_owned_metrics_registrar_still_registers_and_still_refuses(
    evaluator: Any, l2f2: Any
) -> None:
    digest = hashlib.sha256(b"eval-owner-metrics").hexdigest()
    with evaluator.connect() as conn, conn.begin():
        first = (
            conn.execute(
                text(
                    "SELECT artifact_id, created FROM "
                    "evaluation.l2f_register_metrics_artifact(:s, :u, :z)"
                ),
                {"s": digest, "u": "file:///metrics.json", "z": 17},
            )
            .mappings()
            .one()
        )
    assert first["created"] is True
    with evaluator.connect() as conn, conn.begin():
        replay = (
            conn.execute(
                text(
                    "SELECT artifact_id, created FROM "
                    "evaluation.l2f_register_metrics_artifact(:s, :u, :z)"
                ),
                {"s": digest, "u": "file:///metrics.json", "z": 17},
            )
            .mappings()
            .one()
        )
    assert replay["created"] is False and replay["artifact_id"] == first["artifact_id"]

    for params, message in (
        ({"s": "NOTHEX" + "a" * 58, "u": "file:///x", "z": 1}, "hex"),
        ({"s": "b" * 64, "u": "   ", "z": 1}, "empty"),
        ({"s": "c" * 64, "u": "file:///x", "z": -1}, "negative"),
        ({"s": digest, "u": "file:///moved.json", "z": 17}, "different"),
    ):
        with (
            pytest.raises(Exception, match=message),
            evaluator.connect() as conn,
            conn.begin(),
        ):
            conn.execute(
                text("SELECT * FROM evaluation.l2f_register_metrics_artifact(:s, :u, :z)"), params
            )

    # the evaluator reached catalog.artifacts through the definer and by no other route.
    with (
        pytest.raises(Exception, match="permission denied"),
        evaluator.connect() as conn,
        conn.begin(),
    ):
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, media_type) "
                "VALUES ('file:///direct', repeat('d', 64), 'application/json')"
            )
        )


def test_the_re_owned_truth_registrar_still_registers_train_identities(
    evaluator: Any, l2f2: Any
) -> None:
    """TRAIN-only by construction: the caller supplies no partition and cannot reach another."""
    # the TRAIN truth projection reads catalog.split_allocations, which this fixture's upstream
    # seed does not populate. Every plan member here is a TRAIN member by construction.
    with l2f2.engine.connect() as conn, conn.begin():
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
    with l2f2.engine.connect() as conn:
        targets = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT dataset_registry_id, round_id FROM "
                    "evaluation.l2f_train_truth_registration_targets ORDER BY round_id"
                )
            ).mappings()
        ]
    assert targets, "the fixture registers at least one TRAIN target"
    target = targets[0]
    digests = {key: hashlib.sha256(key.encode()).hexdigest() for key in ("v", "t", "m", "i")}

    def _register(engine: Any, registry_id: Any) -> Any:
        with engine.connect() as conn, conn.begin():
            return (
                conn.execute(
                    text(
                        "SELECT identity_id, created FROM "
                        "evaluation.l2f_register_train_truth_identity(:d, :v, :t, :m, :i)"
                    ),
                    {"d": registry_id, **digests},
                )
                .mappings()
                .one()
            )

    first = _register(evaluator, target["dataset_registry_id"])
    assert first["created"] is True
    replay = _register(evaluator, target["dataset_registry_id"])
    assert replay["created"] is False and replay["identity_id"] == first["identity_id"]

    # a dataset that is not a TRAIN registration target is unreachable through this interface.
    with pytest.raises(Exception, match="not in the accepted split"):
        _register(evaluator, uuid.uuid4())

    # the identity it just wrote is one it cannot reach any other way: the re-owned ledgers stay
    # closed to it entirely. (Its INSERT on dataset_evaluation_identity predates this migration
    # and belongs to the L2-E identity path; 0018 neither grants nor removes it.)
    for ledger in (_RESULTS, _FAILURES):
        with (
            pytest.raises(Exception, match="permission denied"),
            evaluator.connect() as conn,
            conn.begin(),
        ):
            conn.execute(text(f"DELETE FROM {ledger}"))  # noqa: S608


def test_the_re_owned_writers_persist_a_real_evaluation_under_evaluator_only(
    isolated_pg_base_url: str, tmp_path: Any
) -> None:
    """The whole L2-F2 evaluator chain, on a 0018 store, with no administrative connection.

    Truth registration, metrics-artifact registration and the success writer are all production
    boundaries and all three land on functions this migration re-owned. The only stand-in is the
    recorded upstream score, at the accepted test seam.
    """
    from minos_engine.baseline.phase_a_observations import load_phase_a_observations
    from tests.integration.layer2_db.l2f2_phase_a_env import REQUIRED_REVISION, phase_a_store

    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        # the store is built at whatever revision the RUNNER currently requires; 0018's own
        # lifecycle pin above is about this migration, not about where the campaign store lives.
        assert _revision(env.engine) == REQUIRED_REVISION
        dispatched = env.run(worker_id="ci-eval-owner")
        assert dispatched is not None and dispatched.status == "SUCCEEDED"

        env.register_truth()
        evaluator = env.evaluator_engine()
        with evaluator.connect() as conn:
            identities = int(
                conn.execute(
                    text("SELECT count(*) FROM evaluation.dataset_evaluation_identity")
                ).scalar_one()
            )
        assert identities >= 1, "the re-owned truth registrar wrote through production code"

        env.evaluate(dispatched, minos_score=0.8625, as_evaluator=True)

        snapshot = load_phase_a_observations(env.engine)
        assert len(snapshot.observations) == 1
        assert snapshot.observations[0].admitted is True
        assert snapshot.evaluation_result_count == 1
        assert snapshot.evaluation_failure_count == 0

        # the XOR the ledger has always enforced still holds, from the evaluator's own principal.
        with pytest.raises(Exception, match="already|outcome|exclusive"):
            env.fail_evaluation(dispatched, failure_code="HAPPY_TIMEOUT", as_evaluator=True)

        # and it still cannot touch either ledger directly.
        for ledger in (_RESULTS, _FAILURES):
            with (
                pytest.raises(Exception, match="permission denied"),
                evaluator.connect() as conn,
                conn.begin(),
            ):
                conn.execute(text(f"DELETE FROM {ledger}"))  # noqa: S608


def test_the_re_owned_failure_writer_persists_a_bounded_failure_under_evaluator_only(
    isolated_pg_base_url: str, tmp_path: Any
) -> None:
    """A bounded evaluation failure is a decided outcome too, and it goes through the same door."""
    from minos_engine.baseline.phase_a_observations import load_phase_a_observations
    from tests.integration.layer2_db.l2f2_phase_a_env import phase_a_store

    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        dispatched = env.run(worker_id="ci-eval-owner-fail")
        assert dispatched is not None and dispatched.status == "SUCCEEDED"
        env.register_truth()

        env.fail_evaluation(dispatched, failure_code="HAPPY_TIMEOUT", as_evaluator=True)

        snapshot = load_phase_a_observations(env.engine)
        assert snapshot.evaluation_failure_count == 1
        assert snapshot.evaluation_result_count == 0
        assert snapshot.infrastructure_incident_count == 1, "a hap.py timeout is OUR failure"

        # the other half of the XOR, and the append-only rule, both from the evaluator principal.
        with pytest.raises(Exception, match="already|outcome|exclusive"):
            env.evaluate(dispatched, minos_score=0.5, as_evaluator=True)


# --------------------------------------------------------------------------- #
# the populated store — the shape the real baseline is in
# --------------------------------------------------------------------------- #
def test_a_populated_store_migrates_and_no_scientific_row_moves(
    isolated_pg_base_url: str, tmp_path: Any
) -> None:
    """0018 must not refuse a store holding execution AND evaluation evidence."""
    from tests.integration.layer2_db.l2f2_phase_a_env import phase_a_store

    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        dispatched = env.run(worker_id="ci-eval-owner-populated")
        assert dispatched is not None
        env.register_truth()
        env.evaluate(dispatched, minos_score=0.8625, as_evaluator=True)

        tables = (
            "experiments.l2f_experiment_plans",
            "experiments.l2f_experiment_jobs",
            "experiments.l2f_execution_results",
            "experiments.l2f_execution_failures",
            "experiments.l2f2_execution_authorities",
            "evaluation.dataset_evaluation_identity",
            _RESULTS,
            _FAILURES,
        )

        def counts(engine: Any) -> dict[str, int]:
            with engine.connect() as conn:
                return {
                    table: int(
                        conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
                    )
                    for table in tables
                }

        def evaluation_rows(engine: Any) -> list[dict[str, Any]]:
            with engine.connect() as conn:
                return [
                    dict(r)
                    for r in conn.execute(
                        text(  # noqa: S608
                            f"SELECT * FROM {_RESULTS} ORDER BY id"
                        )
                    ).mappings()
                ]

        before_counts = counts(env.engine)
        before_rows = evaluation_rows(env.engine)
        before_tables = {table: _table_identity(env.engine, table) for table in _CORRECTED_TABLES}
        assert before_counts[_RESULTS] == 1
        assert before_counts["experiments.l2f_execution_results"] == 1
        url = env.url
        env.engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert counts(engine) == before_counts
            at_0017 = {table: _table_identity(engine, table) for table in _CORRECTED_TABLES}
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        env.engine = _engine(url)
        assert _revision(env.engine) == _HEAD
        assert counts(env.engine) == before_counts
        assert evaluation_rows(env.engine) == before_rows, "an evaluation row moved"
        for table in _CORRECTED_TABLES:
            after = _table_identity(env.engine, table)
            assert after == before_tables[table]
            assert {k: v for k, v in after.items() if k != "owner"} == {
                k: v for k, v in at_0017[table].items() if k != "owner"
            }
            assert after["owner"] == "minos_admin"
