"""r0002 — take raw identity-table enumeration away from the live role.

Revision ID: r0002_l2h_live_profile_lookup
Revises: r0001_l2h_runtime_decisions

THE DEFECT THIS CLOSES
----------------------
``r0001`` granted ``minos_live`` SELECT on ``profiling.bam_profiles`` and
``catalog.dataset_registry`` because the write path needs to resolve the owning profile. Those
are the raw identity tables: between them they carry the profile and dataset identities of every
partition, TEST included. The L2-D architecture deliberately never grants an application role a
raw identity table -- it exposes partition-scoped views instead, and
``evaluation.sealed_test_profile_members`` is granted to no application role at all. ``r0001``
walked around that.

The persistence qualification then certified the grant as least privilege on the strength of a
SQL recorder showing the code only ever queried by ``profile_id``. That is a statement about
behaviour, and least privilege is a statement about capability: with those grants
``SET ROLE minos_live; SELECT * FROM profiling.bam_profiles;`` returns all seventy-five identities,
whatever the code happens to do.

WHAT REPLACES IT
----------------
Both grants are revoked, and the one thing the live path actually needs -- resolve ONE profile
whose identity has already been proven upstream -- is exposed as a narrow SECURITY DEFINER
function owned by ``minos_admin``:

* two mandatory scalar arguments, matched by equality on a UNIQUE column and its owning round;
* no dynamic SQL, no caller-supplied predicate, no list, no count, no wildcard;
* NULL or empty arguments are refused rather than treated as "match anything";
* at most one row comes back, and more than one raises rather than returns;
* fixed ``search_path``, every relation schema-qualified;
* ``PUBLIC`` EXECUTE revoked, EXECUTE granted to ``minos_live`` alone.

It is an exact-lookup surface, not an authorization surface: it does not decide whether a round
may be decided for. That remains the round/profile ownership authority's job, upstream and in
code, which is why the function is deliberately not partition-scoped -- scoping it to ``train``
would bake a research partition into the live production path.

THE TWO VERSION TABLES
----------------------
The live path also has to refuse a store whose schema is not the accepted one, and that means
reading ``public.alembic_version`` and ``runtime.alembic_version_runtime``. ``minos_live`` could
not read either, so under a real least-privilege connection the write path could never have run
-- a second thing the superuser-run v1 campaign could not see. They are granted here, and they
are different in kind from the tables above: each holds a single revision string, no identity, no
partition, nothing sealed. The grants are issued as the migration runner, which owns them.

The MAIN Alembic lineage is untouched, and this revision still requires it to be exactly
``0005_l2e_feature_view``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "r0002_l2h_live_profile_lookup"
down_revision: str | None = "r0001_l2h_runtime_decisions"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

REQUIRED_MAIN_REVISION = "0005_l2e_feature_view"

#: The grants r0001 introduced and this revision takes back. Restored exactly on downgrade.
REVOKED_IDENTITY_TABLE_GRANTS: tuple[str, ...] = (
    "profiling.bam_profiles",
    "catalog.dataset_registry",
)

#: A single revision string each: no identity, no partition, nothing sealed.
VERSION_TABLES: tuple[str, ...] = ("public.alembic_version", "runtime.alembic_version_runtime")

RESOLVER_SCHEMA = "runtime"
RESOLVER_NAME = "l2h_resolve_owned_profile"
RESOLVER_SIGNATURE = f"{RESOLVER_SCHEMA}.{RESOLVER_NAME}(text, text)"
RESOLVER_SEARCH_PATH = "pg_catalog, pg_temp"

#: Exactly the fields ``resolve_owned_profile_row`` cross-checks, plus the row id the decision
#: foreign key needs. ``profile_document`` is deliberately absent: the write path never reads it.
RESOLVER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("profile_row_id", "uuid"),
    ("attestation_hash", "character(64)"),
    ("bai_sha256", "character(64)"),
    ("bam_sha256", "character(64)"),
    ("fai_sha256", "character(64)"),
    ("identity_tuple_hash", "character(64)"),
    ("profile_manifest_sha256", "character(64)"),
    ("profile_sha256", "character(64)"),
    ("reference_sha256", "character(64)"),
    ("region_hash", "character(64)"),
    ("registry_snapshot_hash", "character(64)"),
    ("integrity_degraded", "boolean"),
    ("dataset_id", "text"),
    ("round_id", "text"),
    ("chromosome", "text"),
)

_RESOLVER_BODY = f"""
CREATE FUNCTION {RESOLVER_SCHEMA}.{RESOLVER_NAME}(p_profile_id text, p_round_id text)
RETURNS TABLE ({", ".join(f"{name} {kind}" for name, kind in RESOLVER_COLUMNS)})
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = {RESOLVER_SEARCH_PATH}
AS $body$
DECLARE
    matched integer;
BEGIN
    IF p_profile_id IS NULL OR length(p_profile_id) = 0
       OR p_round_id IS NULL OR length(p_round_id) = 0 THEN
        RAISE EXCEPTION
            'a profile is resolved by an exact profile id and round id, never by absence'
            USING ERRCODE = 'null_value_not_allowed';
    END IF;
    RETURN QUERY
        SELECT p.id,
               p.attestation_hash, p.bai_sha256, p.bam_sha256, p.fai_sha256,
               p.identity_tuple_hash, p.profile_manifest_sha256, p.profile_sha256,
               p.reference_sha256, p.region_hash, p.registry_snapshot_hash,
               p.integrity_degraded,
               r.dataset_id, r.round_id, r.chromosome
        FROM profiling.bam_profiles AS p
        JOIN catalog.dataset_registry AS r ON r.id = p.dataset_registry_id
        WHERE p.profile_id = p_profile_id
          AND r.round_id = p_round_id;
    GET DIAGNOSTICS matched = ROW_COUNT;
    IF matched > 1 THEN
        RAISE EXCEPTION 'profile id resolves to % rows; one identity names one profile', matched
            USING ERRCODE = 'cardinality_violation';
    END IF;
END;
$body$;
"""


def _require_prerequisites() -> None:
    conn = op.get_bind()
    main = conn.execute(
        sa.text("SELECT array_agg(version_num ORDER BY version_num) FROM public.alembic_version")
    ).scalar()
    revisions = tuple(main or ())
    if revisions != (REQUIRED_MAIN_REVISION,):
        raise RuntimeError(
            "the operational runtime overlay requires the main schema to be exactly "
            f"{REQUIRED_MAIN_REVISION}; this database is at {revisions or '(no main lineage)'}"
        )
    for table in ("profiling.bam_profiles", "catalog.dataset_registry"):
        if conn.execute(sa.text(f"SELECT to_regclass('{table}')")).scalar() is None:
            raise RuntimeError(f"{table} does not exist in this database")


def upgrade() -> None:
    _require_prerequisites()
    # as the migration runner, which owns the version tables it created
    for table in VERSION_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO minos_live;")
    op.execute("SET ROLE minos_admin")
    for table in REVOKED_IDENTITY_TABLE_GRANTS:
        op.execute(f"REVOKE SELECT ON {table} FROM minos_live;")
    op.execute(_RESOLVER_BODY)
    op.execute(f"REVOKE ALL ON FUNCTION {RESOLVER_SIGNATURE} FROM PUBLIC;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {RESOLVER_SIGNATURE} TO minos_live;")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    op.execute(f"DROP FUNCTION IF EXISTS {RESOLVER_SIGNATURE};")
    for table in REVOKED_IDENTITY_TABLE_GRANTS:
        op.execute(f"GRANT SELECT ON {table} TO minos_live;")
    op.execute("RESET ROLE")
    for table in VERSION_TABLES:
        op.execute(f"REVOKE SELECT ON {table} FROM minos_live;")
