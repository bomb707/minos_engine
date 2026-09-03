"""The production Phase-D closure entry. It takes nothing, and that is the whole design.

Selecting the L2-F2 baseline is the single most consequential act in this search, so the public
entry offers no lever at all: no observations, no scores, no weights, no candidate indices, no
rank, no winner, no plan, no partition, no database, no revision. Everything is derived from
pinned sources, and the ranking mathematics belong entirely to the already-frozen implementation.

The revision pin is ``0026``, and the older pins deliberately stay where they are. A store at
``0026`` refuses the Phase-D runner (pinned ``0024``) and the biological evaluator (pinned
``0025``): execution and evaluation are both sealed before closure can read a single row. That
state machine is the point, not an accident of ordering:

    0024 EXECUTE  ->  0025 EVALUATE  ->  0026 CLOSE
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from sqlalchemy.engine import Connection, Engine

from minos_engine.baseline.phase_d_observations import PhaseDClosure, build_phase_d_closure
from minos_engine.common.errors import MinosEngineError

__all__ = [
    "ENV_FINALIST_FREEZE_PATH",
    "PHASE_D_CLOSURE_DATABASE",
    "PHASE_D_CLOSURE_REVISION",
    "PhaseDClosureAuthorityError",
    "authorize_validation_closure_connection",
    "derive_l2f2_phase_d_closure",
]

ACCEPTED_BASELINE_PROTOCOL_HASH: Final = (
    "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
)
ACCEPTED_SELECTION_INTERPRETATION_HASH: Final = (
    "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd"
)
ACCEPTED_INTERPRETATION_STATUS: Final = "OUTCOME_BLIND_POST_COLLECTION_CLARIFICATION"

PHASE_D_CLOSURE_DATABASE: Final = "minos_l2f2_validation"
PHASE_D_CLOSURE_REVISION: Final = "0026_l2f2_phase_d_closure"
_CLOSURE_VIEW: Final = "evaluation.l2f_phase_d_closure_inputs"
_REQUIRED_MEMBERSHIP: Final = "minos_evaluator"
_FORBIDDEN_MEMBERSHIPS: Final = frozenset(
    {"minos_admin", "minos_runner", "minos_trainer", "minos_live"}
)

#: the frozen finalist artifact. A path, not a candidate: its bytes are verified on load.
ENV_FINALIST_FREEZE_PATH: Final = "MINOS_L2F2_FINALIST_FREEZE_PATH"


class PhaseDClosureAuthorityError(MinosEngineError):
    """The caller, the store or the campaign is not authorized to be closed."""


def authorize_validation_closure_connection(conn: Connection) -> None:
    """The closure boundary: right store, right revision, least-privilege principal."""
    _authorize_closure_connection(
        conn, database_name=PHASE_D_CLOSURE_DATABASE, revision=PHASE_D_CLOSURE_REVISION
    )


def _authorize_closure_connection(conn: Connection, *, database_name: str, revision: str) -> None:
    """The single boundary body. Store pins are parameters ONLY here, never on the public entry."""
    from sqlalchemy import text

    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != database_name:
        raise PhaseDClosureAuthorityError(
            f"the Phase-D closer refuses database {database!r}; it closes only {database_name!r}"
        )
    live = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live != revision:
        raise PhaseDClosureAuthorityError(
            f"validation database revision is {live!r}, expected {revision!r}; the Phase-D "
            "closure surface is unavailable"
        )

    principal = conn.execute(
        text(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
            "  FROM pg_roles WHERE rolname = session_user"
        )
    ).one_or_none()
    if principal is None:  # pragma: no cover - a session always has a principal
        raise PhaseDClosureAuthorityError("the session principal could not be resolved")
    name, can_login, is_super, createdb, createrole, bypassrls = principal
    if not can_login:
        raise PhaseDClosureAuthorityError(f"session principal {name!r} cannot log in")
    for label, elevated in (
        ("SUPERUSER", is_super),
        ("CREATEDB", createdb),
        ("CREATEROLE", createrole),
        ("BYPASSRLS", bypassrls),
    ):
        if elevated:
            raise PhaseDClosureAuthorityError(
                f"session principal {name!r} holds {label}; the closer must be least-privilege"
            )

    memberships = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT r.rolname FROM pg_auth_members m "
                "  JOIN pg_roles r ON r.oid = m.roleid "
                "  JOIN pg_roles g ON g.oid = m.member "
                " WHERE g.rolname = session_user AND r.rolname LIKE 'minos%'"
            )
        )
    }
    if memberships != {_REQUIRED_MEMBERSHIP}:
        raise PhaseDClosureAuthorityError(
            f"session principal {name!r} holds MINOS memberships {sorted(memberships)}; the "
            f"Phase-D closer requires exactly {{{_REQUIRED_MEMBERSHIP!r}}}"
        )
    forbidden = memberships & _FORBIDDEN_MEMBERSHIPS
    if forbidden:  # pragma: no cover - unreachable given the equality above
        raise PhaseDClosureAuthorityError(f"session principal holds {sorted(forbidden)}")


def _frozen_phase_d_authority(freeze_path: str | Path) -> Any:
    """DERIVE the Phase-D authority from the finalist artifact, never from a caller."""
    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
    from minos_engine.storage.l2f2_validation_prepare import (
        ACCEPTED_FINALIST_FREEZE_SHA256,
        ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )

    freeze = load_finalist_freeze(
        freeze_path,
        expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
        expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )
    return build_l2f2_phase_d_authority(freeze)


def _required_file(variable: str) -> Path:
    """Validate a provisioned path. Never create it, never guess it."""
    import os

    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise PhaseDClosureAuthorityError(f"{variable} is not set")
    path = Path(raw)
    if not path.is_absolute():
        raise PhaseDClosureAuthorityError(f"{variable} must be absolute, got {path}")
    if path.is_symlink() or not path.is_file():
        raise PhaseDClosureAuthorityError(f"{variable} {path} is missing or a symlink")
    return path


def _verify_committed_authorities(authority: Any) -> str:
    """Verify the COMMITTED manifests, and their agreement, before a score row is read.

    Recomputing a hash from source proves the source is self-consistent; it proves nothing about
    what was committed. A tampered or missing manifest has to stop closure BEFORE it reads a
    score, because by the time an outcome has been consumed the damage of having consulted the
    wrong rulebook is already done.

    Returns the VERIFIED committed protocol hash, which is what the closure then binds — never a
    source-derived value that happens to disagree with the manifest.
    """
    from minos_engine.baseline.phase_d_selection import (
        PhaseDSelectionInterpretationError,
        load_committed_selection_interpretation,
    )
    from minos_engine.baseline.protocol import (
        BaselineProtocolError,
        build_baseline_protocol,
        compute_protocol_hash,
        load_committed_protocol,
    )

    try:
        committed_protocol = load_committed_protocol()
    except BaselineProtocolError as exc:
        raise PhaseDClosureAuthorityError(
            f"the committed baseline protocol manifest is unusable: {exc}"
        ) from exc
    protocol_hash = str(committed_protocol.get("protocol_hash"))
    if protocol_hash != ACCEPTED_BASELINE_PROTOCOL_HASH:
        raise PhaseDClosureAuthorityError(
            f"the committed protocol hash is {protocol_hash}, not this search's "
            f"{ACCEPTED_BASELINE_PROTOCOL_HASH}"
        )
    if compute_protocol_hash(build_baseline_protocol()) != protocol_hash:
        raise PhaseDClosureAuthorityError(
            "the committed protocol manifest and the protocol source disagree"
        )

    try:
        committed_interpretation = load_committed_selection_interpretation()
    except PhaseDSelectionInterpretationError as exc:
        raise PhaseDClosureAuthorityError(
            f"the committed selection interpretation is unusable: {exc}"
        ) from exc
    interpretation_hash = str(committed_interpretation.get("selection_interpretation_hash"))
    if interpretation_hash != ACCEPTED_SELECTION_INTERPRETATION_HASH:
        raise PhaseDClosureAuthorityError(
            f"the committed selection interpretation is {interpretation_hash}, not the accepted "
            f"{ACCEPTED_SELECTION_INTERPRETATION_HASH}"
        )
    content = committed_interpretation["content"]
    if content.get("interpretation_status") != ACCEPTED_INTERPRETATION_STATUS:
        raise PhaseDClosureAuthorityError(
            f"the committed interpretation status is {content.get('interpretation_status')!r}, "
            f"not {ACCEPTED_INTERPRETATION_STATUS!r}"
        )

    # the two authorities must AGREE, not merely sit side by side in the closure hash.
    if content.get("baseline_protocol_hash") != protocol_hash:
        raise PhaseDClosureAuthorityError(
            "the selection interpretation cites a different baseline protocol than the committed "
            "manifest"
        )
    if content.get("phase_d_plan_hash") != authority.plan_hash:
        raise PhaseDClosureAuthorityError(
            "the selection interpretation cites a different Phase-D plan than the verified "
            "finalist freeze derives"
        )
    if list(content.get("ordered_finalists", [])) != list(authority.ordered_config_hashes):
        raise PhaseDClosureAuthorityError(
            "the interpretation's finalists differ from the verified freeze, in value or order"
        )
    if dict(content.get("inherited_candidate_index", [])) != dict(
        authority.inherited_candidate_index
    ):
        raise PhaseDClosureAuthorityError(
            "the interpretation's inherited candidate indices differ from the verified freeze"
        )
    if content.get("final_selection_rule") != "SELECT_RANK_ZERO":
        raise PhaseDClosureAuthorityError(
            f"the committed final selection rule is {content.get('final_selection_rule')!r}"
        )
    return protocol_hash


def _read_closure_rows(conn: Connection) -> list[dict[str, Any]]:
    """Every VALIDATION Phase-D row the narrow surface exposes. No filter, no ordering trust."""
    from sqlalchemy import text

    return [
        dict(row)
        for row in conn.execute(
            text(f"SELECT * FROM {_CLOSURE_VIEW}")  # noqa: S608 - module constant, not input
        ).mappings()
    ]


def derive_l2f2_phase_d_closure() -> PhaseDClosure:
    """Close the Phase-D campaign. No argument, because no part of this is a caller's choice."""
    from minos_engine.storage.database import create_db_engine

    engine = create_db_engine()
    try:
        return _derive_with_trust(
            engine=engine,
            expected_database=PHASE_D_CLOSURE_DATABASE,
            expected_revision=PHASE_D_CLOSURE_REVISION,
        )
    finally:
        engine.dispose()


def _derive_with_trust(
    *, engine: Engine, expected_database: str, expected_revision: str
) -> PhaseDClosure:
    """The service core. Private; the store pins are parameters ONLY here.

    A scratch proof must drive this exact sequence against a scratch database. Neither pin is
    reachable from the public entry, which compiles them in, so widening them here does not widen
    the production trust surface.
    """
    with engine.connect() as conn:
        # ---- every scientific authority, in order, ALL of it before a score row exists -------
        _authorize_closure_connection(
            conn, database_name=expected_database, revision=expected_revision
        )
        authority = _frozen_phase_d_authority(_required_file(ENV_FINALIST_FREEZE_PATH))
        protocol_hash = _verify_committed_authorities(authority)

        # ---- only now: the outcomes ---------------------------------------------------------
        rows = _read_closure_rows(conn)

    return build_phase_d_closure(rows, authority=authority, baseline_protocol_hash=protocol_hash)
