"""Transactional persistence of one safe-controller decision into the operational store.

**What the contract actually requires.** The Layer 2 specification's live decision algorithm
ends with two ordered steps::

    persist decision + all candidate reasons in one transaction
    return exact canonical CONFIG bytes referenced by persisted hash

Persistence is not a side effect of returning a config; it is the thing that makes the returned
config legitimate. Until now that requirement was met against the filesystem
(``decision_publication``), recorded as ``FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION``
-- deferred, never waived, because ``runtime.decisions`` was a strict subset of the table the
contract describes and the migration topology had no way to complete it. The overlay lineage
(``migrations_runtime/``) completes the table; this module is the narrow write path onto it.

**Candidate reasons.** Under ``SAFE_BASELINE`` the candidate set is the singleton ``{baseline}``:
capability scope is ``SAFE_BASELINE_ONLY``, and the qualification observed
``candidate_generation_count == 0`` across all 200 decisions. The row therefore carries the whole
candidate set -- ``baseline_config_id``, ``selected_config_id`` (equal, by database CHECK) --
together with the manifest that records ``guards``, ``requested_mode``, ``actual_mode`` and
``fallback_reason``, all in ONE transaction. A ``runtime.decision_candidates`` table becomes
necessary when contextual candidate generation is authorized; creating it empty now would be
schema for a capability the frozen gate lists as NOT AUTHORIZED.

**Nothing here is trusted from the caller.** Not the decision (this module makes it, from
verified capabilities, rather than accepting one), not the config UUID (resolved from the
database by accepted config hash), not the profile UUID (resolved by the proven logical profile
id and then cross-checked field by field against what the production schema owns). A dict cannot
become a ``VerifiedPersistedSafeDecision``: only a completed, committed and re-read transaction
mints one.

**This does not activate anything.** ``Layer2Service.select_config`` still raises. Persistence is
qualified on its own so that activation is later a small, reviewable step rather than a large one.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

import sqlalchemy as sa

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.layer2.contracts import ControlMode, FallbackReason
from minos_engine.storage.runtime_decision_contract import (
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_REVISION,
    decisions_table,
    runtime_overlay_revision,
)

__all__ = [
    "PERSISTED_DECISION_SCHEMA",
    "SafeDecisionPersistenceError",
    "VerifiedPersistedSafeDecision",
    "persist_safe_decision",
    "provision_safe_config_row",
    "resolve_owned_profile_row",
    "resolve_safe_config_row",
]

PERSISTED_DECISION_SCHEMA: Final = "l2h-persisted-safe-decision-v1"

#: Columns whose value is the decision. A converged retry must agree on every one of them.
SCIENTIFIC_COLUMNS: Final[tuple[str, ...]] = (
    "round_id",
    "decision_hash",
    "decision_manifest_hash",
    "config_id",
    "model_bundle_id",
    "profile_id",
    "controller_version",
    "mode",
    "requested_mode",
    "fallback_reason",
    "parameter_space_hash",
    "baseline_config_id",
    "selected_config_id",
    "decision_manifest",
)

#: Operational, deliberately NOT part of the decision identity. A retry of the same decision must
#: converge rather than mint a new identity, so wall-clock cannot be an input to the hash; and the
#: first writer's ``decided_at`` is the truthful one -- that is when the decision was made.
OPERATIONAL_COLUMNS: Final[tuple[str, ...]] = ("id", "created_at", "decided_at")


class SafeDecisionPersistenceError(MinosEngineError):
    """A decision could not be persisted, or what is stored is not what was decided."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeDecisionPersistenceError(message)


_PERSISTED_TOKEN: Final = object()


class VerifiedPersistedSafeDecision:
    """A decision that is committed in PostgreSQL and has been read back after the COMMIT.

    Minted only by :func:`persist_safe_decision`. A caller cannot construct one from a dict, so
    "the decision was persisted" is never something a caller can merely assert.
    """

    __slots__ = (
        "converged",
        "decision_hash",
        "decision_manifest_hash",
        "mode",
        "persisted_id",
        "result",
        "row",
        "selected_config_id",
    )

    def __init__(
        self,
        token: object,
        *,
        result: Any,
        row: dict[str, Any],
        converged: bool,
    ) -> None:
        if token is not _PERSISTED_TOKEN:
            raise SafeDecisionPersistenceError(
                "a persisted decision may only be minted by the persisting transaction; a "
                "dictionary has not been committed to anything"
            )
        self.result = result
        self.row = dict(row)
        #: True when an identical decision was already committed: a converged retry, not a failure.
        self.converged = converged
        self.persisted_id = str(row["id"])
        self.decision_hash = str(row["decision_hash"])
        self.decision_manifest_hash = str(row["decision_manifest_hash"])
        self.selected_config_id = str(row["selected_config_id"])
        self.mode = str(row["mode"])


# --------------------------------------------------------------------------- #
# prerequisites
# --------------------------------------------------------------------------- #
def require_persistence_prerequisites(conn: Any) -> dict[str, str]:
    """The connected database must be the operational store, at the accepted schema + overlay."""
    from minos_engine.storage.database import (
        OperationalDatabaseIdentityError,
        verify_operational_database_identity,
    )
    from minos_engine.storage.runtime_overlay import main_lineage_revision

    try:
        database = verify_operational_database_identity(conn)
    except OperationalDatabaseIdentityError as error:
        raise SafeDecisionPersistenceError(
            f"live decisions are only persisted into the canonical operational store: {error}"
        ) from None
    main = main_lineage_revision(conn)
    _require(
        main == REQUIRED_MAIN_REVISION,
        f"the operational schema is at {main}, not the accepted {REQUIRED_MAIN_REVISION}",
    )
    overlay = runtime_overlay_revision(conn)
    _require(
        overlay == RUNTIME_OVERLAY_REVISION,
        f"the runtime overlay is at {overlay}, not {RUNTIME_OVERLAY_REVISION}; "
        "runtime.decisions cannot hold a complete decision until it is applied",
    )
    return {
        "database": database,
        "main_revision": str(main),
        "overlay_revision": str(overlay),
    }


# --------------------------------------------------------------------------- #
# resolving the two foreign rows, neither of them from the caller
# --------------------------------------------------------------------------- #
def resolve_safe_config_row(conn: Any, *, config_hash: str, parameter_space_hash: str) -> str:
    """Resolve ``catalog.gatk_configs`` BY ACCEPTED HASH. Absent or ambiguous fails closed.

    A caller-supplied UUID is never accepted: a UUID says nothing about which config it names,
    and the whole point of the decision is that the bytes returned are the accepted ones.
    """
    rows = conn.execute(
        sa.text("SELECT id, parameter_space_hash FROM catalog.gatk_configs WHERE config_hash = :h"),
        {"h": config_hash},
    ).all()
    _require(
        bool(rows),
        f"the accepted config {config_hash} has no row in catalog.gatk_configs; the operational "
        "store has not been provisioned with the config this decision returns",
    )
    _require(
        len(rows) == 1,
        f"catalog.gatk_configs holds {len(rows)} rows for config {config_hash}; a config hash "
        "naming more than one row is a schema violation",
    )
    row = rows[0]
    _require(
        str(row[1]) == parameter_space_hash,
        f"the stored config row declares parameter space {row[1]}, not the accepted "
        f"{parameter_space_hash}",
    )
    return str(row[0])


#: Identity fields ``profiling.bam_profiles`` owns, mapped to the proven owned-profile attribute.
#: ``profile_manifest_hash`` is deliberately absent: nothing in this engine defines its preimage
#: (see ``Layer1ProfileReference``), so requiring agreement on it would dress an unauthenticated
#: value up as a cross-check. ``profile_sha256`` is here instead and is stronger -- it is the hash
#: of the whole profile document, so agreement on it settles every field inside it.
PROFILE_IDENTITY_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("attestation_hash", "attestation_hash"),
    ("bai_sha256", "bai_sha256"),
    ("bam_sha256", "bam_sha256"),
    ("fai_sha256", "fai_sha256"),
    ("identity_tuple_hash", "identity_tuple_hash"),
    ("profile_manifest_sha256", "profile_manifest_sha256"),
    ("profile_sha256", "profile_sha256"),
    ("reference_sha256", "reference_sha256"),
    ("region_hash", "region_hash"),
    ("registry_snapshot_hash", "registry_snapshot_hash"),
)

#: Identity fields reached through the schema's own FK into ``catalog.dataset_registry``.
REGISTRY_IDENTITY_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("chromosome", "chromosome"),
    ("dataset_id", "dataset_id"),
    ("round_id", "round_id"),
)

_PROFILE_QUERY: Final = sa.text(
    "SELECT p.id, "
    + ", ".join(f"p.{column}" for column, _ in PROFILE_IDENTITY_FIELDS)
    + ", p.integrity_degraded, "
    + ", ".join(f"r.{column} AS registry_{column}" for column, _ in REGISTRY_IDENTITY_FIELDS)
    + " FROM profiling.bam_profiles AS p "
    "JOIN catalog.dataset_registry AS r ON r.id = p.dataset_registry_id "
    "WHERE p.profile_id = :pid"
)


def resolve_owned_profile_row(conn: Any, *, owned: Any) -> str:
    """Resolve the operational profile row by the PROVEN logical profile id, then cross-check it.

    **Which table.** ``0001`` aimed the decision FK at ``profiling.profiles``;
    ``0004_l2d_profile_ingestion`` built the real ingestion on ``profiling.bam_profiles``, and
    that is where the operational L1 pipeline puts profiles -- 75 rows there, none in the older
    table, and no code path anywhere writes the older one. The overlay re-aims the foreign key
    accordingly, so this resolves against the table production actually populates.

    **Opened by name, never enumerated.** The lookup is a single equality on the proven
    ``profile_id``. The table also holds VALIDATION and TEST members; they are not listed,
    counted, filtered or observed to exist.

    The row must agree with the verified request on every identity field the production schema
    owns, including the dataset, round and chromosome reached through the schema's own foreign
    key. Disagreement is an authority failure, never a fallback: a decision recorded against the
    wrong profile row is worse than no decision.
    """
    row = conn.execute(_PROFILE_QUERY, {"pid": owned.profile_id}).mappings().first()
    _require(
        row is not None,
        f"profile {owned.profile_id!r} has no row in profiling.bam_profiles; the operational "
        "Layer 1 ingestion has not populated the profile this decision is about",
    )
    assert row is not None
    for column, attribute in PROFILE_IDENTITY_FIELDS:
        expected = str(getattr(owned, attribute))
        actual = "" if row[column] is None else str(row[column])
        _require(
            actual == expected,
            f"the stored profile row disagrees on {column}: {actual!r} != proven {expected!r}",
        )
    _require(
        bool(row["integrity_degraded"]) == bool(owned.integrity_degraded),
        "the stored profile row disagrees on integrity_degraded",
    )
    for column, attribute in REGISTRY_IDENTITY_FIELDS:
        expected = str(getattr(owned, attribute))
        actual = str(row[f"registry_{column}"] or "")
        _require(
            actual == expected,
            f"the profile's dataset registry row disagrees on {column}: {actual!r} != proven "
            f"{expected!r}",
        )
    return str(row["id"])


def provision_safe_config_row(
    engine: Any, *, config_hash: str, parameter_space_hash: str, payload_root: Any = None
) -> dict[str, Any]:
    """ADMINISTRATIVE, idempotent binding of the accepted config into ``catalog.gatk_configs``.

    Deliberately separate from the decision transaction. ``minos_live`` holds only SELECT on this
    table, and it must stay that way: a live decision path that could insert catalog rows could
    insert a config nobody accepted and then decide in favour of it. Provisioning verifies the
    frozen payload bytes hash to the accepted config hash before writing anything, so the row can
    only ever name a config this repository actually holds.
    """
    from pathlib import Path

    from minos_engine.models.config_table import CONFIG_PAYLOAD_ROOT

    root = Path(payload_root) if payload_root is not None else CONFIG_PAYLOAD_ROOT
    path = root / f"{config_hash}.json"
    _require(path.is_file(), f"the accepted config payload is missing: {path}")
    _require(not path.is_symlink(), f"{path} is a symlink")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    _require(
        digest == config_hash,
        f"{path} hashes to {digest}, so it is not the config it is named for",
    )
    with engine.begin() as conn:
        conn.execute(sa.text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            sa.text(
                "INSERT INTO catalog.gatk_configs (config_hash, parameter_space_hash) "
                "VALUES (:c, :p) ON CONFLICT (config_hash) DO NOTHING"
            ),
            {"c": config_hash, "p": parameter_space_hash},
        )
    with engine.connect() as conn:
        config_id = resolve_safe_config_row(
            conn, config_hash=config_hash, parameter_space_hash=parameter_space_hash
        )
    return {
        "config_id": config_id,
        "config_hash": config_hash,
        "parameter_space_hash": parameter_space_hash,
        "payload_sha256": digest,
        "payload_bytes": len(payload),
    }


# --------------------------------------------------------------------------- #
# the write path
# --------------------------------------------------------------------------- #
def _row_state(row: Any) -> dict[str, Any]:
    state = {name: row[name] for name in SCIENTIFIC_COLUMNS}
    state["decision_manifest"] = dict(state["decision_manifest"])
    for name in ("config_id", "model_bundle_id"):
        state[name] = None if state[name] is None else str(state[name])
    for name in ("profile_id", "baseline_config_id", "selected_config_id"):
        state[name] = str(state[name])
    return state


def _read_persisted(conn: Any, *, round_id: str, decision_hash: str) -> Any:
    return (
        conn.execute(
            sa.select(decisions_table).where(
                sa.and_(
                    decisions_table.c.round_id == round_id,
                    decisions_table.c.decision_hash == decision_hash,
                )
            )
        )
        .mappings()
        .first()
    )


def _require_stored_matches(
    stored: Any, intended: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """Everything scientific must agree, and the manifest must survive the JSONB round trip."""
    actual = _row_state(stored)
    # the manifest first: when a stored decision diverges, "these are different decisions" is
    # what the operator needs to read, not "two hashes differ".
    _require(
        actual["decision_manifest"] == manifest,
        "the stored decision manifest is not the manifest that was decided",
    )
    for name in SCIENTIFIC_COLUMNS:
        if name == "decision_manifest":
            continue
        _require(
            actual[name] == intended[name],
            f"the stored decision disagrees on {name}: {actual[name]!r} != {intended[name]!r}",
        )
    readback = hashlib.sha256(canonical_json_bytes(actual["decision_manifest"])).hexdigest()
    _require(
        readback == intended["decision_manifest_hash"],
        f"the stored manifest re-canonicalizes to {readback}, not the recorded "
        f"{intended['decision_manifest_hash']}",
    )


def persist_safe_decision(
    *,
    request: Any,
    authority: Any,
    ownership: Any,
    engine: Any,
    root: Any = None,
) -> VerifiedPersistedSafeDecision:
    """Decide, persist atomically, re-read after COMMIT, and only then return.

    The decision is made HERE from verified capabilities rather than accepted as a parameter, so
    there is no seam through which an invented decision could be persisted. The accepted
    SAFE-CONTROLLER-FROZEN gate is required first: a store that records decisions from an
    unaccepted controller is a store of unaccepted decisions.

    Ordering is the contract's: nothing is returned until the row is committed AND has been read
    back from a second transaction. A caller therefore cannot observe a decision that is not
    already durable.
    """
    from minos_engine.layer2.safe_controller import (
        safe_decision_manifest_content,
        safe_decision_manifest_identity,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_frozen_acceptance import (
        SafeControllerFrozenAcceptanceError,
        verify_accepted_safe_controller_frozen_gate,
    )

    try:
        accepted = verify_accepted_safe_controller_frozen_gate(root)
    except SafeControllerFrozenAcceptanceError as error:
        raise SafeDecisionPersistenceError(
            f"decisions are not persisted without the accepted frozen controller: {error}"
        ) from None

    owned = ownership.require_owned_request(request)
    result = select_safe_baseline(request=request, authority=authority, ownership=ownership)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=ownership, owned=owned
    )
    decision_hash = safe_decision_manifest_identity(manifest)
    _require(
        decision_hash == result.decision.decision_manifest_hash,
        "the manifest identity disagrees with the decision the controller returned",
    )
    _require(
        result.mode is ControlMode.SAFE_BASELINE,
        f"this store persists SAFE_BASELINE decisions; the controller returned {result.mode}",
    )
    _require(
        accepted["capability_scope"] == "SAFE_BASELINE_ONLY",
        "the accepted gate does not scope this controller to SAFE_BASELINE_ONLY",
    )
    manifest_bytes = canonical_json_bytes(manifest)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            require_persistence_prerequisites(conn)
            config_id = resolve_safe_config_row(
                conn,
                config_hash=result.selected_config.sha256,
                parameter_space_hash=authority.parameter_space_hash,
            )
            baseline_config_id = resolve_safe_config_row(
                conn,
                config_hash=authority.baseline_config_hash,
                parameter_space_hash=authority.parameter_space_hash,
            )
            profile_row_id = resolve_owned_profile_row(conn, owned=owned)
            intended: dict[str, Any] = {
                "round_id": owned.round_id,
                "decision_hash": decision_hash,
                "decision_manifest_hash": hashlib.sha256(manifest_bytes).hexdigest(),
                # RETIRED in the overlay: 0001 never defined what config_id meant, so no row of
                # ours gives it one. The config identities are the two explicit columns.
                "config_id": None,
                # SAFE_BASELINE loaded no model. NULL is the statement, not an omission.
                "model_bundle_id": None,
                "profile_id": profile_row_id,
                "controller_version": str(manifest["controller_version"]),
                "mode": ControlMode.SAFE_BASELINE.value,
                "requested_mode": str(manifest["requested_mode"]),
                "fallback_reason": str(manifest["fallback_reason"]),
                "parameter_space_hash": authority.parameter_space_hash,
                "baseline_config_id": baseline_config_id,
                "selected_config_id": config_id,
                "decision_manifest": manifest,
            }
            _require(
                intended["fallback_reason"] in {f.value for f in FallbackReason},
                f"{intended['fallback_reason']!r} is not a typed fallback reason",
            )
            inserted = conn.execute(
                sa.dialects.postgresql.insert(decisions_table)
                .values(**intended)
                .on_conflict_do_nothing(constraint="uq_decisions_round_id_decision_hash")
                .returning(decisions_table.c.id)
            ).first()
            stored = _read_persisted(conn, round_id=owned.round_id, decision_hash=decision_hash)
            _require(stored is not None, "the decision is not present after its own insert")
            _require_stored_matches(stored, intended, manifest)
            converged = inserted is None
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    # AFTER the commit, from a new transaction: durable, and still the decision that was made.
    with engine.connect() as conn:
        durable = _read_persisted(conn, round_id=owned.round_id, decision_hash=decision_hash)
        _require(durable is not None, "the committed decision is not readable after COMMIT")
        _require_stored_matches(durable, intended, manifest)
        row = dict(durable)

    return VerifiedPersistedSafeDecision(
        _PERSISTED_TOKEN, result=result, row=row, converged=converged
    )
