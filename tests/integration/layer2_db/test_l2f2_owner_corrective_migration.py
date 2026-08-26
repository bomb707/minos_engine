"""Migration ``0017`` — the legacy runner definers stop executing as a SUPERUSER.

A ``SECURITY DEFINER`` function runs with its OWNER's authority, so who owns one IS its privilege
boundary. ``0011`` created the Phase-A resolver and the artifact registrar as whatever principal
ran the migration — a superuser — while ``0008`` and ``0016`` created their equivalents under
``minos_admin``. The runner's grants were never wrong; the definer was.

The whole change is ownership metadata. These controls exist to prove that literally: same OIDs,
same bodies, same ``SECURITY DEFINER`` flag, same ``search_path``, same ACLs, same authority
table, same rows — and, because ownership is not decoration, that both functions still work
afterwards for a principal whose only MINOS membership is ``minos_runner``.

No GATK and no scoring: outcomes come from ``FakeGatkRunner`` through the private test seam.
"""

from __future__ import annotations

import hashlib
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
_PRIOR = "0016_l2f2_phase_b_execution"
_HEAD = "0017_l2f2_owner_corrective"

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

#: exactly what 0017 is authorized to re-own.
_CORRECTED = (
    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
)

#: every SECURITY DEFINER function the accepted runner entries reach, Phase A and Phase B alike.
_RUNNER_FACING = (
    "experiments.minos_l2f_claim_next_job(text, text)",
    "experiments.minos_l2f_start_job(text, uuid, text)",
    "experiments.minos_l2f_release_job(text, uuid, text)",
    "experiments.minos_l2f_resolve_running_job(text, uuid, text)",
    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
    "experiments.l2f2_resolve_claimed_phase_b_execution(text, uuid, text)",
    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
    (
        "experiments.minos_l2f_complete_job_success(text, uuid, text, text, text, text, text, "
        "text, text, text, uuid, text, uuid, text, text, bigint, text)"
    ),
    "experiments.minos_l2f_fail_job(text, uuid, text, text, integer, text, bigint, text)",
)


def _minos_grantees(function_row: Any) -> dict[str, list[str]]:
    """Which MINOS roles (and PUBLIC) actually hold which privileges on a function."""
    interesting = {*_ROLES, "PUBLIC"}
    out: dict[str, list[str]] = {}
    for entry in function_row["acl_effective"]:
        grantee = str(entry["grantee"])
        if grantee in interesting:
            out.setdefault(grantee, []).append(str(entry["privilege"]))
    return {role: sorted(privileges) for role, privileges in sorted(out.items())}


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _identity(engine: Any, signature: str) -> dict[str, Any]:
    """Everything about a function that must NOT move, plus the one thing that must."""
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT p.oid::text AS oid, p.proname, p.pronamespace::text AS namespace, "
                    "       p.proargtypes::text AS argtypes, p.prorettype::text AS rettype, "
                    "       p.prosecdef, p.proconfig::text AS config, "
                    "       COALESCE(p.proacl::text, '') AS acl, "
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


def _may_execute(engine: Any, role: str, signature: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                {"r": role, "f": signature},
            ).scalar_one()
        )


def _current_user(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT current_user")).scalar_one())


def _relation_owner(engine: Any, relation: str) -> str:
    schema, name = relation.split(".")
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
                    "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname = :s AND c.relname = :n"
                ),
                {"s": schema, "n": name},
            ).scalar_one()
        )


def _counts(engine: Any) -> dict[str, int]:
    tables = (
        "experiments.l2f_experiment_plans",
        "experiments.l2f_experiment_jobs",
        "experiments.l2f_execution_results",
        "experiments.l2f_execution_failures",
        "evaluation.l2f_evaluation_results",
        _AUTHORITIES,
    )
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            for table in tables
        }


def _authority_rows(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM {_AUTHORITIES} ORDER BY created_at, id")  # noqa: S608
            ).mappings()
        ]


# --------------------------------------------------------------------------- #
# the ownership move itself
# --------------------------------------------------------------------------- #
def test_lifecycle_0016_0017_0016_0017_moves_ownership_and_nothing_else(
    isolated_pg_base_url: str,
) -> None:
    """The downgrade returns the functions to the principal that created them in 0011.

    That assumption is asserted rather than assumed: at 0016 both functions are owned by the
    principal running these migrations, and it is that exact name the downgrade restores.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            principal = _current_user(engine)
            at_0016 = {signature: _identity(engine, signature) for signature in _CORRECTED}
            for signature, identity in at_0016.items():
                assert identity["owner"] == principal, signature
                assert identity["owner_superuser"] is True, (
                    f"{signature} is the defect this migration exists for"
                )
                assert identity["prosecdef"] is True
            structural_0016 = _state(engine)
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            at_0017 = {signature: _identity(engine, signature) for signature in _CORRECTED}
            for signature, identity in at_0017.items():
                before = at_0016[signature]
                assert identity["owner"] == "minos_admin", signature
                assert identity["owner_superuser"] is False, signature
                # OID, signature, body, SECURITY DEFINER and search_path are all untouched.
                ignored = ("owner", "owner_superuser", "acl")
                assert {k: v for k, v in identity.items() if k not in ignored} == {
                    k: v for k, v in before.items() if k not in ignored
                }, f"{signature} moved by more than its owner"
                # PostgreSQL rewrites the ACL's GRANTOR when ownership moves — it cannot do
                # otherwise, since privileges are granted BY the owner. What that costs is
                # exactly what this migration is for: the superuser stops being the grantor of
                # this function's privileges, and its implicit owner entry disappears.
                assert principal not in identity["acl"]
                assert "minos_runner=X/minos_admin" in identity["acl"]
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _state(engine) == structural_0016, "downgrade did not restore 0016"
            for signature in _CORRECTED:
                assert _identity(engine, signature)["owner"] == principal
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            again = {signature: _identity(engine, signature) for signature in _CORRECTED}
            assert json.dumps(again, sort_keys=True) == json.dumps(at_0017, sort_keys=True)
        finally:
            engine.dispose()


def test_0017_changes_no_relation_no_role_and_no_effective_grant(
    isolated_pg_base_url: str,
) -> None:
    """Ownership metadata on two functions, and the grantor PostgreSQL rewrites along with it.

    No relation, constraint, index, trigger, role, membership, schema ACL or default ACL moves,
    and no MINOS role gains or loses EXECUTE on anything.
    """
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
        "relations",
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
        ), f"0017 altered {section!r}"

    def _by_name(state: Any) -> dict[str, Any]:
        return {f"{r['name']}({r['identity_arguments']})": r for r in state["functions"]}

    moved = sorted(
        key
        for key in set(_by_name(before)) | set(_by_name(after))
        if json.dumps(_by_name(before).get(key), sort_keys=True, default=str)
        != json.dumps(_by_name(after).get(key), sort_keys=True, default=str)
    )
    assert moved == [
        "l2f2_register_execution_artifact(p_kind text, p_sha256 character, p_uri text, "
        "p_size_bytes integer)",
        "l2f2_resolve_claimed_execution(p_plan_hash text, p_job_id uuid, p_worker_id text)",
    ], moved
    for key in moved:
        before_row, after_row = _by_name(before)[key], _by_name(after)[key]
        differing = sorted(k for k in before_row if before_row[k] != after_row[k])
        assert differing == ["acl_effective", "acl_raw", "owner"], f"{key} moved by {differing}"
        assert (before_row["owner"], after_row["owner"])[1] == "minos_admin"
        # the grantOR moves; the grantEES that matter do not.
        assert _minos_grantees(before_row) == _minos_grantees(after_row)


def test_the_append_only_authority_table_keeps_its_owner(isolated_pg_base_url: str) -> None:
    """A deliberate NON-change, recorded as a control rather than left to inference.

    Owning the table would implicitly give the control plane UPDATE, DELETE, TRUNCATE, ALTER and
    DROP over append-only scientific lineage — exactly what 0011 withheld — and nothing needs it:
    a table has no definer semantics, and the re-owned resolver reads it through the SELECT grant
    0011 already gave minos_admin.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            before = _relation_owner(engine, _AUTHORITIES)
        finally:
            engine.dispose()
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _relation_owner(engine, _AUTHORITIES) == before
            with engine.connect() as conn:
                grants = sorted(
                    privilege
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                    if conn.execute(
                        text("SELECT has_table_privilege('minos_admin', :t, :p)"),
                        {"t": _AUTHORITIES, "p": privilege},
                    ).scalar_one()
                )
            assert grants == ["INSERT", "SELECT"], "the control plane's authority-table grant grew"
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# the role the definer authority now is
# --------------------------------------------------------------------------- #
def test_the_control_plane_role_is_unchanged_and_is_not_a_superuser(l2f2: Any) -> None:
    """Re-owning to a role that could log in, or that was a superuser, would fix nothing."""
    with l2f2.engine.connect() as conn:
        row = dict(
            conn.execute(
                text(
                    "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolbypassrls, "
                    "       rolreplication "
                    "  FROM pg_roles WHERE rolname = 'minos_admin'"
                )
            )
            .mappings()
            .one()
        )
    assert row == {
        "rolsuper": False,
        "rolcanlogin": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }


def test_no_runner_facing_definer_executes_as_a_superuser(l2f2: Any) -> None:
    """THE regression. Asserted through pg_roles.rolsuper, never by owner name."""
    with l2f2.engine.connect() as conn:
        rows = {
            signature: conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, r.rolsuper, p.prosecdef "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            )
            .mappings()
            .one_or_none()
            for signature in _RUNNER_FACING
        }

    missing = sorted(signature for signature, row in rows.items() if row is None)
    assert missing == [], f"a runner-facing function does not exist: {missing}"
    superuser_owned = sorted(signature for signature, row in rows.items() if row["rolsuper"])
    assert superuser_owned == [], f"still executing with SUPERUSER authority: {superuser_owned}"
    assert all(row["prosecdef"] for row in rows.values())
    assert {row["owner"] for row in rows.values()} == {"minos_admin"}


def test_the_runner_still_holds_exactly_its_two_kinds_of_privilege(l2f2: Any) -> None:
    """Re-owning a definer must not have been compensated for with a direct grant."""
    with l2f2.engine.connect() as conn:
        execute = {
            role: sorted(
                signature
                for signature in _RUNNER_FACING
                if conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": role, "f": signature},
                ).scalar_one()
            )
            for role in (*_ROLES, "public")
        }
        tables = {
            role: sorted(
                f"{table}:{privilege}"
                for table in (_AUTHORITIES, "catalog.artifacts", "experiments.l2f_experiment_jobs")
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                ).scalar_one()
            )
            for role in ("minos_runner", "minos_evaluator", "minos_trainer", "minos_live")
        }

    assert execute["minos_runner"] == sorted(_RUNNER_FACING)
    assert execute["minos_admin"] == sorted(_RUNNER_FACING)
    for role in ("minos_evaluator", "minos_trainer", "minos_live", "public"):
        assert execute[role] == [], f"{role} may execute a runner-facing definer"
    # No application role writes any of these tables directly, and none of them may touch the
    # append-only authority table at all — it is what the runner is CHECKED AGAINST, never what it
    # reads. (A pre-existing historical `catalog.artifacts` SELECT is left exactly as it was;
    # test_0017_changes_no_relation_no_role_and_no_effective_grant proves no grant moved.)
    for role, granted in tables.items():
        assert not [e for e in granted if e.startswith(f"{_AUTHORITIES}:")], (
            f"{role} holds privilege on the execution-authority table: {granted}"
        )
        assert not [e for e in granted if not e.endswith(":SELECT")], (
            f"{role} holds direct write privilege: {granted}"
        )
    assert "catalog.artifacts:INSERT" not in tables["minos_runner"]


# --------------------------------------------------------------------------- #
# ownership is a privilege context, so it is proven at RUNTIME
# --------------------------------------------------------------------------- #
def test_the_re_owned_resolver_still_resolves_for_a_runner_only_principal(
    service: Any, l2f2: Any
) -> None:
    """minos_admin must hold everything the 0011 body reads — this is not assumable."""
    plan_hash = l2f2.plan.plan_hash
    with service.connect() as conn, conn.begin():
        claimed = (
            conn.execute(
                text("SELECT job_id, job_key FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": plan_hash, "w": "w-owner-corrective"},
            )
            .mappings()
            .one()
        )
    with service.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM experiments.l2f2_resolve_claimed_execution(:h, :j, :w)"),
                {"h": plan_hash, "j": claimed["job_id"], "w": "w-owner-corrective"},
            )
            .mappings()
            .one()
        )
    assert str(row["job_key"]) == str(claimed["job_key"])
    assert row["partition"] == "train"
    # it still reaches every schema the body joins, none of which the runner may read directly.
    assert row["dataset_id"] and row["bam_sha256"] and row["profile_id"] and row["config_uri"]


def test_the_re_owned_registrar_still_registers_and_still_refuses(service: Any, l2f2: Any) -> None:
    """The registrar inserts into catalog.artifacts as its owner. Both kinds, then the negatives."""
    for kind, media, provenance in (
        ("vcf", "application/vnd.ga4gh.vcf", "l2f:gatk-vcf"),
        (
            "result_manifest",
            "application/vnd.minos.l2f-execution-result+json",
            "l2f:execution-result-json",
        ),
    ):
        digest = hashlib.sha256(f"owner-corrective-{kind}".encode()).hexdigest()
        with service.connect() as conn, conn.begin():
            first = (
                conn.execute(
                    text(
                        "SELECT artifact_id, created FROM "
                        "experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"
                    ),
                    {"k": kind, "s": digest, "u": f"file:///{kind}.out", "z": 11},
                )
                .mappings()
                .one()
            )
        assert first["created"] is True
        with service.connect() as conn, conn.begin():
            replay = (
                conn.execute(
                    text(
                        "SELECT artifact_id, created FROM "
                        "experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"
                    ),
                    {"k": kind, "s": digest, "u": f"file:///{kind}.out", "z": 11},
                )
                .mappings()
                .one()
            )
        assert replay["created"] is False and replay["artifact_id"] == first["artifact_id"]
        with l2f2.engine.connect() as conn:
            stored = (
                conn.execute(
                    text("SELECT media_type, provenance FROM catalog.artifacts WHERE sha256 = :s"),
                    {"s": digest},
                )
                .mappings()
                .one()
            )
        assert (stored["media_type"], stored["provenance"]) == (media, provenance)

    good = hashlib.sha256(b"owner-corrective-vcf").hexdigest()
    for params, message in (
        ({"k": "metrics", "s": "a" * 64, "u": "file:///x", "z": 1}, "unsupported"),
        ({"k": "vcf", "s": "NOTHEX" + "a" * 58, "u": "file:///x", "z": 1}, "canonical lowercase"),
        ({"k": "vcf", "s": "b" * 64, "u": "   ", "z": 1}, "non-empty"),
        ({"k": "vcf", "s": "c" * 64, "u": "file:///x", "z": -1}, "non-negative"),
        ({"k": "vcf", "s": good, "u": "file:///moved.vcf", "z": 11}, "different metadata"),
    ):
        with (
            pytest.raises(Exception, match=message),
            service.connect() as conn,
            conn.begin(),
        ):
            conn.execute(
                text("SELECT * FROM experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"),
                params,
            )


def test_a_whole_phase_a_execution_still_runs_after_the_ownership_move(
    service: Any, l2f2: Any
) -> None:
    """Claim, legacy resolve, byte preparation, artifact registration, RUNNING, durable success."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-owner-corrective",
        runner=FakeGatkRunner(),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )
    assert dispatched is not None
    assert dispatched.status == "SUCCEEDED"
    assert dispatched.execution_result_id is not None
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_results") == 1
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_failures") == 0


# --------------------------------------------------------------------------- #
# the populated store — the shape the real baseline is in
# --------------------------------------------------------------------------- #
def test_a_populated_store_migrates_and_no_scientific_row_moves(service: Any, l2f2: Any) -> None:
    """0017 must not refuse a store holding execution evidence: the real baseline is one."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    for worker, runner in (
        ("ci-owner-success", FakeGatkRunner()),
        ("ci-owner-failure", FakeGatkRunner(exit_code=127)),
    ):
        assert (
            _execute_l2f2_job(
                service,
                l2f2.authority,
                worker_id=worker,
                runner=runner,
                dataset_root=l2f2.dataset_root,
                publisher=l2f2.publisher,
                work_root=l2f2.work_root,
                execution_environment=TEST_EXECUTION_ENVIRONMENT,
            )
            is not None
        )

    before_counts = _counts(l2f2.engine)
    before_authorities = _authority_rows(l2f2.engine)
    assert before_counts["experiments.l2f_execution_results"] == 1
    assert before_counts["experiments.l2f_execution_failures"] == 1
    l2f2.engine.dispose()

    alembic_downgrade(l2f2.url, _PRIOR)
    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _PRIOR
    at_0016 = _counts(l2f2.engine)
    l2f2.engine.dispose()

    alembic_upgrade(l2f2.url, _HEAD)
    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _HEAD
    assert _counts(l2f2.engine) == at_0016 == before_counts
    assert _authority_rows(l2f2.engine) == before_authorities
    for signature in _CORRECTED:
        assert _identity(l2f2.engine, signature)["owner"] == "minos_admin"


def test_the_round_trip_does_not_strip_the_control_planes_execute_grant(
    isolated_pg_base_url: str,
) -> None:
    """A subtlety that silently breaks the control plane if the downgrade ignores it.

    While ``minos_admin`` owns a function, its explicit ``EXECUTE`` grant is absorbed into the
    implicit owner entry. Handing ownership back therefore drops that entry with it, and
    ``minos_admin`` — correctly NOT a superuser — would quietly lose the ability to execute the
    very functions ``0011`` granted it. The downgrade re-issues both grants, so the ACL comes back
    exactly as ``0011`` left it.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            principal = _current_user(engine)
            at_0016 = {signature: _identity(engine, signature)["acl"] for signature in _CORRECTED}
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            for signature in _CORRECTED:
                acl = _identity(engine, signature)["acl"]
                assert f"minos_admin=X/{'minos_admin'}" in acl
                assert "minos_runner=X/minos_admin" in acl
                assert principal not in acl
                # and it is a real capability, not just an ACL string.
                for role in ("minos_admin", "minos_runner"):
                    assert _may_execute(engine, role, signature) is True
                for role in ("minos_evaluator", "minos_trainer", "minos_live", "public"):
                    assert _may_execute(engine, role, signature) is False
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            for signature in _CORRECTED:
                assert _identity(engine, signature)["acl"] == at_0016[signature], (
                    f"{signature} did not come back with 0011's grants"
                )
                assert _may_execute(engine, "minos_admin", signature) is True
                assert _may_execute(engine, "minos_runner", signature) is True
        finally:
            engine.dispose()
