"""Carry the exact ten VALIDATION lineages from the frozen operational store into a new target.

A validation database migrated to ``0024`` has schema and nothing else. Preparation resolves its
ten members from ``catalog``/``profiling`` rows that a fresh store does not contain, and the closed
TRAIN campaign store cannot supply them — it was built for the fifty TRAIN members and holds no
validation allocation at all. The authoritative validation lineage lives in the operational store
qualified by ``PROFILE-SNAPSHOT-FROZEN-1``.

So this module reads that store, READ ONLY, and writes the minimum closure a Phase-D plan needs:

    one split snapshot, one profile snapshot, and — for each of the ten frozen members — its
    dataset registry row, its split allocation, its BAM profile, its snapshot membership, and
    the catalog identity of the three artifacts that profile references.

Seventy-two rows. Not a clone of the operational database, and not a table dump: the query is
driven by ``build_validation_schedule()``, so the fifty TRAIN and fifteen TEST members are not
selected, not read and not transferable through this interface.

Two identities of one split
---------------------------
The frozen profile snapshot records ``split_manifest_hash`` ``b23cd571…``, which is
``catalog.split_snapshots.manifest_hash`` — the database's own canonical identity for the epoch-1
split. ``build_validation_schedule()`` reads the committed manifest FILE, whose sha256 is
``ffdd3195…``. These are two identities of the same split, not a conflict, and neither is
substituted for the other: the snapshot lineage keeps ``b23cd571…`` because that is what it is,
the Phase-D binding keeps ``ffdd3195…`` because that is what preparation verifies against the
manifest bytes, and this module proves the ten members agree field for field across both.

A projection, honestly labelled
-------------------------------
The transferred ``profile_snapshots`` row keeps ``member_count = 75``. It is a seventy-five-member
snapshot; that number is part of the identity hashing to ``cf717ebb…``. Writing ``10`` while
keeping that hash would be a lie about which snapshot this is, and inventing a new hash would be a
lie about the science. What the target holds is the frozen snapshot's identity plus a
VALIDATION-ONLY PROJECTION of its membership, and the same reasoning keeps the split snapshot's
``sample_count``/``count_train``/``count_test`` exact. A reader of the target must understand it
that way: seventy-five is what the snapshot is, ten is what this store carries.

Truth is not involved. No truth path, digest, file or table is read here; provisioning concerns
split, dataset, profile and artifact lineage only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.validation_members import VALIDATION_COUNT, build_validation_schedule
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "ACCEPTED_REGISTRY_SNAPSHOT_HASH",
    "ACCEPTED_SNAPSHOT_HASH",
    "OPERATIONAL_DATABASE_NAME",
    "OPERATIONAL_REVISION",
    "ValidationProvisionError",
    "ValidationProvisionResult",
    "provision_l2f2_validation_upstream",
]

#: THE verified operational lineage authority. Not parameters of the public boundary.
OPERATIONAL_DATABASE_NAME = "minos_engine_db"
OPERATIONAL_REVISION = "0005_l2e_feature_view"

#: the frozen profile-snapshot identity, from PROFILE-SNAPSHOT-FROZEN-1.
ACCEPTED_SNAPSHOT_HASH = "cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7"
ACCEPTED_REGISTRY_SNAPSHOT_HASH = "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
ACCEPTED_SNAPSHOT_MEMBER_COUNT = 75

_VALIDATION = "validation"


class ValidationProvisionError(MinosEngineError):
    """The validation upstream lineage cannot be provisioned as the frozen protocol requires."""


@dataclass(frozen=True, slots=True)
class ValidationProvisionResult:
    """Row counts and the identities that were established. No digests of anything private."""

    source_database: str
    source_revision: str
    snapshot_hash: str
    registry_snapshot_hash: str
    member_count: int
    created_rows: dict[str, int]
    existing_rows: dict[str, int]


# ------------------------------------------------------------------------------------------- #
# what a row IS, for transfer and for conflict detection
# ------------------------------------------------------------------------------------------- #
#: ``created_at`` is deliberately excluded everywhere: when a row was written into a particular
#: store is provenance, not scientific identity, and requiring it to match would make an
#: otherwise-exact replay look like a conflict.
_IMMUTABLE: dict[str, tuple[str, ...]] = {
    "catalog.split_snapshots": (
        "id",
        "epoch",
        "salt",
        "split_policy_version",
        "policy_hash",
        "manifest_hash",
        "registry_snapshot_hash",
        "ancestor_v1_dataset_registry_hash",
        "parent_registry_snapshot_hash",
        "parent_manifest_hash",
        "parent_snapshot_id",
        "parent_epoch",
        "transition_count",
        "sample_count",
        "count_train",
        "count_validation",
        "count_test",
    ),
    "catalog.dataset_registry": (
        "id",
        "dataset_id",
        "round_id",
        "chromosome",
        "region_source",
        "region_start0",
        "region_end0_exclusive",
        "region_length_bp",
        "region_coordinate_system",
        "region_hash",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "bam_size_bytes",
        "parameter_space_hash",
        "feature_registry_hash",
        "identity_tuple_hash",
        "manifest_hash",
        "split_algorithm_version",
        "split_salt",
        "allocation_digest",
    ),
    "catalog.split_allocations": (
        "id",
        "dataset_registry_id",
        "partition",
        "sort_order",
        "manifest_hash",
    ),
    "catalog.artifacts": ("id", "uri", "sha256", "media_type", "size_bytes", "provenance"),
    "profiling.bam_profiles": (
        "id",
        "dataset_registry_id",
        "profile_id",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "region_hash",
        "identity_tuple_hash",
        "m5_status",
        "integrity_degraded",
        "attestation_hash",
        "registry_snapshot_hash",
        "profile_status",
        "profiler_version",
        "profiler_config_hash",
        "windows_row_count",
        "feature_values_hash",
        "l1_feature_values_hash",
        "eligible_value_count",
        "profile_document",
        "profile_sha256",
        "profile_manifest_sha256",
        "windows_sha256",
        "profile_artifact_id",
        "profile_manifest_artifact_id",
        "windows_artifact_id",
        "ingestion_key",
        "content_hash",
    ),
    "profiling.profile_snapshots": (
        "id",
        "epoch",
        "split_snapshot_id",
        "split_manifest_hash",
        "registry_snapshot_hash",
        "member_count",
        "snapshot_hash",
    ),
    "profiling.profile_snapshot_members": (
        "id",
        "profile_snapshot_id",
        "bam_profile_id",
        "dataset_registry_id",
        "partition",
        "feature_values_hash",
    ),
}

#: written parent-first, so every FK is satisfiable at the moment its child is inserted.
_TRANSFER_ORDER: tuple[str, ...] = (
    "catalog.split_snapshots",
    "catalog.dataset_registry",
    "catalog.split_allocations",
    "catalog.artifacts",
    "profiling.bam_profiles",
    "profiling.profile_snapshots",
    "profiling.profile_snapshot_members",
)

#: ``profile_document`` is jsonb; psycopg returns a dict and needs an explicit cast on the way in.
_JSONB_COLUMNS = frozenset({"profile_document"})


def _columns(table: str) -> tuple[str, ...]:
    return _IMMUTABLE[table]


# ------------------------------------------------------------------------------------------- #
# authorization
# ------------------------------------------------------------------------------------------- #
def _require_store(conn: Connection, *, database: str, revision: str, role: str) -> None:
    from sqlalchemy import text

    live_db = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if live_db != database:
        raise ValidationProvisionError(
            f"the {role} connection is attached to {live_db!r}; L2-F2-F provisioning requires "
            f"{database!r}"
        )
    live_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live_rev != revision:
        raise ValidationProvisionError(
            f"the {role} database is at revision {live_rev!r}, expected {revision!r}"
        )


def _require_read_only(conn: Connection, *, role: str) -> None:
    """Read-only is asserted from the SERVER, not assumed from having issued the statement."""
    from sqlalchemy import text

    conn.execute(text("SET TRANSACTION READ ONLY"))
    if str(conn.execute(text("SHOW transaction_read_only")).scalar_one()) != "on":
        raise ValidationProvisionError(  # pragma: no cover - postgres would have raised first
            f"the {role} transaction is not READ ONLY"
        )


# ------------------------------------------------------------------------------------------- #
# the frozen snapshot, and the exact ten
# ------------------------------------------------------------------------------------------- #
def _verify_frozen_snapshot(conn: Connection, *, snapshot_hash: str) -> dict[str, Any]:
    """The operational store must hold the qualified snapshot, by identity — not by resemblance."""
    from sqlalchemy import text

    rows = (
        conn.execute(
            text(
                "SELECT id, epoch, split_snapshot_id, split_manifest_hash, "
                "       registry_snapshot_hash, member_count, snapshot_hash "
                "  FROM profiling.profile_snapshots WHERE snapshot_hash = :h"
            ),
            {"h": snapshot_hash},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise ValidationProvisionError(
            f"the operational store holds {len(rows)} profile snapshots with the frozen identity "
            f"{snapshot_hash}; exactly one is required"
        )
    snapshot = dict(rows[0])
    if str(snapshot["registry_snapshot_hash"]) != ACCEPTED_REGISTRY_SNAPSHOT_HASH:
        raise ValidationProvisionError(
            f"the frozen snapshot cites registry snapshot {snapshot['registry_snapshot_hash']}, "
            f"not the accepted {ACCEPTED_REGISTRY_SNAPSHOT_HASH}"
        )
    if int(snapshot["member_count"]) != ACCEPTED_SNAPSHOT_MEMBER_COUNT:
        raise ValidationProvisionError(
            f"the frozen snapshot declares {snapshot['member_count']} members, the qualified "
            f"snapshot has {ACCEPTED_SNAPSHOT_MEMBER_COUNT}"
        )
    return snapshot


def _resolve_members(conn: Connection, snapshot_id: str) -> tuple[dict[str, Any], ...]:
    """One authoritative lineage per frozen schedule member, compared field by field.

    Driven by the schedule, so TRAIN and TEST are never selected — this function has no query
    that could return them and no argument that could ask for them.
    """
    from sqlalchemy import text

    schedule = build_validation_schedule()
    if len(schedule.members) != VALIDATION_COUNT:  # pragma: no cover - the schedule guarantees it
        raise ValidationProvisionError("the frozen validation schedule is not ten members")

    resolved: list[dict[str, Any]] = []
    for member in schedule.members:
        rows = (
            conn.execute(
                text(
                    "SELECT dr.id AS dataset_registry_id, dr.dataset_id, dr.round_id, "
                    "       dr.chromosome, dr.identity_tuple_hash, "
                    "       sa.id AS allocation_id, sa.partition AS allocation_partition, "
                    "       bp.id AS bam_profile_id, bp.profile_id, bp.content_hash, "
                    "       bp.profile_status, bp.feature_values_hash AS profile_fvh, "
                    "       bp.identity_tuple_hash AS profile_identity_tuple_hash, "
                    "       bp.profile_artifact_id, bp.profile_manifest_artifact_id, "
                    "       bp.windows_artifact_id, "
                    "       psm.id AS member_id, psm.partition AS member_partition, "
                    "       psm.feature_values_hash AS member_fvh "
                    "  FROM catalog.dataset_registry dr "
                    "  JOIN catalog.split_allocations sa ON sa.dataset_registry_id = dr.id "
                    "  JOIN profiling.profile_snapshot_members psm "
                    "    ON psm.dataset_registry_id = dr.id "
                    "   AND psm.profile_snapshot_id = :snap "
                    "  JOIN profiling.bam_profiles bp ON bp.id = psm.bam_profile_id "
                    " WHERE dr.dataset_id = :d"
                ),
                {"d": member.dataset_id, "snap": snapshot_id},
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            raise ValidationProvisionError(
                f"validation member {member.dataset_id} resolves to {len(rows)} authoritative "
                "lineages in the operational store; exactly one is required"
            )
        row = dict(rows[0])

        differing = sorted(
            field
            for field, found, expected in (
                ("round_id", str(row["round_id"]), member.round_id),
                ("chromosome", str(row["chromosome"]), member.chromosome),
                (
                    "identity_tuple_hash",
                    str(row["identity_tuple_hash"]),
                    member.identity_tuple_hash,
                ),
            )
            if found != expected
        )
        if differing:
            raise ValidationProvisionError(
                f"operational member {member.dataset_id} disagrees with the frozen validation "
                f"schedule on {differing}"
            )
        for column, label in (
            ("allocation_partition", "the accepted split"),
            ("member_partition", "the frozen snapshot"),
        ):
            if str(row[column]) != _VALIDATION:
                raise ValidationProvisionError(
                    f"member {member.dataset_id} is {row[column]!r} according to {label}; "
                    "Phase D provisions the VALIDATION partition only"
                )
        if str(row["profile_status"]) != "COMPLETE":
            raise ValidationProvisionError(
                f"member {member.dataset_id} has a {row['profile_status']} BAM profile; "
                "validation requires a COMPLETE one"
            )
        # the registry row, the profile and the snapshot membership must be the SAME sample.
        if str(row["profile_identity_tuple_hash"]) != member.identity_tuple_hash:
            raise ValidationProvisionError(
                f"the BAM profile for {member.dataset_id} carries a different identity tuple "
                "than its registry row"
            )
        if str(row["member_fvh"]) != str(row["profile_fvh"]):
            raise ValidationProvisionError(
                f"the snapshot membership for {member.dataset_id} cites feature values "
                "that its BAM profile does not"
            )
        for column in ("profile_id", "content_hash", "member_fvh"):
            if not row[column]:
                raise ValidationProvisionError(
                    f"member {member.dataset_id} has no {column}; its profile identity is "
                    "incomplete"
                )
        row["member_index"] = member.member_index
        resolved.append(row)
    return tuple(resolved)


# ------------------------------------------------------------------------------------------- #
# reading the exact closure out of the source
# ------------------------------------------------------------------------------------------- #
def _read_rows(
    conn: Connection, table: str, *, key: str, values: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    from sqlalchemy import text

    if not values:  # pragma: no cover - every closure below is non-empty
        return ()
    columns = ", ".join(_columns(table))
    rows = (
        conn.execute(
            text(f"SELECT {columns} FROM {table} WHERE {key} = ANY(:v)"),  # noqa: S608
            {"v": sorted(set(values))},
        )
        .mappings()
        .all()
    )
    if len(rows) != len(set(values)):
        raise ValidationProvisionError(
            f"expected {len(set(values))} rows from {table}, the operational store returned "
            f"{len(rows)}"
        )
    return tuple(dict(r) for r in rows)


def _read_closure(
    conn: Connection, snapshot: dict[str, Any], members: tuple[dict[str, Any], ...]
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Exactly the rows a Phase-D plan needs, and no others."""
    registry_ids = tuple(str(m["dataset_registry_id"]) for m in members)
    artifact_ids = tuple(
        str(m[column])
        for m in members
        for column in (
            "profile_artifact_id",
            "profile_manifest_artifact_id",
            "windows_artifact_id",
        )
    )
    return {
        "catalog.split_snapshots": _read_rows(
            conn,
            "catalog.split_snapshots",
            key="id",
            values=(str(snapshot["split_snapshot_id"]),),
        ),
        "catalog.dataset_registry": _read_rows(
            conn, "catalog.dataset_registry", key="id", values=registry_ids
        ),
        "catalog.split_allocations": _read_rows(
            conn, "catalog.split_allocations", key="dataset_registry_id", values=registry_ids
        ),
        "catalog.artifacts": _read_rows(conn, "catalog.artifacts", key="id", values=artifact_ids),
        "profiling.bam_profiles": _read_rows(
            conn,
            "profiling.bam_profiles",
            key="id",
            values=tuple(str(m["bam_profile_id"]) for m in members),
        ),
        "profiling.profile_snapshots": _read_rows(
            conn, "profiling.profile_snapshots", key="id", values=(str(snapshot["id"]),)
        ),
        "profiling.profile_snapshot_members": _read_rows(
            conn,
            "profiling.profile_snapshot_members",
            key="id",
            values=tuple(str(m["member_id"]) for m in members),
        ),
    }


# ------------------------------------------------------------------------------------------- #
# writing it, once
# ------------------------------------------------------------------------------------------- #
def _persist(
    conn: Connection, closure: dict[str, tuple[dict[str, Any], ...]]
) -> tuple[dict[str, int], dict[str, int]]:
    """Insert what is absent, verify what is present, refuse what disagrees.

    Row-by-row idempotency is correct HERE, and deliberately different from the EMPTY-or-COMPLETE
    rule the job materializer enforces. The difference is what a partial graph means. A partial
    job graph is unexplained: jobs are created only by one atomic materialization, so a subset is
    evidence that something went wrong and completing it would hide that. Upstream catalog rows
    have an external authority — every one of them is re-read from the closed operational store
    and compared field by field before anything is written — so a row that is already present and
    exactly right is not a mystery, it is the same row. Completing such a graph adds no assertion
    that was not independently verified against the source.

    Any disagreement still fails closed. These tables are append-only; nothing here updates,
    deletes, or repairs.
    """
    from sqlalchemy import text

    created: dict[str, int] = {}
    existing: dict[str, int] = {}
    for table in _TRANSFER_ORDER:
        columns = _columns(table)
        created[table] = 0
        existing[table] = 0
        for row in closure[table]:
            found = (
                conn.execute(
                    text(f"SELECT {', '.join(columns)} FROM {table} WHERE id = :i"),  # noqa: S608
                    {"i": row["id"]},
                )
                .mappings()
                .one_or_none()
            )
            if found is not None:
                differing = sorted(
                    column for column in columns if _differs(found[column], row[column])
                )
                if differing:
                    raise ValidationProvisionError(
                        f"the target already holds a conflicting {table} row {row['id']}: "
                        f"{differing} disagree with the operational source. Upstream lineage is "
                        "append-only scientific identity and is never repaired"
                    )
                existing[table] += 1
                continue
            placeholders = ", ".join(
                f"CAST(:{c} AS jsonb)" if c in _JSONB_COLUMNS else f":{c}" for c in columns
            )
            conn.execute(
                text(
                    f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({placeholders})"
                ),
                {c: _bind(c, row[c]) for c in columns},
            )
            created[table] += 1
    return created, existing


def _bind(column: str, value: Any) -> Any:
    if column in _JSONB_COLUMNS and value is not None and not isinstance(value, str):
        import json

        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _differs(found: Any, expected: Any) -> bool:
    """Compare scientific content, not driver representation."""
    if isinstance(found, dict) or isinstance(expected, dict):
        import json

        def norm(v: Any) -> str:
            if isinstance(v, str):
                v = json.loads(v)
            return json.dumps(v, sort_keys=True, separators=(",", ":"))

        return norm(found) != norm(expected)
    return str(found) != str(expected)


# ------------------------------------------------------------------------------------------- #
# THE production boundary
# ------------------------------------------------------------------------------------------- #
def provision_l2f2_validation_upstream(
    *, source: Engine, target: Engine
) -> ValidationProvisionResult:
    """Provision a validation store's upstream lineage from the frozen operational store.

    Two engines. Nothing scientific crosses this boundary: which snapshot, which ten members,
    which order, which partition, which artifacts — all derived inside from the frozen validation
    schedule and the accepted ``PROFILE-SNAPSHOT-FROZEN-1`` identity.

    Prepares no plan, copies no CONFIG payload, registers no truth, materializes no job.
    """
    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    return _provision_with_trust(
        source=source,
        target=target,
        expected_source_database=OPERATIONAL_DATABASE_NAME,
        expected_source_revision=OPERATIONAL_REVISION,
        expected_target_database=VALIDATION_DATABASE_NAME,
        expected_target_revision=VALIDATION_REVISION,
    )


def _provision_with_trust(
    *,
    source: Engine,
    target: Engine,
    expected_source_database: str,
    expected_source_revision: str,
    expected_target_database: str,
    expected_target_revision: str,
    expected_snapshot_hash: str = ACCEPTED_SNAPSHOT_HASH,
) -> ValidationProvisionResult:
    """The provisioning core. Private; store identity is a parameter only here."""
    from sqlalchemy import text

    with source.connect() as conn:
        _require_store(
            conn,
            database=expected_source_database,
            revision=expected_source_revision,
            role="operational lineage source",
        )
        _require_read_only(conn, role="operational lineage source")
        snapshot = _verify_frozen_snapshot(conn, snapshot_hash=expected_snapshot_hash)
        members = _resolve_members(conn, str(snapshot["id"]))
        closure = _read_closure(conn, snapshot, members)

    with target.connect() as conn, conn.begin():
        _require_store(
            conn,
            database=expected_target_database,
            revision=expected_target_revision,
            role="validation target",
        )
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        created, existing = _persist(conn, closure)

    return ValidationProvisionResult(
        source_database=expected_source_database,
        source_revision=expected_source_revision,
        snapshot_hash=str(snapshot["snapshot_hash"]),
        registry_snapshot_hash=str(snapshot["registry_snapshot_hash"]),
        member_count=len(members),
        created_rows=created,
        existing_rows=existing,
    )
