"""Prepare the Phase-D validation campaign: two databases, one direction, zero jobs.

Activation has a shape no earlier phase had. The campaign runs in a SEPARATE validation store, but
the four configurations it must run were frozen inside the closed TRAIN baseline, and their
canonical payload bytes cannot be reconstructed from an empty database — they came out of a
Phase-B design that only exists in that ledger. So preparation reads the baseline as a strictly
read-only source and carries verified bytes forward.

The asymmetry is enforced, not merely described:

* the SOURCE is verified to be ``minos_l2f2_baseline`` at the closed revision ``0020``, and its
  transaction is put into ``READ ONLY`` before a single scientific row is touched. The completed
  500-observation ledger is evidence; this module reads it and cannot write to it;
* the TARGET is verified to be the validation store at the validation revision before any
  scientific access;
* every payload is re-hashed and RE-CANONICALIZED. The registered artifact digest for a CONFIG
  payload *is* its config hash, so a substituted or drifted payload cannot survive either check;
* nothing scientific is a parameter. Connections, artifact roots and the path of the frozen
  finalist artifact are provisioning. Which four, which ten, which order, which partition, which
  identities — all derived from frozen authorities inside.

Preparation stops at ZERO jobs, deliberately. Recording what a campaign IS and starting to spend
GATK hours on it are different decisions, so they are different authorizations. The materializer
is not in this module and is not called from it.

The last thing preparation does is ask the database's own argument-free bootstrap whether what it
just wrote is a campaign. If the bootstrap refuses, the transaction rolls back — preparation does
not leave behind a plan the runner would decline to execute.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.phase_d import PHASE_D_LOGICAL_JOB_BUDGET, PhaseDAuthority
from minos_engine.baseline.validation_plan import VALIDATION_PLAN_PARTITION
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "ValidationPrepareError",
    "ValidationPrepareResult",
    "prepare_l2f2_validation_plan",
]

#: the closed TRAIN source. Neither is a parameter.
_BASELINE_DATABASE = "minos_l2f2_baseline"
_BASELINE_REVISION = "0020_l2f2_phase_c_execution"

#: the frozen artifacts this campaign is. Not accepted from a caller.
ACCEPTED_FINALIST_FREEZE_SHA256 = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
ACCEPTED_PHASE_C_CLOSURE_SHA256 = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"

#: recorded on every artifact this boundary publishes into the validation store.
_PROVENANCE = "l2f2-phase-d-validation-prepare"


class ValidationPrepareError(MinosEngineError):
    """The validation campaign cannot be prepared as the frozen protocol requires."""


@dataclass(frozen=True, slots=True)
class ValidationPrepareResult:
    """What preparation durably established. ``job_count`` is part of the contract, and is zero."""

    plan_hash: str
    plan_id: str
    authority_id: str
    binding_id: str
    created_plan: bool
    created_members: int
    created_configs: int
    created_authority: bool
    created_binding: bool
    job_count: int
    bootstrap_plan_hash: str
    bootstrap_environment_hash: str


# ------------------------------------------------------------------------------------------- #
# connection authorization
# ------------------------------------------------------------------------------------------- #
def _require_database(conn: Connection, *, database: str, revision: str, role: str) -> None:
    from sqlalchemy import text

    live_db = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if live_db != database:
        raise ValidationPrepareError(
            f"the {role} connection is attached to {live_db!r}; L2-F2-F requires {database!r}"
        )
    live_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live_rev != revision:
        raise ValidationPrepareError(
            f"the {role} database is at revision {live_rev!r}, expected {revision!r}"
        )


# ------------------------------------------------------------------------------------------- #
# the four frozen CONFIG payloads, read from the closed baseline
# ------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _TransferredConfig:
    config_index: int
    config_hash: str
    parameter_space_hash: str
    schema_version: str
    media_type: str
    payload: bytes
    size_bytes: int
    inherited_candidate_index: int


def _read_frozen_configs(
    baseline: Engine, authority: PhaseDAuthority
) -> tuple[_TransferredConfig, ...]:
    """Resolve, verify and RE-CANONICALIZE the frozen payloads. The source is never written."""
    from sqlalchemy import text

    from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config

    out: list[_TransferredConfig] = []
    with baseline.connect() as conn:
        _require_database(
            conn, database=_BASELINE_DATABASE, revision=_BASELINE_REVISION, role="baseline source"
        )
        conn.execute(text("SET TRANSACTION READ ONLY"))
        for index, config_hash in enumerate(authority.ordered_config_hashes):
            rows = (
                conn.execute(
                    text(
                        "SELECT cp.config_hash, cp.parameter_space_hash, cp.schema_version, "
                        "       cp.media_type, a.uri, a.sha256, a.size_bytes "
                        "  FROM experiments.l2f_config_payloads cp "
                        "  JOIN catalog.artifacts a ON a.id = cp.artifact_id "
                        " WHERE cp.config_hash = :h"
                    ),
                    {"h": config_hash},
                )
                .mappings()
                .all()
            )
            if len(rows) != 1:
                raise ValidationPrepareError(
                    f"the closed baseline holds {len(rows)} payloads for frozen config "
                    f"{config_hash}; exactly one is required"
                )
            row = rows[0]
            if str(row["parameter_space_hash"]) != authority.parameter_space_hash:
                raise ValidationPrepareError(
                    f"frozen config {config_hash} binds parameter space "
                    f"{row['parameter_space_hash']}, not {authority.parameter_space_hash}"
                )
            payload = _read_artifact_bytes(str(row["uri"]))
            digest = hashlib.sha256(payload).hexdigest()
            # the registered artifact digest for a CONFIG payload IS its config hash
            if digest != str(row["sha256"]) or digest != config_hash:
                raise ValidationPrepareError(
                    f"the payload bytes for frozen config {config_hash} hash to {digest}; the "
                    "artifact has been tampered with or substituted"
                )
            if int(row["size_bytes"]) != len(payload):
                raise ValidationPrepareError(
                    f"the payload for frozen config {config_hash} is {len(payload)} bytes, the "
                    f"baseline registered {row['size_bytes']}"
                )
            recanonical = canonicalize_live_gatk_config(_effective_config(payload, config_hash))
            if recanonical.config_hash != config_hash:
                raise ValidationPrepareError(
                    f"frozen config {config_hash} does not recanonicalize to itself "
                    f"(got {recanonical.config_hash}); its payload has drifted"
                )
            out.append(
                _TransferredConfig(
                    config_index=index,
                    config_hash=config_hash,
                    parameter_space_hash=str(row["parameter_space_hash"]),
                    schema_version=str(row["schema_version"]),
                    media_type=str(row["media_type"]),
                    payload=payload,
                    size_bytes=len(payload),
                    inherited_candidate_index=authority.inherited_candidate_index[config_hash],
                )
            )
    return tuple(out)


def _effective_config(payload: bytes, config_hash: str) -> dict[str, Any]:
    import json

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationPrepareError(
            f"the payload for frozen config {config_hash} is not JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ValidationPrepareError(
            f"the payload for frozen config {config_hash} is not an object"
        )
    effective = document.get("effective_config", document)
    if not isinstance(effective, dict):
        raise ValidationPrepareError(
            f"the payload for frozen config {config_hash} carries no effective config"
        )
    return dict(effective)


def _read_artifact_bytes(uri: str) -> bytes:
    if not uri.startswith("file:"):
        raise ValidationPrepareError(f"refusing a non-file config artifact URI {uri!r}")
    raw = uri[len("file://") :] if uri.startswith("file://") else uri[len("file:") :]
    path = Path(raw).resolve()
    if not path.is_file():
        raise ValidationPrepareError(f"the config artifact {path} does not exist")
    return path.read_bytes()


# ------------------------------------------------------------------------------------------- #
# the ten VALIDATION members, resolved against accepted upstream in the target store
# ------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _ResolvedMember:
    member_index: int
    dataset_id: str
    round_id: str
    chromosome: str
    dataset_registry_id: str
    profile_snapshot_id: str
    profile_snapshot_member_id: str
    bam_profile_id: str
    profile_id: str
    content_hash: str
    feature_values_hash: str


def _resolve_members(
    conn: Connection, authority: PhaseDAuthority
) -> tuple[tuple[_ResolvedMember, ...], dict[str, Any]]:
    """Each frozen schedule entry must resolve to exactly one authoritative VALIDATION row."""
    from sqlalchemy import text

    members: list[_ResolvedMember] = []
    snapshots: set[str] = set()
    for index, entry in enumerate(authority.schedule.members):
        rows = (
            conn.execute(
                text(
                    "SELECT dr.id AS dataset_registry_id, dr.dataset_id, dr.round_id, "
                    "       dr.chromosome, sa.partition, psm.partition AS member_partition, "
                    "       psm.id AS psm_id, psm.profile_snapshot_id, psm.bam_profile_id, "
                    "       psm.feature_values_hash, bp.profile_id, bp.content_hash, "
                    "       bp.profile_status "
                    "  FROM catalog.dataset_registry dr "
                    "  JOIN catalog.split_allocations sa ON sa.dataset_registry_id = dr.id "
                    "  JOIN profiling.profile_snapshot_members psm "
                    "    ON psm.dataset_registry_id = dr.id "
                    "  JOIN profiling.bam_profiles bp ON bp.id = psm.bam_profile_id "
                    " WHERE dr.dataset_id = :d"
                ),
                {"d": entry.dataset_id},
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            raise ValidationPrepareError(
                f"validation member {entry.dataset_id} resolves to {len(rows)} upstream rows; "
                "exactly one authoritative row is required"
            )
        row = rows[0]
        if str(row["partition"]) != VALIDATION_PLAN_PARTITION:
            raise ValidationPrepareError(
                f"member {entry.dataset_id} is allocated to partition {row['partition']!r}; "
                "L2-F2-F confirms on VALIDATION only"
            )
        # the ALLOCATION and the SNAPSHOT MEMBER must agree. The plan-member FK carries the
        # snapshot member's partition, so a disagreement here would be persisted as validation on
        # the strength of a row that says otherwise.
        if str(row["member_partition"]) != VALIDATION_PLAN_PARTITION:
            raise ValidationPrepareError(
                f"member {entry.dataset_id} is a {row['member_partition']!r} snapshot member "
                "though the split allocates it to validation"
            )
        if str(row["chromosome"]) != entry.chromosome:
            raise ValidationPrepareError(
                f"member {entry.dataset_id} is on {row['chromosome']}, the frozen schedule says "
                f"{entry.chromosome}"
            )
        if str(row["round_id"]) != entry.round_id:
            raise ValidationPrepareError(
                f"member {entry.dataset_id} has round {row['round_id']}, the frozen schedule says "
                f"{entry.round_id}"
            )
        if str(row["profile_status"]) != "COMPLETE":
            raise ValidationPrepareError(
                f"member {entry.dataset_id} has a {row['profile_status']} BAM profile; validation "
                "requires a COMPLETE one"
            )
        snapshots.add(str(row["profile_snapshot_id"]))
        members.append(
            _ResolvedMember(
                member_index=index,
                dataset_id=str(row["dataset_id"]),
                round_id=str(row["round_id"]),
                chromosome=str(row["chromosome"]),
                dataset_registry_id=str(row["dataset_registry_id"]),
                profile_snapshot_id=str(row["profile_snapshot_id"]),
                profile_snapshot_member_id=str(row["psm_id"]),
                bam_profile_id=str(row["bam_profile_id"]),
                profile_id=str(row["profile_id"]),
                content_hash=str(row["content_hash"]),
                feature_values_hash=str(row["feature_values_hash"]),
            )
        )
    if len(snapshots) != 1:
        raise ValidationPrepareError(
            f"the ten validation members span {len(snapshots)} profile snapshots; a campaign is "
            "measured against one"
        )
    snapshot = (
        conn.execute(
            text(
                "SELECT id, snapshot_hash, split_manifest_hash, registry_snapshot_hash "
                "  FROM profiling.profile_snapshots WHERE id = :i"
            ),
            {"i": next(iter(snapshots))},
        )
        .mappings()
        .one()
    )
    return tuple(members), dict(snapshot)


# ------------------------------------------------------------------------------------------- #
# persistence — one atomic scientific transaction, ending at zero jobs
# ------------------------------------------------------------------------------------------- #
def prepare_l2f2_validation_plan(
    *,
    target: Engine,
    baseline: Engine,
    finalist_freeze_path: str | Path,
    config_artifact_root: str | Path,
) -> ValidationPrepareResult:
    """THE production Phase-D preparation boundary. Provisioning in, science derived.

    Verifies the target is the validation store at the validation revision, loads and verifies the
    frozen finalist outcome, derives the authority, resolves the ten members, carries the four
    payloads forward from the closed baseline, and persists plan + members + configs + authority +
    binding in one transaction. Ends by asking the database's own argument-free bootstrap whether
    what it wrote is a campaign; if not, the transaction rolls back.

    Creates ZERO jobs. The materializer is a separate authorization and is not called here.
    """
    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    return _prepare_with_trust(
        target=target,
        baseline=baseline,
        finalist_freeze_path=finalist_freeze_path,
        config_artifact_root=Path(config_artifact_root),
        expected_database=VALIDATION_DATABASE_NAME,
        expected_revision=VALIDATION_REVISION,
    )


def _prepare_with_trust(
    *,
    target: Engine,
    baseline: Engine,
    finalist_freeze_path: str | Path,
    config_artifact_root: Path,
    expected_database: str,
    expected_revision: str,
    expected_freeze_sha256: str = ACCEPTED_FINALIST_FREEZE_SHA256,
    expected_closure_sha256: str = ACCEPTED_PHASE_C_CLOSURE_SHA256,
) -> ValidationPrepareResult:
    """The preparation core. Private, following the repository's existing ``*_with_trust`` pattern.

    The store NAMES, the revisions and the accepted artifact digests are parameters here and only
    here, so a proof can run this exact persistence logic against scratch databases and against
    deliberately wrong provisioning. They are NOT parameters of the public boundary above, which
    is the only production authority and compiles them in; a caller cannot reach this seam without
    importing a private name.

    Widening them does not widen the science. ``0024`` writes the campaign's identities into the
    Phase-D bootstrap as SQL literals, and this function refuses unless that bootstrap accepts what
    it wrote — so a substituted freeze, a substituted four or a substituted seed fails in the
    database no matter which digest a caller passed in here.
    """
    from sqlalchemy import text

    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
    from minos_engine.baseline.schedule import split_manifest_sha256

    freeze = load_finalist_freeze(
        finalist_freeze_path,
        expected_artifact_sha256=expected_freeze_sha256,
        expected_phase_c_closure_sha256=expected_closure_sha256,
    )
    authority = build_l2f2_phase_d_authority(freeze)
    if authority.candidate_count != 4 or authority.member_count != 10:
        raise ValidationPrepareError(  # pragma: no cover - the authority enforces this
            "the derived Phase-D authority is not the frozen 4 x 10 campaign"
        )
    if authority.logical_job_count != PHASE_D_LOGICAL_JOB_BUDGET:  # pragma: no cover
        raise ValidationPrepareError(
            "the derived Phase-D authority is not a 40-evaluation campaign"
        )

    configs = _read_frozen_configs(baseline, authority)

    with target.connect() as conn, conn.begin():
        _require_database(
            conn,
            database=expected_database,
            revision=expected_revision,
            role="validation target",
        )
        # every table this boundary writes is owned by the non-superuser control plane and grants
        # nothing to any service role. LOCAL, so the role reverts with the transaction.
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        members, snapshot = _resolve_members(conn, authority)

        # The split manifest is verified HERE, where the bytes are. A database cannot hash a file
        # on a filesystem it does not have, so 0024 requires the binding to CARRY a digest and
        # preparation is what proves that digest is the manifest's. The file is re-read and
        # re-hashed at preparation time rather than trusted from the authority that was built from
        # it, so a manifest edited between the two is caught.
        manifest_digest = split_manifest_sha256()
        if manifest_digest != authority.split_manifest_sha256:
            raise ValidationPrepareError(
                f"the committed split manifest now hashes to {manifest_digest}, but this campaign "
                f"was scheduled against {authority.split_manifest_sha256}; the ten members are "
                "not the ten that were frozen"
            )

        existing = conn.execute(
            text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
            {"h": authority.plan_hash},
        ).scalar_one_or_none()
        if existing is not None:
            result = _verify_existing(conn, authority, members=members, configs=configs)
        else:
            result = _insert_campaign(
                conn,
                authority,
                members=members,
                configs=configs,
                snapshot=snapshot,
                config_artifact_root=config_artifact_root,
            )

        # the database's own argument-free bootstrap is the last word. If it refuses, this
        # transaction rolls back rather than leaving a plan the runner would decline to execute.
        row = conn.execute(
            text(
                "SELECT plan_hash, execution_environment_hash "
                "  FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"
            )
        ).one()
        if str(row[0]) != authority.plan_hash:
            raise ValidationPrepareError(
                f"the Phase-D bootstrap resolved plan {row[0]}, not the prepared "
                f"{authority.plan_hash}"
            )
        if str(row[1]) != authority.execution_environment_hash:
            raise ValidationPrepareError(
                f"the Phase-D bootstrap resolved environment {row[1]}, not the frozen "
                f"{authority.execution_environment_hash}"
            )

        jobs = conn.execute(
            text("SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE plan_id = :p"),
            {"p": result["plan_id"]},
        ).scalar_one()
        if jobs:
            raise ValidationPrepareError(
                f"preparation must end at zero jobs; the plan carries {jobs}"
            )

        return ValidationPrepareResult(
            plan_hash=authority.plan_hash,
            plan_id=str(result["plan_id"]),
            authority_id=str(result["authority_id"]),
            binding_id=str(result["binding_id"]),
            created_plan=bool(result["created_plan"]),
            created_members=int(result["created_members"]),
            created_configs=int(result["created_configs"]),
            created_authority=bool(result["created_authority"]),
            created_binding=bool(result["created_binding"]),
            job_count=0,
            bootstrap_plan_hash=str(row[0]),
            bootstrap_environment_hash=str(row[1]),
        )


def _insert_campaign(
    conn: Connection,
    authority: PhaseDAuthority,
    *,
    members: tuple[_ResolvedMember, ...],
    configs: tuple[_TransferredConfig, ...],
    snapshot: dict[str, Any],
    config_artifact_root: Path,
) -> dict[str, Any]:
    """Write the whole campaign. Every TRAIN matrix column is NULL, by 0022's semantics."""
    from sqlalchemy import text

    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan

    accepted = build_accepted_experiment_plan()
    plan_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f_experiment_plans ("
            "  profile_snapshot_id, partition, snapshot_hash, split_manifest_hash, "
            "  registry_snapshot_hash, gatk_registry_hash, parameter_space_hash, "
            "  experiment_parameter_policy_hash, candidate_set_hash, train_member_count, "
            "  candidate_count, logical_job_count, plan_hash) "
            "VALUES (:sid, :part, :sh, :smh, :rsh, :grh, :psh, :eph, :csh, :mc, :cc, :ljc, :ph) "
            "RETURNING id"
        ),
        {
            "sid": snapshot["id"],
            "part": VALIDATION_PLAN_PARTITION,
            "sh": snapshot["snapshot_hash"],
            "smh": snapshot["split_manifest_hash"],
            "rsh": snapshot["registry_snapshot_hash"],
            "grh": accepted.gatk_registry_hash,
            "psh": authority.parameter_space_hash,
            "eph": accepted.experiment_parameter_policy_hash,
            "csh": authority.phase_c_candidate_set_hash,
            # historical column NAME: for a validation plan it holds the plan's member count.
            # 0022 documents the reinterpretation; nothing about it is TRAIN.
            "mc": len(members),
            "cc": len(configs),
            "ljc": PHASE_D_LOGICAL_JOB_BUDGET,
            "ph": authority.plan_hash,
        },
    ).scalar_one()

    for member in members:
        conn.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_plan_members ("
                "  plan_id, profile_snapshot_id, feature_matrix_id, profile_snapshot_member_id, "
                "  feature_matrix_member_id, bam_profile_id, dataset_registry_id, partition, "
                "  feature_values_hash, member_index, source_matrix_member_index) "
                # NULL, NULL: a validation member has no feature matrix, and 0022's conditional
                # check requires their absence rather than a placeholder.
                "VALUES (:p, :sid, NULL, :psm, NULL, :bp, :dr, :part, :fvh, :mi, :smi)"
            ),
            {
                "p": plan_id,
                "sid": member.profile_snapshot_id,
                "psm": member.profile_snapshot_member_id,
                "bp": member.bam_profile_id,
                "dr": member.dataset_registry_id,
                "part": VALIDATION_PLAN_PARTITION,
                "fvh": member.feature_values_hash,
                "mi": member.member_index,
                "smi": member.member_index,
            },
        )

    created_configs = 0
    for config in configs:
        uri = _publish_payload(config, root=config_artifact_root)
        # look up, then insert. Both tables are append-only (0001 for artifacts by policy, 0006 by
        # trigger for payloads), so ON CONFLICT DO UPDATE would be an UPDATE against a table whose
        # whole point is that it has none.
        artifact_id = conn.execute(
            text("SELECT id FROM catalog.artifacts WHERE sha256 = :s"),
            {"s": config.config_hash},
        ).scalar_one_or_none()
        if artifact_id is None:
            artifact_id = conn.execute(
                text(
                    "INSERT INTO catalog.artifacts "
                    "  (uri, sha256, media_type, size_bytes, provenance) "
                    "VALUES (:u, :s, :m, :b, :prov) RETURNING id"
                ),
                {
                    "u": uri,
                    "s": config.config_hash,
                    "m": config.media_type,
                    "b": config.size_bytes,
                    "prov": _PROVENANCE,
                },
            ).scalar_one()
        payload_id = conn.execute(
            text("SELECT id FROM experiments.l2f_config_payloads WHERE config_hash = :h"),
            {"h": config.config_hash},
        ).scalar_one_or_none()
        if payload_id is None:
            payload_id = conn.execute(
                text(
                    "INSERT INTO experiments.l2f_config_payloads ("
                    "  config_hash, parameter_space_hash, schema_version, media_type, artifact_id) "
                    "VALUES (:ch, :psh, :sv, :mt, :aid) RETURNING id"
                ),
                {
                    "ch": config.config_hash,
                    "psh": config.parameter_space_hash,
                    "sv": config.schema_version,
                    "mt": config.media_type,
                    "aid": artifact_id,
                },
            ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_plan_configs ("
                "  plan_id, config_payload_id, config_hash, parameter_space_hash, config_index) "
                "VALUES (:p, :pid, :ch, :psh, :ci)"
            ),
            {
                "p": plan_id,
                "pid": payload_id,
                "ch": config.config_hash,
                "psh": config.parameter_space_hash,
                "ci": config.config_index,
            },
        )
        created_configs += 1

    authority_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f2_execution_authorities ("
            # canary_job_key is deliberately absent: only Phase A has a canary, and 0021's
            # phase-semantic CHECK requires it NULL for PHASE_D. execution_environment_hash is
            # absent because the table has no such column — the environment identity lives on the
            # Phase-D binding, where 0024 pins it to the frozen literal.
            "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
            "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
            "  logical_job_count) "
            "VALUES (:bp, 'PHASE_D', :p, :ph, :ts, :cs, :ps, :mc, :cc, :lj) RETURNING id"
        ),
        {
            "bp": authority.baseline_protocol_hash,
            "p": plan_id,
            "ph": authority.plan_hash,
            "ts": authority.split_manifest_sha256,
            "cs": authority.phase_c_candidate_set_hash,
            "ps": authority.parameter_space_hash,
            "mc": len(members),
            "cc": len(configs),
            "lj": PHASE_D_LOGICAL_JOB_BUDGET,
        },
    ).scalar_one()

    binding_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f2_phase_d_binding ("
            "  baseline_protocol_hash, authority_id, plan_id, plan_hash, "
            "  finalist_freeze_sha256, phase_c_closure_sha256, parameter_space_hash, "
            "  execution_environment_hash, scoring_contract_hash, minos_subnet_sha, "
            "  split_manifest_sha256, seed_config_hash, ordered_config_hashes, "
            "  inherited_candidate_indices, member_count, candidate_count, logical_job_count) "
            "VALUES (:bp, :aid, :p, :ph, :fz, :cl, :ps, :env, :sc, :ms, :sm, :seed, "
            "        :cfgs, :idx, :mc, :cc, :lj) RETURNING id"
        ),
        {
            "bp": authority.baseline_protocol_hash,
            "aid": authority_id,
            "p": plan_id,
            "ph": authority.plan_hash,
            "fz": authority.finalist_freeze_sha256,
            "cl": authority.phase_c_closure_sha256,
            "ps": authority.parameter_space_hash,
            "env": authority.execution_environment_hash,
            "sc": authority.scoring_contract_hash,
            "ms": authority.minos_subnet_sha,
            "sm": authority.split_manifest_sha256,
            "seed": authority.seed_config_hash,
            "cfgs": list(authority.ordered_config_hashes),
            "idx": [
                authority.inherited_candidate_index[h] for h in authority.ordered_config_hashes
            ],
            "mc": len(members),
            "cc": len(configs),
            "lj": PHASE_D_LOGICAL_JOB_BUDGET,
        },
    ).scalar_one()

    return {
        "plan_id": plan_id,
        "authority_id": authority_id,
        "binding_id": binding_id,
        "created_plan": True,
        "created_members": len(members),
        "created_configs": created_configs,
        "created_authority": True,
        "created_binding": True,
    }


def _publish_payload(config: _TransferredConfig, *, root: Path) -> str:
    """Write the verified bytes into the target's content-addressed CONFIG root."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{config.config_hash}.json"
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != config.config_hash:
            raise ValidationPrepareError(
                f"an existing artifact at {path} does not hash to {config.config_hash}"
            )
    else:
        path.write_bytes(config.payload)
    return f"file://{path.resolve()}"


def _verify_existing(
    conn: Connection,
    authority: PhaseDAuthority,
    *,
    members: tuple[_ResolvedMember, ...],
    configs: tuple[_TransferredConfig, ...],
) -> dict[str, Any]:
    """A replay verifies every immutable field. Never overwrite, never delete and recreate."""
    from sqlalchemy import text

    plan = (
        conn.execute(
            text(
                "SELECT id, partition, candidate_count, logical_job_count, train_member_count "
                "  FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"
            ),
            {"h": authority.plan_hash},
        )
        .mappings()
        .one()
    )
    plan_id = plan["id"]
    if str(plan["partition"]) != VALIDATION_PLAN_PARTITION:
        raise ValidationPrepareError("the persisted plan for this hash is not a VALIDATION plan")
    if int(plan["candidate_count"]) != len(configs) or int(plan["train_member_count"]) != len(
        members
    ):
        raise ValidationPrepareError("the persisted plan disagrees about its own shape")
    if int(plan["logical_job_count"]) != PHASE_D_LOGICAL_JOB_BUDGET:
        raise ValidationPrepareError("the persisted plan disagrees about its logical job count")

    persisted_configs = [
        str(r["config_hash"])
        for r in conn.execute(
            text(
                "SELECT config_hash FROM experiments.l2f_experiment_plan_configs "
                " WHERE plan_id = :p ORDER BY config_index"
            ),
            {"p": plan_id},
        ).mappings()
    ]
    if persisted_configs != list(authority.ordered_config_hashes):
        raise ValidationPrepareError(
            "the persisted plan holds different configurations than the frozen four; refusing to "
            "reconcile a conflicting scientific identity"
        )
    persisted_members = [
        (str(r["dataset_registry_id"]), str(r["partition"]))
        for r in conn.execute(
            text(
                "SELECT dataset_registry_id, partition "
                "  FROM experiments.l2f_experiment_plan_members "
                " WHERE plan_id = :p ORDER BY member_index"
            ),
            {"p": plan_id},
        ).mappings()
    ]
    if [d for d, _ in persisted_members] != [m.dataset_registry_id for m in members]:
        raise ValidationPrepareError("the persisted plan holds different VALIDATION members")
    if any(p != VALIDATION_PLAN_PARTITION for _, p in persisted_members):
        raise ValidationPrepareError("a persisted plan member is not a VALIDATION member")

    authority_id = conn.execute(
        text(
            "SELECT id FROM experiments.l2f2_execution_authorities "
            " WHERE phase = 'PHASE_D' AND plan_id = :p AND plan_hash = :h"
        ),
        {"p": plan_id, "h": authority.plan_hash},
    ).scalar_one()
    binding = (
        conn.execute(
            text(
                "SELECT id, finalist_freeze_sha256, phase_c_closure_sha256, seed_config_hash, "
                "       ordered_config_hashes, split_manifest_sha256 "
                "  FROM experiments.l2f2_phase_d_binding WHERE plan_id = :p"
            ),
            {"p": plan_id},
        )
        .mappings()
        .one()
    )
    if str(binding["finalist_freeze_sha256"]) != authority.finalist_freeze_sha256:
        raise ValidationPrepareError("the persisted binding cites a different finalist freeze")
    if str(binding["phase_c_closure_sha256"]) != authority.phase_c_closure_sha256:
        raise ValidationPrepareError("the persisted binding cites a different Phase-C closure")
    if list(binding["ordered_config_hashes"]) != list(authority.ordered_config_hashes):
        raise ValidationPrepareError("the persisted binding names a different ordered four")
    if str(binding["split_manifest_sha256"]) != authority.split_manifest_sha256:
        raise ValidationPrepareError("the persisted binding cites a different split manifest")

    return {
        "plan_id": plan_id,
        "authority_id": authority_id,
        "binding_id": binding["id"],
        "created_plan": False,
        "created_members": 0,
        "created_configs": 0,
        "created_authority": False,
        "created_binding": False,
    }
