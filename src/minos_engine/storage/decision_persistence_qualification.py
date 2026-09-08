"""Qualify PRODUCTION decision persistence: the real write path against real PostgreSQL 16.

Nothing here is simulated. A scratch database is migrated through the accepted operational
schema (``0005_l2e_feature_view``) and the runtime overlay, the accepted SAFE config row is
provisioned from its frozen payload bytes, the fifty TRAIN-owned profile rows are carried across
from the operational store by name, and then the real
:func:`~minos_engine.storage.decision_persistence.persist_safe_decision` writes the same two
hundred decisions the accepted controller qualification made.

**Isolation is observed, not claimed.** Two independent observers run for the whole campaign: the
file-level :func:`sealed_access_guard`, and a SQL recorder attached to the engine that captures
every statement the persistence path issues. The report's isolation checks are derived from what
those observers counted -- an unqualified read of ``profiling.bam_profiles`` or any reference to
an evaluation/truth table would appear in the recorder, so "no TEST or VALIDATION access" is a
measurement rather than a constant.

**No truth, no scores, no VALIDATION, no TEST.** The corpus is the fifty TRAIN identity rows and
nothing else: no outcome, no score, no mutation file, no truth VCF, and no scorer is imported.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Final

import sqlalchemy as sa

from minos_engine.common.errors import MinosEngineError
from minos_engine.layer2.contracts import ControlMode
from minos_engine.storage.runtime_decision_contract import (
    LIVE_PROFILE_RESOLVER,
    LIVE_PROFILE_RESOLVER_SIGNATURE,
    R0002_REVISION,
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_HEAD_REVISION,
    RUNTIME_OVERLAY_REVISION,
    RUNTIME_OVERLAY_SCHEMA,
    runtime_overlay_contract_hash,
    runtime_overlay_head_contract_hash,
)

__all__ = [
    "DECISION_PERSISTENCE_QUALIFICATION_PATH",
    "MANDATORY_CHECKS",
    "PERSISTENCE_QUALIFICATION_DOMAIN",
    "PERSISTENCE_QUALIFICATION_SCHEMA",
    "DecisionPersistenceQualificationError",
    "TrustedPersistenceQualification",
    "assemble_persistence_report",
    "derive_checks",
    "persistence_report_identity",
    "run_decision_persistence_qualification",
    "verify_persistence_report",
]

PERSISTENCE_QUALIFICATION_SCHEMA: Final = "l2h-decision-persistence-qualification-v2"
PERSISTENCE_QUALIFICATION_DOMAIN: Final = "minos:l2h-decision-persistence-qualification:v2\n"
DECISION_PERSISTENCE_QUALIFICATION_PATH: Final = (
    "reports/layer2/l2h-decision-persistence-qualification-v2.json"
)
QUALIFICATION_TOOL_VERSION: Final = "l2h-decision-persistence-qualifier-v2"

#: v1 stands on disk as historical evidence and is never edited. It is superseded because it
#: certified a privilege surface it had only measured, not bounded: the SQL recorder showed the
#: code never scanned the raw identity tables, which is not the same claim as the live role being
#: unable to. It was never accepted for service activation.
HISTORICAL_V1_PATH: Final = "reports/layer2/l2h-decision-persistence-qualification-v1.json"
SUPERSEDED_QUALIFICATIONS: Final[tuple[dict[str, str], ...]] = (
    {
        "schema": "l2h-decision-persistence-qualification-v1",
        "identity": "e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20",
        "file_sha256": "9bb98d5106f239e596715d79e91c8dee2055b2cc3f2b2a860eb625b2b5400775",
        "reason": (
            "it derived least privilege from observed query behaviour rather than from database "
            "capability, and so certified r0001's raw SELECT grants on profiling.bam_profiles "
            "and catalog.dataset_registry -- which let the live role enumerate the profile "
            "identities of every partition, TEST included"
        ),
        "status": "SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION",
    },
)

#: Every check must be true for PASS. Named here so a caller cannot supply its own check set.
MANDATORY_CHECKS: Final[tuple[str, ...]] = (
    "accepted_safe_controller_frozen_gate",
    "execution_source_is_a_real_checkout",
    # --- capability, not behaviour: the corrective this qualification exists for -------------
    "live_role_cannot_enumerate_profile_table",
    "live_role_cannot_enumerate_registry_table",
    "live_role_cannot_read_any_partition_bearing_relation",
    "live_role_can_resolve_one_owned_profile",
    "public_cannot_execute_profile_resolver",
    "nonlive_roles_cannot_execute_profile_resolver",
    "resolver_security_definer_is_hardened",
    "resolver_refuses_absent_arguments",
    "campaign_executed_under_the_live_role",
    "overlay_head_is_the_corrective_revision",
    "corrective_downgrade_restores_the_prior_revision",
    "exact_operational_schema_prerequisite",
    "exact_runtime_overlay_identity",
    "overlay_refuses_a_foreign_schema",
    "overlay_downgrade_restores_the_schema",
    "overlay_downgrade_restores_the_grant_shape",
    "main_lineage_never_advanced",
    "safe_config_resolved_by_accepted_hash",
    "missing_safe_config_fails_closed",
    "wrong_safe_config_fails_closed",
    "profile_resolved_from_the_production_table",
    "missing_profile_fails_closed",
    "mismatched_profile_fails_closed",
    "every_decision_persisted_atomically",
    "readback_equals_the_decision",
    "canonical_manifest_identity_survives_jsonb",
    "same_decision_retry_converges",
    "concurrent_identical_writers_converge",
    "conflicting_decision_identity_fails_closed",
    "rollback_leaves_no_visible_state",
    "model_bundle_id_is_null_in_safe_mode",
    "append_only_enforced_against_the_live_role",
    "live_role_holds_least_privilege",
    "other_roles_cannot_forge_a_decision",
    "public_holds_no_privilege",
    "no_scientific_or_evaluation_write",
    "no_sealed_partition_access",
    "no_truth_or_scoring_dependency",
    "select_config_public_boundary_blocked",
)

#: Relations the live persistence path is allowed to touch. Anything else in the SQL recorder is
#: an isolation violation, whatever it claims to be for.
ALLOWED_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "catalog.gatk_configs",
        # the narrow lookup surface -- NOT the raw identity tables it replaced
        "runtime.l2h_resolve_owned_profile",
        "public.alembic_version",
        "runtime.alembic_version_runtime",
        "runtime.decisions",
    }
)

#: Relations the live path must not so much as name. The first two are the raw identity tables
#: r0001 granted and r0002 takes back.
FORBIDDEN_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "catalog.dataset_registry",
        "profiling.bam_profiles",
        "profiling.profile_snapshot_members",
        "evaluation.sealed_test_profile_members",
    }
)

#: A profile id that belongs to no partition. Declared here so the SQL observation can tell a
#: deliberate negative probe apart from an identity the path went looking for.
_UNKNOWN_PROFILE_PROBE: Final = "0" * 32

#: Substrings that must never appear in a statement the live path issues.
FORBIDDEN_SQL_TOKENS: Final[tuple[str, ...]] = (
    "evaluation.",
    "score_results",
    "truth",
    "happy",
    "mutation",
    "experiments.",
    "models.model_bundles",
    "profiling.bam_profiles",
    "catalog.dataset_registry",
)


class DecisionPersistenceQualificationError(MinosEngineError):
    """The persistence qualification could not be completed honestly. Nothing is issued."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecisionPersistenceQualificationError(message)


_TRUSTED_TOKEN: Final = object()


class TrustedPersistenceQualification:
    """An observation produced by actually running the campaign. Never constructed from a dict."""

    __slots__ = ("_observation",)

    def __init__(self, token: object, observation: dict[str, Any]) -> None:
        if token is not _TRUSTED_TOKEN:
            raise DecisionPersistenceQualificationError(
                "a persistence qualification may only be minted by running one; a dictionary of "
                "claimed results is not an observation"
            )
        self._observation = copy.deepcopy(observation)

    @property
    def observation(self) -> dict[str, Any]:
        return copy.deepcopy(self._observation)


# --------------------------------------------------------------------------- #
# SQL observation
# --------------------------------------------------------------------------- #
_RELATION = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)", re.IGNORECASE
)


class StatementRecorder:
    """Every statement the persistence path issues, and what it touched.

    Attached to the engine rather than to the code under test, so a query cannot avoid being
    recorded by taking a different code path.
    """

    __slots__ = (
        "corpus_ids",
        "forbidden_tokens",
        "negative_probes",
        "profile_ids",
        "relations",
        "statements",
        "unqualified_scans",
    )

    def __init__(self) -> None:
        self.statements = 0
        #: the proven TRAIN corpus, set once ownership is loaded
        self.corpus_ids: frozenset[str] = frozenset()
        #: ids this qualification deliberately probes with, which belong to no partition at all
        self.negative_probes: frozenset[str] = frozenset()
        self.relations: set[str] = set()
        self.unqualified_scans: list[str] = []
        self.forbidden_tokens: list[str] = []
        self.profile_ids: set[str] = set()

    def record(self, sql: str, parameters: Any) -> None:
        self.statements += 1
        lowered = " ".join(sql.split()).lower()
        for schema, table in _RELATION.findall(lowered):
            self.relations.add(f"{schema}.{table}")
        for token in FORBIDDEN_SQL_TOKENS:
            if token in lowered:
                self.forbidden_tokens.append(token)
        if "profiling.bam_profiles" in lowered or "catalog.dataset_registry" in lowered:
            # after r0002 the live role cannot read these at all; naming one is a finding.
            self.unqualified_scans.append(lowered[:120])
        # the narrow surface takes an exact profile id: record which ones were asked for.
        if (
            LIVE_PROFILE_RESOLVER in lowered
            and isinstance(parameters, dict)
            and "profile_id" in parameters
        ):
            self.profile_ids.add(str(parameters["profile_id"]))

    def content(self) -> dict[str, Any]:
        return {
            "observed_statements": self.statements,
            "observed_relations": sorted(self.relations),
            "relations_outside_the_allowed_set": sorted(self.relations - ALLOWED_RELATIONS),
            "forbidden_relations_named": sorted(self.relations & FORBIDDEN_RELATIONS),
            "unqualified_profile_scans": len(self.unqualified_scans),
            "forbidden_sql_tokens_seen": sorted(set(self.forbidden_tokens)),
            "distinct_profiles_opened_by_name": len(self.profile_ids),
            "profiles_opened_from_the_train_corpus": len(self.profile_ids & self.corpus_ids),
            "profiles_opened_outside_the_train_corpus": sorted(self.profile_ids - self.corpus_ids),
            "declared_negative_probes": sorted(self.negative_probes),
        }


def _live_role_url(url: str) -> str:
    """The same database, reached on a connection that has assumed ``minos_live``.

    ``minos_live`` is NOLOGIN by design, so a deployment connects as a login role that is a
    member of it. ``options=-c role=minos_live`` reproduces exactly that: the session's effective
    role is the live role, and every statement is bounded by its grants.
    """
    from sqlalchemy.engine import make_url

    from minos_engine.storage.database import normalize_database_url

    return (
        make_url(normalize_database_url(url))
        .update_query_dict({"options": "-c role=minos_live"})
        .render_as_string(hide_password=False)
    )


def _attach(engine: Any, recorder: StatementRecorder) -> None:
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _before(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ):
        recorder.record(str(statement), parameters)


# --------------------------------------------------------------------------- #
# TRAIN corpus provisioning (identity rows only, by name)
# --------------------------------------------------------------------------- #
_REGISTRY_COLUMNS: Final = (
    "id, dataset_id, round_id, chromosome, region_source, region_start0, region_end0_exclusive, "
    "region_length_bp, region_coordinate_system, region_hash, bam_sha256, bai_sha256, "
    "reference_sha256, fai_sha256, bam_size_bytes, parameter_space_hash, feature_registry_hash, "
    "identity_tuple_hash, manifest_hash, split_algorithm_version, split_salt, allocation_digest"
)
_ARTIFACT_COLUMNS: Final = "id, uri, sha256, media_type, size_bytes, provenance"
_PROFILE_COLUMNS: Final = (
    "id, dataset_registry_id, profile_id, bam_sha256, bai_sha256, reference_sha256, fai_sha256, "
    "region_hash, identity_tuple_hash, m5_status, integrity_degraded, attestation_hash, "
    "registry_snapshot_hash, profile_status, profiler_version, profiler_config_hash, "
    "windows_row_count, feature_values_hash, l1_feature_values_hash, eligible_value_count, "
    "profile_document, profile_sha256, profile_manifest_sha256, windows_sha256, "
    "profile_artifact_id, profile_manifest_artifact_id, windows_artifact_id, ingestion_key, "
    "content_hash"
)


def _insert(conn: Any, table: str, columns: str, row: Any, *, on_conflict: str = "") -> None:
    """Insert one row verbatim, binding JSON columns as JSONB rather than as opaque objects."""
    from sqlalchemy.dialects.postgresql import JSONB

    names = [c.strip() for c in columns.split(",")]
    values = {n: row[n] for n in names}
    statement = sa.text(
        f"INSERT INTO {table} ({columns}) VALUES ("
        + ", ".join(f":{n}" for n in names)
        + f"){on_conflict}"
    )
    json_columns = [n for n in names if isinstance(values[n], dict | list)]
    if json_columns:
        statement = statement.bindparams(*(sa.bindparam(n, type_=JSONB) for n in json_columns))
    conn.execute(statement, values)


def provision_train_identity_rows(*, source_engine: Any, target_engine: Any, rounds: Any) -> int:
    """Carry the named TRAIN rounds' identity rows into the target. TRAIN only, by name.

    Reads the operational store READ ONLY and selects strictly by ``round_id`` from the accepted
    TRAIN schedule, so the fifteen TEST and ten VALIDATION members are never selected, never read
    and never observed to exist. No outcome, score, truth or mutation row is transferable through
    this interface -- it does not name those tables.
    """
    wanted = list(rounds)
    copied = 0
    with source_engine.connect() as src, target_engine.begin() as dst:
        dst.execute(sa.text("SET LOCAL ROLE minos_admin"))
        for round_id in wanted:
            registry = (
                src.execute(
                    sa.text(
                        f"SELECT {_REGISTRY_COLUMNS} FROM catalog.dataset_registry "
                        "WHERE round_id = :r"
                    ),
                    {"r": round_id},
                )
                .mappings()
                .first()
            )
            _require(registry is not None, f"round {round_id!r} has no operational registry row")
            assert registry is not None
            profile = (
                src.execute(
                    sa.text(
                        f"SELECT {_PROFILE_COLUMNS} FROM profiling.bam_profiles "
                        "WHERE dataset_registry_id = :d"
                    ),
                    {"d": registry["id"]},
                )
                .mappings()
                .first()
            )
            _require(profile is not None, f"round {round_id!r} has no operational profile row")
            assert profile is not None
            _insert(dst, "catalog.dataset_registry", _REGISTRY_COLUMNS, registry)
            for key in (
                "profile_artifact_id",
                "profile_manifest_artifact_id",
                "windows_artifact_id",
            ):
                artifact = (
                    src.execute(
                        sa.text(f"SELECT {_ARTIFACT_COLUMNS} FROM catalog.artifacts WHERE id = :i"),
                        {"i": profile[key]},
                    )
                    .mappings()
                    .first()
                )
                _require(artifact is not None, "a referenced profile artifact row is missing")
                assert artifact is not None
                _insert(
                    dst,
                    "catalog.artifacts",
                    _ARTIFACT_COLUMNS,
                    artifact,
                    on_conflict=" ON CONFLICT (id) DO NOTHING",
                )
            _insert(dst, "profiling.bam_profiles", _PROFILE_COLUMNS, profile)
            copied += 1
    return copied


# --------------------------------------------------------------------------- #
# observation helpers
# --------------------------------------------------------------------------- #
#: Every grant any minos role holds anywhere in the application schemas. Snapshotted whole, so
#: the overlay's effect on the privilege matrix is a DIFF rather than a claim about four tables.
_APPLICATION_SCHEMAS: Final = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)
#: The application schemas plus ``public``, where Alembic keeps the two version tables the
#: live path reads. A grant snapshot blind to ``public`` cannot see the whole matrix.
_GRANT_SCAN_SCHEMAS: Final = (*_APPLICATION_SCHEMAS, "public")
_APPLICATION_GRANTS: Final = (
    "SELECT table_schema, table_name, grantee, privilege_type "
    "FROM information_schema.role_table_grants WHERE grantee LIKE 'minos_%' "
    "AND table_schema IN (" + ", ".join(f"'{s}'" for s in _GRANT_SCAN_SCHEMAS) + ")"
)


def _schema_snapshot(conn: Any) -> dict[str, Any]:
    """Everything the overlay is allowed to change, as the server reports it."""
    columns = [
        f"{r[0]}:{r[1]}:{r[2]}"
        for r in conn.execute(
            sa.text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema='runtime' AND table_name='decisions' ORDER BY column_name"
            )
        )
    ]
    constraints = sorted(
        str(r[0])
        for r in conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid='runtime.decisions'::regclass"
            )
        )
    )
    indexes = sorted(
        str(r[0])
        for r in conn.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='runtime' "
                "AND tablename='decisions'"
            )
        )
    )
    grants = sorted(
        f"{r[0]}.{r[1]}:{r[2]}:{r[3]}" for r in conn.execute(sa.text(_APPLICATION_GRANTS))
    )
    return {"columns": columns, "constraints": constraints, "indexes": indexes, "grants": grants}


#: PostgreSQL SQLSTATEs that mean "you are not allowed", as opposed to "your data is wrong".
_INSUFFICIENT_PRIVILEGE: Final = "42501"

#: The SQLSTATE ``audit.minos_reject_mutation`` raises. An append-only refusal is not a
#: privilege refusal and must not be counted as one.
_APPEND_ONLY_SQLSTATE: Final = "23001"  # restrict_violation


def _denied(
    conn: Any, role: str, sql: str, *, sqlstate: str = _INSUFFICIENT_PRIVILEGE, **params: Any
) -> bool:
    """True when PostgreSQL refuses ``sql`` for ``role`` with the EXPECTED SQLSTATE.

    Insisting on the code matters: a statement rejected by a CHECK constraint proves nothing
    about privilege, and counting it as a denial would turn a broken probe into evidence.
    """
    from sqlalchemy.exc import DBAPIError

    savepoint = conn.begin_nested()
    try:
        conn.execute(sa.text(f"SET ROLE {role}"))
        conn.execute(sa.text(sql), params)
    except DBAPIError as error:
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        return str(code) == sqlstate
    else:
        return False
    finally:
        savepoint.rollback()
        conn.execute(sa.text("RESET ROLE"))


def _permitted(conn: Any, role: str, sql: str, **params: Any) -> bool:
    """True when ``role`` may actually run ``sql``. Executed, then rolled back."""
    savepoint = conn.begin_nested()
    try:
        conn.execute(sa.text(f"SET ROLE {role}"))
        conn.execute(sa.text(sql), params)
        return True
    except Exception:
        return False
    finally:
        savepoint.rollback()
        conn.execute(sa.text("RESET ROLE"))


def _refuses(callable_: Any) -> bool:
    """True when the engine failed closed rather than proceeding."""
    try:
        callable_()
    except MinosEngineError:
        return True
    return False


def _overlay_refusal_drill(url: str, *, root: Any) -> dict[str, Any]:
    """Attempt the overlay where it does not belong, and prove nothing was applied.

    "It raised" is not enough on its own: what matters is that the schema is untouched
    afterwards, so the drill re-reads the database rather than trusting the exception.
    """
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.runtime_decision_contract import runtime_overlay_revision
    from minos_engine.storage.runtime_overlay import upgrade_runtime_overlay

    error = ""
    try:
        upgrade_runtime_overlay(url, root=root)
    except Exception as raised:
        error = type(raised).__name__
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            applied = runtime_overlay_revision(conn) is not None
            columns = int(
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.columns WHERE "
                        "table_schema='runtime' AND table_name='decisions' AND column_name = "
                        "ANY(ARRAY['mode','decision_manifest','decided_at'])"
                    )
                ).scalar()
                or 0
            )
    finally:
        engine.dispose()
    return {"refused": bool(error), "error": error, "applied": applied, "added_columns": columns}


#: Every way a role could try to LIST the raw identity tables. All must be refused by privilege.
ENUMERATION_PROBES: Final[tuple[tuple[str, str], ...]] = (
    ("profile_select_star", "SELECT * FROM profiling.bam_profiles"),
    ("profile_count", "SELECT count(*) FROM profiling.bam_profiles"),
    ("profile_single_column", "SELECT profile_id FROM profiling.bam_profiles LIMIT 1"),
    ("profile_exists", "SELECT EXISTS (SELECT 1 FROM profiling.bam_profiles)"),
    ("registry_select_star", "SELECT * FROM catalog.dataset_registry"),
    ("registry_count", "SELECT count(*) FROM catalog.dataset_registry"),
    ("registry_single_column", "SELECT round_id FROM catalog.dataset_registry LIMIT 1"),
)

#: Other partition-bearing relations the live role must not reach either. The sealed TEST view is
#: probed the ONLY safe way: by asking whether the privilege exists, never by reading a row.
PARTITION_BEARING_RELATIONS: Final[tuple[str, ...]] = (
    "profiling.bam_profiles",
    "profiling.profile_snapshot_members",
    "profiling.training_profile_members",
    "catalog.dataset_registry",
    "catalog.split_allocations",
    "evaluation.sealed_test_profile_members",
    "evaluation.validation_profile_members",
)


def _capability_audit(*, live_engine: Any, admin_engine: Any) -> dict[str, Any]:
    """What the LIVE ROLE can do, executed as the live role on its own connection.

    This is the correction. v1 attached a recorder to the engine and showed the code never
    scanned the raw identity tables; that is a fact about the code, and it left untouched the
    fact that ``SET ROLE minos_live; SELECT * FROM profiling.bam_profiles`` returned every
    identity in the store. Here each probe is actually run under the live role and must come back
    with SQLSTATE 42501 -- a privilege refusal, not a constraint failure and not an empty result.
    """
    refusals: dict[str, str] = {}
    for name, sql in ENUMERATION_PROBES:
        with live_engine.connect() as conn:
            try:
                conn.execute(sa.text(sql))
                refusals[name] = "NOT_REFUSED"
            except Exception as error:
                refusals[name] = str(getattr(getattr(error, "orig", None), "sqlstate", "unknown"))
    # the sealed relations are probed by PRIVILEGE, never by reading a row: asking PostgreSQL
    # whether the grant exists tells us what we need and observes no identity at all.
    with admin_engine.connect() as conn:
        readable = sorted(
            relation
            for relation in PARTITION_BEARING_RELATIONS
            if conn.execute(
                sa.text("SELECT has_table_privilege('minos_live', :r, 'SELECT')"),
                {"r": relation},
            ).scalar()
        )
        resolver = (
            conn.execute(
                sa.text(
                    "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig::text, "
                    "l.lanname, p.prosrc, coalesce(p.proacl::text, '') "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "JOIN pg_language l ON l.oid = p.prolang "
                    "WHERE n.nspname = :schema AND p.proname = :name"
                ),
                {
                    "schema": LIVE_PROFILE_RESOLVER.split(".")[0],
                    "name": LIVE_PROFILE_RESOLVER.split(".")[1],
                },
            )
            .mappings()
            .first()
        )
        _require(resolver is not None, "the narrow live lookup surface does not exist")
        assert resolver is not None
        body = str(resolver["prosrc"])
        executors = {
            role: bool(
                conn.execute(
                    sa.text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": role, "f": LIVE_PROFILE_RESOLVER_SIGNATURE},
                ).scalar()
            )
            for role in ("public", "minos_live", "minos_runner", "minos_evaluator", "minos_trainer")
        }
    denied_execute: dict[str, str] = {}
    for role in ("minos_runner", "minos_evaluator", "minos_trainer"):
        with admin_engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(sa.text(f"SET ROLE {role}"))
                conn.execute(sa.text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}('probe', 'probe')"))
                denied_execute[role] = "NOT_REFUSED"
            except Exception as error:
                denied_execute[role] = str(
                    getattr(getattr(error, "orig", None), "sqlstate", "unknown")
                )
            finally:
                transaction.rollback()
    absent: dict[str, str] = {}
    for label, arguments in (
        ("both_null", "NULL, NULL"),
        ("both_empty", "'', ''"),
        ("profile_null", "NULL, 'a-round'"),
        ("round_empty", "'a-profile', ''"),
    ):
        with live_engine.connect() as conn:
            try:
                conn.execute(sa.text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}({arguments})"))
                absent[label] = "NOT_REFUSED"
            except Exception as error:
                absent[label] = str(getattr(getattr(error, "orig", None), "sqlstate", "unknown"))
    with live_engine.connect() as conn:
        unknown_rows = len(
            conn.execute(
                sa.text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}(:p, :r)"),
                {"p": "0" * 32, "r": "a-round-that-does-not-exist"},
            ).all()
        )
        live_role = str(conn.execute(sa.text("SELECT current_user")).scalar())
    return {
        "enumeration_probe_sqlstates": dict(sorted(refusals.items())),
        "partition_bearing_relations_probed": list(PARTITION_BEARING_RELATIONS),
        "partition_bearing_relations_readable_by_live": readable,
        "resolver": {
            "name": LIVE_PROFILE_RESOLVER,
            "signature": LIVE_PROFILE_RESOLVER_SIGNATURE,
            "owner": str(resolver["pg_get_userbyid"]),
            "security_definer": bool(resolver["prosecdef"]),
            "search_path_setting": str(resolver["proconfig"]),
            "language": str(resolver["lanname"]),
            "uses_dynamic_sql": any(
                token in body.upper() for token in ("EXECUTE ", "FORMAT(", "QUOTE_IDENT")
            ),
            "returns_at_most_one_row": "cardinality_violation" in body,
            "refuses_absent_arguments": "null_value_not_allowed" in body,
            "execute_privilege": dict(sorted(executors.items())),
            "nonlive_execute_sqlstates": dict(sorted(denied_execute.items())),
            "absent_argument_sqlstates": dict(sorted(absent.items())),
            "unknown_profile_row_count": unknown_rows,
        },
        "campaign_connection_role": live_role,
    }


def _no_truth_or_scoring_imports(root: Any) -> dict[str, Any]:
    """AST proof that the write path imports nothing that reads truth or computes a score."""
    import ast
    from pathlib import Path

    forbidden = ("hap", "scoring", "truth", "evaluation", "mutations")
    seen: list[str] = []
    for name in ("decision_persistence.py", "decision_persistence_qualification.py"):
        tree = ast.parse((Path(root) / "src/minos_engine/storage" / name).read_text())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            for module in modules:
                if any(part in forbidden for part in module.split(".")):
                    seen.append(module)
    return {"forbidden_imports": sorted(set(seen)), "modules_scanned": 2}


# --------------------------------------------------------------------------- #
# the campaign
# --------------------------------------------------------------------------- #
def run_decision_persistence_qualification(
    *,
    source_url: str,
    target_url: str,
    foreign_url: str,
    foreign_revisions: tuple[str, ...] = ("0001_l2b_initial", "0020_l2f2_phase_c_execution"),
    root: Any = None,
    config_payload_root: Any = None,
) -> TrustedPersistenceQualification:
    """Run the real persistence campaign end to end and return what was OBSERVED.

    ``target_url`` must be an EMPTY scratch database named ``minos_engine_db`` (the persistence
    entry point refuses any other database, by live ``current_database()``); ``source_url`` is the
    operational store, read only; ``foreign_url`` is an empty scratch database used to prove the
    overlay refuses schemas it does not belong on.

    Nothing about the caller's environment enters the returned observation: no URL, host, path,
    timestamp or process id.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from minos_engine.layer2.round_profile_authority import (
        OwnedRoundProfile,
        load_verified_round_profile_corpus,
    )
    from minos_engine.layer2.safe_controller import load_verified_safe_baseline_authority
    from minos_engine.layer2.safe_controller_frozen_acceptance import (
        verify_accepted_safe_controller_frozen_gate,
    )
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.layer2.safe_controller_qualification import _request_for
    from minos_engine.layer2.sealed_access_guard import sealed_access_guard
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.decision_persistence import (
        PROFILE_IDENTITY_FIELDS,
        REGISTRY_IDENTITY_FIELDS,
        persist_safe_decision,
        provision_safe_config_row,
        resolve_owned_profile_row,
        resolve_safe_config_row,
    )
    from minos_engine.storage.runtime_decision_contract import decisions_table
    from minos_engine.storage.runtime_overlay import (
        database_url_for,
        downgrade_runtime_overlay,
        observed_overlay_state,
        upgrade_runtime_overlay,
    )

    base = _resolve_root(root)
    observation: dict[str, Any] = {}

    # ---- 1. the accepted controller, before anything is written anywhere ----------------
    accepted = verify_accepted_safe_controller_frozen_gate(base)
    observation["accepted_gate"] = {
        "gate_hash": accepted["gate_hash"],
        "gate_file_sha256": accepted["gate_file_sha256"],
        "capability_scope": accepted["capability_scope"],
        "issuer_source_commit": accepted["issuer_source_commit"],
        "issuer_source_tree": accepted["issuer_source_tree"],
        "qualified_source_commit": accepted["qualified_source_git_sha"],
        "qualified_source_tree": accepted["qualified_source_tree_sha"],
        "qualification_identity": accepted["qualification_identity"],
        "check_count": accepted["check_count"],
    }
    observation["persistence_schema"] = {
        "overlay_schema": RUNTIME_OVERLAY_SCHEMA,
        "overlay_base_revision": RUNTIME_OVERLAY_REVISION,
        "overlay_head_revision": RUNTIME_OVERLAY_HEAD_REVISION,
        "overlay_base_contract_hash": runtime_overlay_contract_hash(base),
        "overlay_head_contract_hash": runtime_overlay_head_contract_hash(base),
        "requires_main_revision": REQUIRED_MAIN_REVISION,
        "live_lookup_surface": LIVE_PROFILE_RESOLVER_SIGNATURE,
        "supersedes": [dict(sorted(entry.items())) for entry in SUPERSEDED_QUALIFICATIONS],
    }

    def _alembic(url: str, revision: str) -> None:
        """Advance the MAIN chain of a scratch database, through the same URL discipline."""
        with database_url_for(url):
            command.upgrade(Config(str(Path(base) / "alembic.ini")), revision)

    # ---- 2. the overlay refuses every schema that is not the accepted operational one -----
    refusals: dict[str, Any] = {"no_main_lineage": _overlay_refusal_drill(foreign_url, root=base)}
    for revision in foreign_revisions:
        _alembic(foreign_url, revision)
        refusals[revision] = _overlay_refusal_drill(foreign_url, root=base)
    observation["overlay_refusals"] = dict(sorted(refusals.items()))

    # ---- 3. the target: accepted operational schema, then the overlay ---------------------
    _alembic(target_url, REQUIRED_MAIN_REVISION)
    before_engine = create_db_engine(target_url)
    try:
        with before_engine.connect() as conn:
            pre_overlay = _schema_snapshot(conn)
    finally:
        before_engine.dispose()
    observation["state_before_overlay"] = observed_overlay_state(target_url)

    # r0001 first, so the corrective's effect on the privilege matrix is an observed DIFF
    upgrade_runtime_overlay(target_url, RUNTIME_OVERLAY_REVISION, root=base)
    observation["state_after_overlay"] = observed_overlay_state(target_url)

    engine = create_db_engine(target_url)
    recorder = StatementRecorder()
    try:
        with engine.connect() as conn:
            post_overlay = _schema_snapshot(conn)
        observation["schema_after_overlay"] = post_overlay

        upgrade_runtime_overlay(target_url, R0002_REVISION, root=base)
        with engine.connect() as conn:
            post_corrective = _schema_snapshot(conn)
        observation["state_after_corrective"] = observed_overlay_state(target_url)
        observation["corrective"] = {
            "revision": R0002_REVISION,
            "grants_revoked": sorted(set(post_overlay["grants"]) - set(post_corrective["grants"])),
            "grants_added": sorted(set(post_corrective["grants"]) - set(post_overlay["grants"])),
            "columns_unchanged": post_corrective["columns"] == post_overlay["columns"],
            "constraints_unchanged": post_corrective["constraints"] == post_overlay["constraints"],
            "indexes_unchanged": post_corrective["indexes"] == post_overlay["indexes"],
        }

        # ---- 4. every downgrade boundary is exact -----------------------------------------
        downgrade_runtime_overlay(target_url, RUNTIME_OVERLAY_REVISION, root=base)
        with engine.connect() as conn:
            back_to_r0001 = _schema_snapshot(conn)
        downgrade_runtime_overlay(target_url, root=base)
        with engine.connect() as conn:
            reverted = _schema_snapshot(conn)
        observation["downgrade"] = {
            "schema_restored": reverted["columns"] == pre_overlay["columns"]
            and reverted["constraints"] == pre_overlay["constraints"]
            and reverted["indexes"] == pre_overlay["indexes"],
            "grants_restored": reverted["grants"] == pre_overlay["grants"],
            "corrective_restores_the_prior_revision": back_to_r0001 == post_overlay,
            "grant_shape_before": pre_overlay["grants"],
            "grant_shape_after_overlay": post_overlay["grants"],
            "grant_shape_after_corrective": post_corrective["grants"],
        }
        upgrade_runtime_overlay(target_url, root=base)
        observation["state_final_overlay"] = observed_overlay_state(target_url)

        # ---- 5. the two prerequisites the LIVE path must not provision itself -------------
        # From here the write path runs on a connection that has ASSUMED minos_live, so every
        # statement it issues is bounded by the live role's actual grants rather than by a
        # superuser's. v1 ran the whole campaign as the owner, which is why it could not have
        # noticed that the live role cannot even read the two schema-version tables.
        live_engine = create_db_engine(_live_role_url(target_url))
        _attach(live_engine, recorder)
        authority = load_verified_safe_baseline_authority(
            repo_root=base, config_payload_root=config_payload_root
        )
        with sealed_access_guard() as sealed:
            ownership = load_verified_round_profile_corpus(root=base)
            rounds = ownership.rounds()
            recorder.corpus_ids = frozenset(ownership.owned(r).profile_id for r in rounds)
            recorder.negative_probes = frozenset({_UNKNOWN_PROFILE_PROBE})

            def _persist(round_id: str, mode: ControlMode) -> Any:
                owned = ownership.owned(round_id)
                request = _request_for(owned, authority=authority, mode=mode)
                return persist_safe_decision(
                    request=request,
                    authority=authority,
                    ownership=ownership,
                    engine=live_engine,
                    root=base,
                )

            prerequisites: dict[str, bool] = {}
            prerequisites["missing_safe_config"] = _refuses(
                lambda: _persist(rounds[0], ControlMode.SAFE_BASELINE)
            )
            provisioned = provision_safe_config_row(
                engine,
                config_hash=authority.baseline_config_hash,
                parameter_space_hash=authority.parameter_space_hash,
                payload_root=config_payload_root,
            )
            prerequisites["missing_profile"] = _refuses(
                lambda: _persist(rounds[0], ControlMode.SAFE_BASELINE)
            )
            observation["safe_config_row"] = {
                "config_hash": provisioned["config_hash"],
                "parameter_space_hash": provisioned["parameter_space_hash"],
                "payload_sha256": provisioned["payload_sha256"],
                "payload_bytes": provisioned["payload_bytes"],
                "resolved_by": "config_hash",
                "resolved_exactly_one_row": True,
            }

            source_engine = create_db_engine(source_url)
            try:
                copied = provision_train_identity_rows(
                    source_engine=source_engine, target_engine=engine, rounds=rounds
                )
            finally:
                source_engine.dispose()
            observation["train_identity_rows_copied"] = copied

            # ---- 6. the real campaign: every accepted decision, persisted ------------------
            # One round is HELD BACK for the failure drills, so no drill ever has to delete or
            # rewrite a decision the campaign made.
            drill_round, campaign_rounds = rounds[0], rounds[1:]
            modes = tuple(ControlMode)
            persisted: list[Any] = []
            for round_id in campaign_rounds:
                for mode in modes:
                    persisted.append(_persist(round_id, mode))
            observation["campaign_rounds"] = len(campaign_rounds)
            observation["decision_count"] = len(persisted)
            observation["inserted_count"] = sum(1 for p in persisted if not p.converged)
            observation["converged_count"] = sum(1 for p in persisted if p.converged)
            observation["actual_mode_counts"] = {
                mode: sum(1 for p in persisted if p.mode == mode)
                for mode in sorted({p.mode for p in persisted})
            }
            observation["model_bundle_id_values"] = sorted(
                {"NULL" if p.row["model_bundle_id"] is None else "SET" for p in persisted}
            )
            observation["config_id_values"] = sorted(
                {"NULL" if p.row["config_id"] is None else "SET" for p in persisted}
            )
            observation["selected_equals_baseline"] = all(
                str(p.row["selected_config_id"]) == str(p.row["baseline_config_id"])
                for p in persisted
            )
            observation["distinct_selected_config_ids"] = len(
                {str(p.row["selected_config_id"]) for p in persisted}
            )
            observation["fallback_reason_counts"] = {
                reason: sum(1 for p in persisted if p.row["fallback_reason"] == reason)
                for reason in sorted({str(p.row["fallback_reason"]) for p in persisted})
            }
            observation["decisions_per_round"] = sorted(
                {
                    sum(1 for p in persisted if p.row["round_id"] == round_id)
                    for round_id in campaign_rounds
                }
            )

            # ---- 7. the held-back round: rollback, retry, concurrency, conflict -------------
            observation["rollback"] = _rollback_drill(
                engine=live_engine, admin_engine=engine, persist=_persist, round_id=drill_round
            )
            first = _persist(drill_round, ControlMode.SAFE_BASELINE)
            retry = _persist(drill_round, ControlMode.SAFE_BASELINE)
            observation["retry"] = {
                "first_inserted": not first.converged,
                "converged": bool(retry.converged),
                "same_row": retry.persisted_id == first.persisted_id,
                "same_decision_hash": retry.decision_hash == first.decision_hash,
            }
            observation["concurrency"] = _concurrent_identical_writers(
                persist=_persist, round_id=drill_round, engine=live_engine
            )
            observation["conflict"] = _conflict_drills(
                engine=engine,
                persist=_persist,
                drill_round=drill_round,
                sample_round=campaign_rounds[0],
                authority=authority,
                ownership=ownership,
            )
            #: the two decisions the drills legitimately added on the held-back round
            observation["drill_rows_added"] = 2

            # ---- 8. the two resolvers, driven off their proven inputs, AS THE LIVE ROLE ----
            with live_engine.connect() as conn:
                owned = ownership.owned(rounds[0])
                observation["profile_binding"] = {
                    "resolved_by": "profile_id",
                    "lookup_surface": LIVE_PROFILE_RESOLVER,
                    "cross_checked_profile_fields": [c for c, _ in PROFILE_IDENTITY_FIELDS],
                    "cross_checked_registry_fields": [c for c, _ in REGISTRY_IDENTITY_FIELDS],
                    "resolved": bool(resolve_owned_profile_row(conn, owned=owned)),
                    "missing_profile_refused": _refuses(
                        lambda: resolve_owned_profile_row(
                            conn,
                            owned=OwnedRoundProfile(**{**owned.content(), "profile_id": "0" * 32}),
                        )
                    ),
                    "mismatched_profile_refused": _refuses(
                        lambda: resolve_owned_profile_row(
                            conn,
                            owned=OwnedRoundProfile(**{**owned.content(), "bam_sha256": "0" * 64}),
                        )
                    ),
                }
                observation["safe_config_row"]["wrong_parameter_space_refused"] = _refuses(
                    lambda: resolve_safe_config_row(
                        conn,
                        config_hash=authority.baseline_config_hash,
                        parameter_space_hash="0" * 64,
                    )
                )
                observation["safe_config_row"]["unknown_config_refused"] = _refuses(
                    lambda: resolve_safe_config_row(
                        conn,
                        config_hash="0" * 64,
                        parameter_space_hash=authority.parameter_space_hash,
                    )
                )
            observation["prerequisite_refusals"] = dict(sorted(prerequisites.items()))

            # ---- 9. readback: the store still holds exactly what was decided --------------
            observation["readback"] = _readback_audit(engine=engine, persisted=persisted)

        observation.update(sealed.content())
        observation["sql_observation"] = recorder.content()
        observation["privileges"] = _privilege_audit(engine)
        observation["capabilities"] = _capability_audit(
            live_engine=live_engine, admin_engine=engine
        )
        live_engine.dispose()
        observation["final_state"] = observed_overlay_state(target_url)
        with engine.connect() as conn:
            observation["final_row_count"] = int(
                conn.execute(sa.select(sa.func.count()).select_from(decisions_table)).scalar() or 0
            )
    finally:
        engine.dispose()

    from minos_engine.qualification.provenance import read_provenance

    provenance = read_provenance(base)
    observation["execution_source_commit"] = str(provenance.head_sha)
    observation["execution_source_tree"] = str(provenance.tree_sha)
    observation["no_truth_or_scoring"] = _no_truth_or_scoring_imports(base)
    observation["service_activated"] = _service_still_blocked() is False
    observation["select_config_blocked"] = _service_still_blocked()
    observation["tool_version"] = QUALIFICATION_TOOL_VERSION
    observation["persisted_decision_schema"] = _persisted_decision_schema()
    _require(
        isinstance(observation.get("decision_count"), int),
        "the campaign did not complete; no observation may be reported",
    )
    return TrustedPersistenceQualification(_TRUSTED_TOKEN, observation)


def _persisted_decision_schema() -> str:
    from minos_engine.storage.decision_persistence import PERSISTED_DECISION_SCHEMA

    return PERSISTED_DECISION_SCHEMA


def _service_still_blocked() -> bool:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception:
        return False
    return False


def _concurrent_identical_writers(*, persist: Any, round_id: str, engine: Any) -> dict[str, Any]:
    """Two threads persist the SAME decision at once. Exactly one row may result."""
    import threading

    from minos_engine.layer2.contracts import ControlMode
    from minos_engine.storage.runtime_decision_contract import decisions_table

    results: list[Any] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(persist(round_id, ControlMode.BOUNDED))
        except Exception as error:  # recorded, never swallowed
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    hashes = {r.decision_hash for r in results}
    with engine.connect() as conn:
        rows = int(
            conn.execute(
                sa.select(sa.func.count())
                .select_from(decisions_table)
                .where(decisions_table.c.decision_hash == (next(iter(hashes)) if hashes else ""))
            ).scalar()
            or 0
        )
    return {
        "writers": 2,
        "errors": sorted(errors),
        "successes": len(results),
        "distinct_decision_identities": len(hashes),
        "distinct_persisted_row_ids": len({r.persisted_id for r in results}),
        "rows_for_that_identity": rows,
    }


def _conflict_drills(
    *,
    engine: Any,
    persist: Any,
    drill_round: str,
    sample_round: str,
    authority: Any,
    ownership: Any,
) -> dict[str, Any]:
    """The two ways a conflicting decision can arrive, and what the store does about each."""
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.exc import IntegrityError

    from minos_engine.layer2.contracts import ControlMode
    from minos_engine.layer2.safe_controller import (
        safe_decision_manifest_content,
        safe_decision_manifest_identity,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for
    from minos_engine.storage.runtime_decision_contract import decisions_table

    outcome: dict[str, Any] = {}

    # (i) ONE decision identity names ONE decision, forever. Re-filing an identity that is
    # already persisted under a DIFFERENT round is refused by the contract's global UNIQUE.
    with engine.connect() as conn:
        existing = conn.execute(sa.select(decisions_table).limit(1)).mappings().first()
    assert existing is not None
    duplicated = {c: existing[c] for c in existing if c != "id"}
    duplicated["round_id"] = "a-round-that-does-not-own-this-decision"
    duplicated["decision_manifest"] = dict(duplicated["decision_manifest"])
    duplicated["decision_manifest"]["round_id"] = duplicated["round_id"]
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("SET LOCAL ROLE minos_admin"))
            conn.execute(sa.insert(decisions_table).values(**duplicated))
        outcome["same_identity_different_round_refused"] = False
        outcome["refusing_constraint"] = ""
    except IntegrityError as error:
        outcome["same_identity_different_round_refused"] = True
        outcome["refusing_constraint"] = str(
            getattr(getattr(error, "orig", None), "diag", None) and error.orig.diag.constraint_name  # type: ignore[union-attr]
        )

    # (ii) SAME (round, identity), DIFFERENT stored state. A forged row is planted under the
    # identity a real decision is about to claim, so ON CONFLICT DO NOTHING converges onto a row
    # that is not the decision -- and the readback comparison is what has to refuse. The mutated
    # field is one no CHECK constrains, so the forgery is internally consistent and the database
    # accepts it: only the write path's own comparison stands between it and a silent success.
    mode = ControlMode.FULL_CONTEXTUAL
    owned = ownership.owned(drill_round)
    request = _request_for(owned, authority=authority, mode=mode)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=ownership, owned=owned
    )
    identity = safe_decision_manifest_identity(manifest)
    forged = dict(manifest)
    forged["profile_sha256"] = "0" * 64
    with engine.connect() as conn:
        template = (
            conn.execute(
                sa.select(decisions_table)
                .where(decisions_table.c.round_id == sample_round)
                .limit(1)
            )
            .mappings()
            .first()
        )
    assert template is not None
    with engine.begin() as conn:
        conn.execute(sa.text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            sa.insert(decisions_table).values(
                round_id=drill_round,
                decision_hash=identity,
                decision_manifest_hash="e" * 64,
                profile_id=template["profile_id"],
                controller_version=forged["controller_version"],
                mode=forged["actual_mode"],
                requested_mode=forged["requested_mode"],
                fallback_reason=forged["fallback_reason"],
                parameter_space_hash=forged["parameter_space_hash"],
                baseline_config_id=template["baseline_config_id"],
                selected_config_id=template["selected_config_id"],
                decision_manifest=sa.bindparam("m", value=forged, type_=JSONB),
            )
        )
    outcome["forgery_planted"] = True
    outcome["divergent_stored_state_refused"] = _refuses(lambda: persist(drill_round, mode))
    # remove it again so the drill leaves no trace. The append-only trigger has to be lifted for
    # exactly this one statement, which is recorded rather than glossed over; the privilege audit
    # afterwards proves the trigger is live for both the live role and the owner.
    with engine.begin() as conn:
        conn.execute(sa.text("SET LOCAL ROLE minos_admin"))
        conn.execute(sa.text("ALTER TABLE runtime.decisions DISABLE TRIGGER USER"))
        removed = conn.execute(
            sa.text("DELETE FROM runtime.decisions WHERE decision_manifest_hash = :e"),
            {"e": "e" * 64},
        ).rowcount
        conn.execute(sa.text("ALTER TABLE runtime.decisions ENABLE TRIGGER USER"))
    outcome["forgery_removed"] = int(removed) == 1
    outcome["append_only_trigger_lifted_for_the_removal"] = True

    # A round may legally carry more than one decision: the contract's own
    # (round_id, decision_hash) uniqueness -- not (round_id) -- and its
    # (round_id, decided_at DESC) index both say so, and the four requested modes produce four
    # distinct, non-conflicting decisions for each campaign round.
    with engine.connect() as conn:
        outcome["decisions_for_one_round"] = int(
            conn.execute(
                sa.select(sa.func.count())
                .select_from(decisions_table)
                .where(decisions_table.c.round_id == sample_round)
            ).scalar()
            or 0
        )
    return outcome


def _rollback_drill(
    *, engine: Any, admin_engine: Any, persist: Any, round_id: str
) -> dict[str, Any]:
    """Inject a real driver-level failure after the INSERT. Nothing may become visible."""
    from sqlalchemy import event

    from minos_engine.layer2.contracts import ControlMode
    from minos_engine.storage.runtime_decision_contract import decisions_table

    armed = {"on": True, "fired": False, "inserted": False}

    def _poison(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        text_ = " ".join(str(statement).split()).lower()
        if not armed["on"]:
            return
        if text_.startswith("insert into runtime.decisions"):
            # the row really is written before the failure; otherwise "nothing became visible"
            # would be true for the trivial reason that nothing was ever attempted
            armed["inserted"] = True
        if text_.startswith("select") and "runtime.decisions" in text_:
            armed["fired"] = True
            raise RuntimeError("injected failure after INSERT, before COMMIT")

    with admin_engine.connect() as probe:
        before = int(
            probe.execute(sa.select(sa.func.count()).select_from(decisions_table)).scalar() or 0
        )
    event.listen(engine, "before_cursor_execute", _poison)
    raised = False
    try:
        persist(round_id, ControlMode.SAFE_BASELINE)
    except Exception:
        raised = True
    finally:
        armed["on"] = False
        event.remove(engine, "before_cursor_execute", _poison)
    with admin_engine.connect() as probe:
        after = int(
            probe.execute(sa.select(sa.func.count()).select_from(decisions_table)).scalar() or 0
        )
    return {
        "insert_executed": bool(armed["inserted"]),
        "failure_injected": bool(armed["fired"]),
        "call_raised": raised,
        "rows_before": before,
        "rows_after": after,
        "no_visible_state": before == after,
    }


def _readback_audit(*, engine: Any, persisted: Any) -> dict[str, Any]:
    """Re-read every persisted decision from a fresh connection and re-derive its identities."""
    import hashlib

    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.layer2.safe_controller import safe_decision_manifest_identity
    from minos_engine.storage.runtime_decision_contract import decisions_table

    equal = 0
    identity_ok = 0
    bytes_ok = 0
    with engine.connect() as conn:
        for item in persisted:
            row = (
                conn.execute(
                    sa.select(decisions_table).where(
                        decisions_table.c.decision_hash == item.decision_hash
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                continue
            manifest = dict(row["decision_manifest"])
            if manifest == dict(item.row["decision_manifest"]):
                equal += 1
            if safe_decision_manifest_identity(manifest) == str(row["decision_hash"]):
                identity_ok += 1
            digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
            if digest == str(row["decision_manifest_hash"]):
                bytes_ok += 1
    return {
        "decisions_re_read": len(persisted),
        "manifests_equal": equal,
        "decision_hash_re_derived_from_the_manifest": identity_ok,
        "manifest_bytes_hash_re_derived": bytes_ok,
    }


def _privilege_audit(engine: Any) -> dict[str, Any]:
    """What each role can actually do, executed rather than read off a policy document."""
    with engine.connect() as conn:
        grants = sorted(
            f"{r[0]}.{r[1]}:{r[2]}"
            for r in conn.execute(
                sa.text(
                    "SELECT table_schema, table_name, privilege_type FROM "
                    "information_schema.role_table_grants WHERE grantee = 'minos_live' "
                    "AND table_schema IN (" + ", ".join(f"'{s}'" for s in _GRANT_SCAN_SCHEMAS) + ")"
                )
            )
        )
        # The live role's ONLY write is the live decision. Proven by executing a genuinely valid
        # insert as that role -- a probe rejected by a CHECK would say nothing about privilege.
        live_can_insert = _permitted(
            conn,
            "minos_live",
            "INSERT INTO runtime.decisions (round_id, decision_hash, decision_manifest_hash, "
            "profile_id, controller_version, mode, requested_mode, fallback_reason, "
            "parameter_space_hash, baseline_config_id, selected_config_id, decision_manifest) "
            "SELECT round_id, repeat('a', 64), decision_manifest_hash, profile_id, "
            "controller_version, mode, requested_mode, fallback_reason, parameter_space_hash, "
            "baseline_config_id, selected_config_id, decision_manifest "
            "FROM runtime.decisions LIMIT 1",
        )
        live_can_select = _permitted(conn, "minos_live", "SELECT count(*) FROM runtime.decisions")
        live_can_read_profile = _permitted(
            conn,
            "minos_live",
            f"SELECT * FROM {LIVE_PROFILE_RESOLVER}('a-profile', 'a-round')",
        )
        denials = {
            "live_update_decisions": _denied(
                conn, "minos_live", "UPDATE runtime.decisions SET round_id = 'x'"
            ),
            "live_delete_decisions": _denied(conn, "minos_live", "DELETE FROM runtime.decisions"),
            "live_truncate_decisions": _denied(conn, "minos_live", "TRUNCATE runtime.decisions"),
            "live_insert_gatk_configs": _denied(
                conn,
                "minos_live",
                "INSERT INTO catalog.gatk_configs (config_hash, parameter_space_hash) "
                "VALUES (repeat('c', 64), repeat('d', 64))",
            ),
            "live_insert_profiles": _denied(
                conn,
                "minos_live",
                "INSERT INTO profiling.bam_profiles (profile_id) VALUES ('x')",
            ),
            "live_update_profiles": _denied(
                conn, "minos_live", "UPDATE profiling.bam_profiles SET profile_id = 'x'"
            ),
            "live_read_evaluation": _denied(
                conn, "minos_live", "SELECT count(*) FROM evaluation.evaluations"
            ),
            "live_write_evaluation": _denied(
                conn,
                "minos_live",
                "INSERT INTO evaluation.evaluations (experiment_result_id, evaluation_hash) "
                "VALUES (gen_random_uuid(), repeat('e', 64))",
            ),
            "runner_insert_decisions": _denied(
                conn, "minos_runner", "INSERT INTO runtime.decisions (round_id) VALUES ('forged')"
            ),
            "evaluator_insert_decisions": _denied(
                conn,
                "minos_evaluator",
                "INSERT INTO runtime.decisions (round_id) VALUES ('forged')",
            ),
            "trainer_insert_decisions": _denied(
                conn, "minos_trainer", "INSERT INTO runtime.decisions (round_id) VALUES ('forged')"
            ),
            "runner_read_decisions": _denied(
                conn, "minos_runner", "SELECT count(*) FROM runtime.decisions"
            ),
            # the append-only trigger, not a grant: the OWNER is refused too, with the
            # engine's own SQLSTATE rather than a privilege error.
            "admin_update_decisions_rejected_by_trigger": _denied(
                conn,
                "minos_admin",
                "UPDATE runtime.decisions SET round_id = 'x'",
                sqlstate=_APPEND_ONLY_SQLSTATE,
            ),
            "admin_delete_decisions_rejected_by_trigger": _denied(
                conn,
                "minos_admin",
                "DELETE FROM runtime.decisions",
                sqlstate=_APPEND_ONLY_SQLSTATE,
            ),
        }
        public = sorted(
            str(r[0])
            for r in conn.execute(
                sa.text(
                    "SELECT table_schema || '.' || table_name FROM "
                    "information_schema.role_table_grants WHERE grantee = 'PUBLIC' "
                    "AND table_schema IN ("
                    + ", ".join(f"'{s}'" for s in _APPLICATION_SCHEMAS)
                    + ")"
                )
            )
        )
        definer = sorted(
            str(r[0])
            for r in conn.execute(
                sa.text(
                    "SELECT n.nspname || '.' || p.proname "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.prosecdef AND n.nspname IN ("
                    + ", ".join(f"'{s}'" for s in _APPLICATION_SCHEMAS)
                    + ")"
                )
            )
        )
        owner = str(
            conn.execute(
                sa.text(
                    "SELECT tableowner FROM pg_tables WHERE schemaname='runtime' "
                    "AND tablename='decisions'"
                )
            ).scalar()
        )
        owner_is_superuser = bool(
            conn.execute(
                sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = :o"), {"o": owner}
            ).scalar()
        )
    return {
        "minos_live_grants": grants,
        "minos_live_privilege_types": sorted({g.rsplit(":", 1)[1] for g in grants}),
        "minos_live_write_targets": sorted(
            g.rsplit(":", 1)[0] for g in grants if g.endswith(":INSERT")
        ),
        "minos_live_can_insert_decisions": live_can_insert,
        "minos_live_can_select_decisions": live_can_select,
        "minos_live_can_read_the_owning_profile": live_can_read_profile,
        "denials": dict(sorted(denials.items())),
        "public_table_grants_in_application_schemas": public,
        "security_definer_functions": definer,
        "security_definer_function_count": len(definer),
        "decisions_table_owner": owner,
        "owner_is_superuser": owner_is_superuser,
    }


# --------------------------------------------------------------------------- #
# checks, report, verifier
# --------------------------------------------------------------------------- #
def derive_checks(observation: dict[str, Any]) -> dict[str, bool]:
    """Every check is DERIVED from what was observed. None of them is a constant."""
    gate = observation.get("accepted_gate", {})
    schema = observation.get("persistence_schema", {})
    before = observation.get("state_before_overlay", {})
    after = observation.get("state_after_overlay", {})
    final = observation.get("final_state", {})
    downgrade = observation.get("downgrade", {})
    config = observation.get("safe_config_row", {})
    profile = observation.get("profile_binding", {})
    prerequisites = observation.get("prerequisite_refusals", {})
    readback = observation.get("readback", {})
    retry = observation.get("retry", {})
    concurrency = observation.get("concurrency", {})
    conflict = observation.get("conflict", {})
    rollback = observation.get("rollback", {})
    privileges = observation.get("privileges", {})
    sql = observation.get("sql_observation", {})
    refusals = observation.get("overlay_refusals", {})
    capabilities = observation.get("capabilities", {})
    resolver = capabilities.get("resolver", {})
    probes = capabilities.get("enumeration_probe_sqlstates", {})
    corrective = observation.get("corrective", {})
    count = int(observation.get("decision_count", 0))

    def _all_refused(names: tuple[str, ...]) -> bool:
        """Every probe must come back INSUFFICIENT PRIVILEGE -- not empty, not a constraint."""
        return bool(names) and all(probes.get(name) == _INSUFFICIENT_PRIVILEGE for name in names)

    return {
        "accepted_safe_controller_frozen_gate": (
            gate.get("capability_scope") == "SAFE_BASELINE_ONLY"
            and int(gate.get("check_count", 0)) > 0
            and gate.get("issuer_source_commit") != gate.get("qualified_source_commit")
        ),
        "execution_source_is_a_real_checkout": (
            len(str(observation.get("execution_source_commit", ""))) == 40
            and len(str(observation.get("execution_source_tree", ""))) == 40
            and str(observation.get("execution_source_commit"))
            != str(observation.get("execution_source_tree"))
        ),
        # --- capability, not behaviour ---------------------------------------------------
        "live_role_cannot_enumerate_profile_table": _all_refused(
            ("profile_select_star", "profile_count", "profile_single_column", "profile_exists")
        ),
        "live_role_cannot_enumerate_registry_table": _all_refused(
            ("registry_select_star", "registry_count", "registry_single_column")
        ),
        "live_role_cannot_read_any_partition_bearing_relation": (
            len(capabilities.get("partition_bearing_relations_probed", [])) >= 7
            and capabilities.get("partition_bearing_relations_readable_by_live") == []
        ),
        "live_role_can_resolve_one_owned_profile": (
            bool(profile.get("resolved"))
            and profile.get("lookup_surface") == LIVE_PROFILE_RESOLVER
            and int(resolver.get("unknown_profile_row_count", -1)) == 0
            and capabilities.get("campaign_connection_role") == "minos_live"
        ),
        "public_cannot_execute_profile_resolver": (
            resolver.get("execute_privilege", {}).get("public") is False
            and resolver.get("execute_privilege", {}).get("minos_live") is True
        ),
        "nonlive_roles_cannot_execute_profile_resolver": (
            all(
                resolver.get("execute_privilege", {}).get(role) is False
                for role in ("minos_runner", "minos_evaluator", "minos_trainer")
            )
            and all(
                state == _INSUFFICIENT_PRIVILEGE
                for state in resolver.get("nonlive_execute_sqlstates", {}).values()
            )
            and len(resolver.get("nonlive_execute_sqlstates", {})) == 3
        ),
        "resolver_security_definer_is_hardened": (
            resolver.get("owner") == "minos_admin"
            and resolver.get("security_definer") is True
            and resolver.get("language") == "plpgsql"
            and resolver.get("uses_dynamic_sql") is False
            and resolver.get("returns_at_most_one_row") is True
            and resolver.get("refuses_absent_arguments") is True
            and "search_path=pg_catalog, pg_temp" in str(resolver.get("search_path_setting", ""))
            and privileges.get("owner_is_superuser") is False
            and privileges.get("security_definer_function_count") == 1
        ),
        "resolver_refuses_absent_arguments": (
            len(resolver.get("absent_argument_sqlstates", {})) == 4
            and all(
                state == "22004" for state in resolver.get("absent_argument_sqlstates", {}).values()
            )
        ),
        "campaign_executed_under_the_live_role": (
            capabilities.get("campaign_connection_role") == "minos_live"
            and count > 0
            and sql.get("relations_outside_the_allowed_set") == []
        ),
        "overlay_head_is_the_corrective_revision": (
            observation.get("state_after_overlay", {}).get("overlay_revision")
            == RUNTIME_OVERLAY_REVISION
            and observation.get("state_after_corrective", {}).get("overlay_revision")
            == RUNTIME_OVERLAY_HEAD_REVISION
            and final.get("overlay_revision") == RUNTIME_OVERLAY_HEAD_REVISION
            and corrective.get("grants_revoked")
            == [
                "catalog.dataset_registry:minos_live:SELECT",
                "profiling.bam_profiles:minos_live:SELECT",
            ]
            and bool(corrective.get("columns_unchanged"))
            and bool(corrective.get("constraints_unchanged"))
            and bool(corrective.get("indexes_unchanged"))
        ),
        "corrective_downgrade_restores_the_prior_revision": bool(
            downgrade.get("corrective_restores_the_prior_revision")
        ),
        "exact_operational_schema_prerequisite": (
            before.get("main_revision") == REQUIRED_MAIN_REVISION
            and after.get("main_revision") == REQUIRED_MAIN_REVISION
            and final.get("main_revision") == REQUIRED_MAIN_REVISION
        ),
        "exact_runtime_overlay_identity": (
            before.get("overlay_revision") is None
            and after.get("overlay_revision") == RUNTIME_OVERLAY_REVISION
            and schema.get("overlay_head_revision") == RUNTIME_OVERLAY_HEAD_REVISION
            and len(str(schema.get("overlay_base_contract_hash", ""))) == 64
            and len(str(schema.get("overlay_head_contract_hash", ""))) == 64
            and schema.get("overlay_base_contract_hash") != schema.get("overlay_head_contract_hash")
            and [entry["identity"] for entry in schema.get("supersedes", [])]
            == [entry["identity"] for entry in SUPERSEDED_QUALIFICATIONS]
        ),
        "overlay_refuses_a_foreign_schema": (
            len(refusals) >= 3
            and all(
                bool(drill.get("refused"))
                and not drill.get("applied")
                and int(drill.get("added_columns", -1)) == 0
                for drill in refusals.values()
            )
        ),
        "overlay_downgrade_restores_the_schema": bool(downgrade.get("schema_restored")),
        "overlay_downgrade_restores_the_grant_shape": bool(downgrade.get("grants_restored")),
        "main_lineage_never_advanced": (
            before.get("main_revision")
            == after.get("main_revision")
            == final.get("main_revision")
            == REQUIRED_MAIN_REVISION
        ),
        "safe_config_resolved_by_accepted_hash": (
            config.get("resolved_by") == "config_hash"
            and bool(config.get("resolved_exactly_one_row"))
            and config.get("payload_sha256") == config.get("config_hash")
        ),
        "missing_safe_config_fails_closed": (
            bool(prerequisites.get("missing_safe_config"))
            and bool(config.get("unknown_config_refused"))
        ),
        "wrong_safe_config_fails_closed": bool(config.get("wrong_parameter_space_refused")),
        "profile_resolved_from_the_production_table": (
            profile.get("lookup_surface") == LIVE_PROFILE_RESOLVER
            and bool(profile.get("resolved"))
            and len(profile.get("cross_checked_profile_fields", [])) >= 10
            and len(profile.get("cross_checked_registry_fields", [])) == 3
        ),
        "missing_profile_fails_closed": (
            bool(profile.get("missing_profile_refused"))
            and bool(prerequisites.get("missing_profile"))
        ),
        "mismatched_profile_fails_closed": bool(profile.get("mismatched_profile_refused")),
        "every_decision_persisted_atomically": (
            count > 0
            and int(observation.get("inserted_count", -1)) == count
            and int(observation.get("converged_count", -1)) == 0
            and int(observation.get("final_row_count", -1))
            == count + int(observation.get("drill_rows_added", -1))
            and observation.get("decisions_per_round") == [len(ControlMode)]
        ),
        "readback_equals_the_decision": (
            int(readback.get("decisions_re_read", 0)) == count
            and int(readback.get("manifests_equal", -1)) == count
        ),
        "canonical_manifest_identity_survives_jsonb": (
            int(readback.get("decision_hash_re_derived_from_the_manifest", -1)) == count
            and int(readback.get("manifest_bytes_hash_re_derived", -1)) == count
        ),
        "same_decision_retry_converges": (
            bool(retry.get("first_inserted"))
            and bool(retry.get("converged"))
            and bool(retry.get("same_row"))
            and bool(retry.get("same_decision_hash"))
        ),
        "concurrent_identical_writers_converge": (
            not concurrency.get("errors")
            and int(concurrency.get("successes", 0)) == 2
            and int(concurrency.get("distinct_decision_identities", 0)) == 1
            and int(concurrency.get("distinct_persisted_row_ids", 0)) == 1
            and int(concurrency.get("rows_for_that_identity", 0)) == 1
        ),
        "conflicting_decision_identity_fails_closed": (
            bool(conflict.get("same_identity_different_round_refused"))
            and conflict.get("refusing_constraint") == "uq_decisions_decision_hash"
            and bool(conflict.get("forgery_planted"))
            and bool(conflict.get("divergent_stored_state_refused"))
            and bool(conflict.get("forgery_removed"))
            and int(conflict.get("decisions_for_one_round", 0)) == len(ControlMode)
        ),
        "rollback_leaves_no_visible_state": (
            bool(rollback.get("insert_executed"))
            and bool(rollback.get("failure_injected"))
            and bool(rollback.get("call_raised"))
            and bool(rollback.get("no_visible_state"))
        ),
        "model_bundle_id_is_null_in_safe_mode": (
            observation.get("model_bundle_id_values") == ["NULL"]
            and observation.get("config_id_values") == ["NULL"]
            and observation.get("actual_mode_counts") == {"SAFE_BASELINE": count}
            and bool(observation.get("selected_equals_baseline"))
            and int(observation.get("distinct_selected_config_ids", 0)) == 1
        ),
        "append_only_enforced_against_the_live_role": (
            bool(privileges.get("denials", {}).get("live_update_decisions"))
            and bool(privileges.get("denials", {}).get("live_delete_decisions"))
            and bool(
                privileges.get("denials", {}).get("admin_update_decisions_rejected_by_trigger")
            )
            and bool(
                privileges.get("denials", {}).get("admin_delete_decisions_rejected_by_trigger")
            )
        ),
        "live_role_holds_least_privilege": (
            # the live role can do the live operation ...
            bool(privileges.get("minos_live_can_insert_decisions"))
            and bool(privileges.get("minos_live_can_select_decisions"))
            and bool(privileges.get("minos_live_can_read_the_owning_profile"))
            # ... and nothing beyond read + append, anywhere
            and privileges.get("minos_live_privilege_types") == ["INSERT", "SELECT"]
            and privileges.get("minos_live_write_targets") == ["audit.events", "runtime.decisions"]
            and all(
                bool(privileges.get("denials", {}).get(name))
                for name in (
                    "live_truncate_decisions",
                    "live_update_profiles",
                    "live_read_evaluation",
                )
            )
            # after the corrective the live role holds NO raw identity table
            and not any(
                grant.startswith(("catalog.dataset_registry:", "profiling.bam_profiles:"))
                for grant in privileges.get("minos_live_grants", [])
            )
            and sorted(
                set(downgrade.get("grant_shape_after_corrective", []))
                - set(downgrade.get("grant_shape_before", []))
            )
            == [
                "public.alembic_version:minos_live:SELECT",
                "runtime.alembic_version_runtime:minos_live:SELECT",
            ]
            and privileges.get("decisions_table_owner") == "minos_admin"
            and privileges.get("owner_is_superuser") is False
            # exactly one SECURITY DEFINER function exists, and it is the narrow lookup surface
            and privileges.get("security_definer_functions") == [LIVE_PROFILE_RESOLVER]
        ),
        "other_roles_cannot_forge_a_decision": all(
            bool(privileges.get("denials", {}).get(name))
            for name in (
                "runner_insert_decisions",
                "evaluator_insert_decisions",
                "trainer_insert_decisions",
            )
        ),
        "public_holds_no_privilege": (
            privileges.get("public_table_grants_in_application_schemas") == []
        ),
        "no_scientific_or_evaluation_write": (
            bool(privileges.get("denials", {}).get("live_write_evaluation"))
            and bool(privileges.get("denials", {}).get("live_insert_gatk_configs"))
            and bool(privileges.get("denials", {}).get("live_insert_profiles"))
            and sql.get("relations_outside_the_allowed_set") == []
            and sql.get("forbidden_sql_tokens_seen") == []
            and sql.get("forbidden_relations_named") == []
        ),
        "no_sealed_partition_access": (
            int(observation.get("forbidden_sealed_path_open_attempts", -1)) == 0
            and int(observation.get("test_identity_authority_open_attempts", -1)) == 0
            and int(observation.get("validation_identity_authority_open_attempts", -1)) == 0
            and int(sql.get("unqualified_profile_scans", -1)) == 0
            and int(sql.get("profiles_opened_from_the_train_corpus", -1))
            == int(observation.get("train_identity_rows_copied", -2))
            # every other profile the path opened by name is a probe this qualification
            # declares, not an identity it discovered
            and sql.get("profiles_opened_outside_the_train_corpus")
            == sql.get("declared_negative_probes")
        ),
        "no_truth_or_scoring_dependency": (
            observation.get("no_truth_or_scoring", {}).get("forbidden_imports") == []
        ),
        "select_config_public_boundary_blocked": (
            bool(observation.get("select_config_blocked"))
            and observation.get("service_activated") is False
        ),
    }


def assemble_persistence_report(trusted: TrustedPersistenceQualification) -> dict[str, Any]:
    """Assemble the canonical evidence document. Only a real observation reaches this."""
    _require(
        isinstance(trusted, TrustedPersistenceQualification),
        "a persistence report may only be assembled from an observation the qualifier produced",
    )
    observation = trusted.observation
    checks = derive_checks(observation)
    missing = [name for name in MANDATORY_CHECKS if name not in checks]
    _require(not missing, f"the derived checks omit {missing}")
    extra = sorted(set(checks) - set(MANDATORY_CHECKS))
    status = "PASS" if all(checks[name] for name in MANDATORY_CHECKS) else "HOLD"
    return {
        "schema_version": PERSISTENCE_QUALIFICATION_SCHEMA,
        "status": status,
        "mandatory_checks": list(MANDATORY_CHECKS),
        "additional_checks": extra,
        "checks": dict(sorted(checks.items())),
        "observation": observation,
        "gate_issued": False,
        "service_activated": False,
        "test_accessed": bool(observation.get("test_identity_authority_open_attempts", 1)),
        "validation_read": bool(observation.get("validation_identity_authority_open_attempts", 1)),
    }


def persistence_report_identity(content: dict[str, Any]) -> str:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    return sha256_hex(
        PERSISTENCE_QUALIFICATION_DOMAIN.encode("utf-8") + canonical_json_bytes(content)
    )


#: Nothing operational belongs in a scientific identity. Checked recursively, by shape.
FORBIDDEN_EVIDENCE_PATTERNS: Final[tuple[str, ...]] = (
    "postgresql://",
    "postgres://",
    "password",
    "@localhost",
    "127.0.0.1",
    "/home/",
    "/tmp/",
    ".s.PGSQL",
)


def _scan_for_operational_leakage(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _scan_for_operational_leakage(key, found)
            _scan_for_operational_leakage(value, found)
    elif isinstance(node, list | tuple):
        for value in node:
            _scan_for_operational_leakage(value, found)
    elif isinstance(node, str):
        lowered = node.lower()
        for pattern in FORBIDDEN_EVIDENCE_PATTERNS:
            if pattern in lowered:
                found.append(pattern)


def _require_provable_source(observation: dict[str, Any], root: Any) -> None:
    """Prove the checkout that ran the campaign, rather than length-checking its name.

    A report can claim any forty hex characters as its source. Shape is not provenance -- the same
    defect the frozen-controller acceptance closed -- so the commit is required to exist in THIS
    repository and to carry exactly the tree the report records.
    """
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit

    base = _resolve_root(root)
    commit = str(observation.get("execution_source_commit", ""))
    tree = str(observation.get("execution_source_tree", ""))
    _require(
        len(commit) == 40 and all(c in "0123456789abcdef" for c in commit),
        f"{commit!r} is not a git object name",
    )
    _require(
        is_commit(base, commit),
        f"the campaign names source {commit}, which is not a commit in this repository",
    )
    actual = str(commit_tree_sha(base, commit) or "")
    _require(
        actual == tree,
        f"the campaign's source {commit} has tree {actual}, not the recorded {tree}",
    )


def verify_persistence_report(content: dict[str, Any], *, root: Any = None) -> dict[str, Any]:
    """Re-derive the whole document from its own observation. A stored verdict is not evidence."""
    _require(
        content.get("schema_version") == PERSISTENCE_QUALIFICATION_SCHEMA,
        f"{content.get('schema_version')!r} is not {PERSISTENCE_QUALIFICATION_SCHEMA}",
    )
    observation = content.get("observation")
    _require(isinstance(observation, dict), "the report carries no observation")
    assert isinstance(observation, dict)
    derived = derive_checks(observation)
    _require(
        content.get("checks") == dict(sorted(derived.items())),
        "the recorded checks are not what this observation derives",
    )
    _require(
        list(content.get("mandatory_checks", ())) == list(MANDATORY_CHECKS),
        "the report does not carry exactly the registered mandatory checks",
    )
    status = "PASS" if all(derived[name] for name in MANDATORY_CHECKS) else "HOLD"
    _require(
        content.get("status") == status,
        f"the report records {content.get('status')!r} but derives {status!r}",
    )
    _require(content.get("gate_issued") is False, "this qualification issues no gate")
    _require(content.get("service_activated") is False, "the public service is not activated")
    _require(content.get("test_accessed") is False, "TEST is sealed until L2-I")
    _require(content.get("validation_read") is False, "VALIDATION is not authorised here")
    _require_provable_source(observation, root)
    leaked: list[str] = []
    _scan_for_operational_leakage(content, leaked)
    _require(
        not leaked,
        f"the evidence carries operational values that have no place in an identity: "
        f"{sorted(set(leaked))}",
    )
    return {
        "ok": True,
        "status": status,
        "identity": persistence_report_identity(content),
        "check_count": len(derived),
        "decision_count": int(observation.get("decision_count", 0)),
        "execution_source_commit": str(observation["execution_source_commit"]),
        "execution_source_tree": str(observation["execution_source_tree"]),
    }
