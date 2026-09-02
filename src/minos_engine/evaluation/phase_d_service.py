"""THE production Phase-D evaluation service entry: one argument, everything else derived.

``evaluate_validation_execution`` is a COMPONENT seam. Its caller supplies the engine, the
scoring authority, the oracle, the publisher and the provisioning, which is exactly right for
composition and for tests — and exactly wrong for a production service, because every one of
those is a scientific authority a caller would then be trusted to nominate. A caller who can hand
in a ``ScoringAuthority`` can decide what "score" means.

So this module sits above it. The public entry accepts one thing — which of the already-frozen
executions to evaluate — and constructs the rest from pinned sources:

* the database and its revision, proven on the connection itself;
* the evaluator SERVICE principal, proven by ``session_user`` rather than ``current_user``, so an
  already-issued ``SET ROLE`` cannot disguise who logged in;
* the EXACT Phase-D campaign the execution belongs to;
* the scoring authority, loaded from the pinned contract and required to equal the frozen hash;
* the MINOS_SUBNET oracle, built only from the verified checkout;
* every filesystem root, resolved from provisioned environment and validated, never created here.

Order is the point
------------------
Every one of those is established BEFORE a truth path is constructed. Truth is the answer key:
an execution that fails authorization must produce zero truth opens, zero scoring subprocesses,
zero metrics artifacts and zero ledger rows — and in particular no evaluation FAILURE row either,
because an authorization refusal is an operator error, not a candidate's scientific outcome.

Which campaign, not which partition
-----------------------------------
Partition alone is necessary and not sufficient. Two validation plans over the same ten members
and the same four configurations are indistinguishable by partition, member and config; only the
plan identity separates them. ``0025`` exposes exactly that one fact to the evaluator, and this
module requires it to equal the frozen Phase-D plan. The partition gate stays where it is — the
two are separate layers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

    from minos_engine.evaluation.orchestrator import EvaluationOutcome

__all__ = [
    "ENV_EVALUATION_PRACTICE_ROOT",
    "ENV_EVALUATION_REFERENCE_ROOT",
    "ENV_EVALUATION_WORK_ROOT",
    "ENV_FINALIST_FREEZE_PATH",
    "PHASE_D_EVALUATOR_DATABASE",
    "PHASE_D_EVALUATOR_REVISION",
    "PhaseDEvaluatorAuthorityError",
    "authorize_validation_evaluator_connection",
    "evaluate_l2f2_phase_d_execution",
]

#: the store this service evaluates, and the revision that exposes the Phase-D authority view.
PHASE_D_EVALUATOR_DATABASE = "minos_l2f2_validation"
PHASE_D_EVALUATOR_REVISION = "0025_l2f2_phase_d_eval_auth"

#: the ONE authority fact ``0025`` exposes to a least-privilege evaluator.
_AUTHORITY_VIEW = "evaluation.l2f_phase_d_execution_authority"

_REQUIRED_MEMBERSHIP = "minos_evaluator"
_FORBIDDEN_MEMBERSHIPS = frozenset({"minos_admin", "minos_runner", "minos_trainer", "minos_live"})

#: the frozen scoring identity. Phase D must confirm the finalists under the semantics that
#: SELECTED them; a newer scorer would silently reinterpret the campaign.
ACCEPTED_SCORING_CONTRACT_HASH = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
ACCEPTED_MINOS_SUBNET_COMMIT = "649bb92c6abccebde58a736a2b2af7fd77a701c1"

#: provisioned roots. Paths only — none of these enters any scientific identity.
ENV_EVALUATION_PRACTICE_ROOT = "MINOS_L2F2_EVALUATION_PRACTICE_ROOT"
ENV_EVALUATION_REFERENCE_ROOT = "MINOS_L2F2_EVALUATION_REFERENCE_ROOT"
ENV_EVALUATION_WORK_ROOT = "MINOS_L2F2_EVALUATION_WORK_ROOT"
#: the frozen finalist artifact the Phase-D plan hash is DERIVED from, so the plan identity is
#: never a literal duplicated across modules.
ENV_FINALIST_FREEZE_PATH = "MINOS_L2F2_FINALIST_FREEZE_PATH"


class PhaseDEvaluatorAuthorityError(MinosEngineError):
    """This connection, principal, campaign or runtime may not evaluate Phase D."""


# ------------------------------------------------------------------------------------------- #
# 1. the connection itself
# ------------------------------------------------------------------------------------------- #
def authorize_validation_evaluator_connection(conn: Connection) -> None:
    """Authorize THIS EXACT connection before any scientific access.

    The evaluator counterpart of the runner boundary, and identical in strength: database,
    revision, and the SESSION principal's authority and memberships. ``session_user`` is used
    deliberately rather than ``current_user`` — an already-issued ``SET ROLE`` must not be able to
    disguise which principal actually logged in, so authenticating as admin and assuming the
    evaluator role does not pass.
    """
    _authorize_evaluator_connection(
        conn, database_name=PHASE_D_EVALUATOR_DATABASE, revision=PHASE_D_EVALUATOR_REVISION
    )


def _authorize_evaluator_connection(conn: Connection, *, database_name: str, revision: str) -> None:
    """The single boundary body. The store pins are parameters so the scratch proof can drive
    this exact sequence against a scratch database without a mutable module global."""
    from sqlalchemy import text

    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != database_name:
        raise PhaseDEvaluatorAuthorityError(
            f"the Phase-D evaluator refuses database {database!r}; it evaluates only against "
            f"{database_name!r}"
        )
    live = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live != revision:
        raise PhaseDEvaluatorAuthorityError(
            f"validation database revision is {live!r}, expected {revision!r}; the Phase-D "
            "authority surface is unavailable"
        )

    principal = conn.execute(
        text(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
            "  FROM pg_roles WHERE rolname = session_user"
        )
    ).one_or_none()
    if principal is None:  # pragma: no cover - a session always has a principal
        raise PhaseDEvaluatorAuthorityError("the session principal could not be resolved")
    name, can_login, is_super, createdb, createrole, bypassrls = principal
    if not can_login:
        raise PhaseDEvaluatorAuthorityError(f"session principal {name!r} cannot log in")
    for label, elevated in (
        ("SUPERUSER", is_super),
        ("CREATEDB", createdb),
        ("CREATEROLE", createrole),
        ("BYPASSRLS", bypassrls),
    ):
        if elevated:
            raise PhaseDEvaluatorAuthorityError(
                f"session principal {name!r} holds {label}; the evaluator must be least-privilege"
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
        raise PhaseDEvaluatorAuthorityError(
            f"session principal {name!r} holds MINOS memberships {sorted(memberships)}; the "
            f"Phase-D evaluator requires exactly {{{_REQUIRED_MEMBERSHIP!r}}}"
        )
    forbidden = memberships & _FORBIDDEN_MEMBERSHIPS
    if forbidden:  # pragma: no cover - unreachable given the equality above
        raise PhaseDEvaluatorAuthorityError(f"session principal holds {sorted(forbidden)}")


# ------------------------------------------------------------------------------------------- #
# 2. which campaign this execution belongs to
# ------------------------------------------------------------------------------------------- #
def _frozen_phase_d_plan_hash(freeze_path: str | Path) -> str:
    """DERIVE the frozen plan hash from the finalist artifact, never a duplicated literal."""
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
    return build_l2f2_phase_d_authority(freeze).plan_hash


def _require_exact_phase_d_execution(
    conn: Connection, *, execution_result_id: str, expected_plan_hash: str
) -> None:
    """The execution must belong to THE frozen Phase-D campaign, not merely look like one.

    Read through ``0025``'s narrow view, which is the only surface exposing plan identity to this
    principal and which is validation-only by construction — so a TRAIN or TEST execution is not a
    row that resolves here at all.
    """
    from sqlalchemy import text

    rows = conn.execute(
        text(  # noqa: S608 - the view name is a module constant, not caller input
            f"SELECT plan_hash FROM {_AUTHORITY_VIEW} WHERE execution_result_id = :e"
        ),
        {"e": execution_result_id},
    ).all()
    if len(rows) != 1:
        raise PhaseDEvaluatorAuthorityError(
            f"execution {execution_result_id} resolves to {len(rows)} Phase-D authority rows; it "
            "is not a completed VALIDATION execution of this store"
        )
    observed = str(rows[0][0])
    if observed != expected_plan_hash:
        raise PhaseDEvaluatorAuthorityError(
            f"execution {execution_result_id} belongs to plan {observed}, not the frozen Phase-D "
            f"campaign {expected_plan_hash}; validation confirms one campaign's finalists and "
            "must not attribute another campaign's executions to it"
        )


# ------------------------------------------------------------------------------------------- #
# 3. the scientific authority, all of it derived
# ------------------------------------------------------------------------------------------- #
def _require_scoring_authority() -> Any:
    """Load the PINNED scoring authority and require it to be this campaign's contract."""
    from minos_engine.evaluation.scoring_contract import (
        compute_scoring_contract_hash,
        load_scoring_authority,
    )

    authority = load_scoring_authority()
    contract = compute_scoring_contract_hash(authority)
    if contract != ACCEPTED_SCORING_CONTRACT_HASH:
        raise PhaseDEvaluatorAuthorityError(
            f"the loaded scoring contract is {contract}, not this campaign's "
            f"{ACCEPTED_SCORING_CONTRACT_HASH}; Phase D confirms the finalists under the scoring "
            "semantics that selected them and must not silently reinterpret them"
        )
    if authority.upstream_commit != ACCEPTED_MINOS_SUBNET_COMMIT:
        raise PhaseDEvaluatorAuthorityError(
            f"the scoring authority pins MINOS_SUBNET {authority.upstream_commit}, not "
            f"{ACCEPTED_MINOS_SUBNET_COMMIT}"
        )
    return authority


def _required_directory(variable: str) -> Path:
    """A provisioned root. Validated, never created here: the evaluator provisions nothing."""
    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise PhaseDEvaluatorAuthorityError(
            f"{variable} is not set; the Phase-D evaluation roots are operational provisioning"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise PhaseDEvaluatorAuthorityError(f"{variable} must be absolute, got {path}")
    if path.is_symlink() or not path.is_dir():
        raise PhaseDEvaluatorAuthorityError(
            f"{variable} must be an existing non-symlink directory, got {path}"
        )
    return path


def _required_file(variable: str) -> Path:
    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise PhaseDEvaluatorAuthorityError(f"{variable} is not set")
    path = Path(raw)
    if path.is_symlink() or not path.is_file():
        raise PhaseDEvaluatorAuthorityError(
            f"{variable} must be an existing non-symlink file, got {path}"
        )
    return path


# ------------------------------------------------------------------------------------------- #
# 4. THE production entry
# ------------------------------------------------------------------------------------------- #
def evaluate_l2f2_phase_d_execution(*, execution_result_id: str) -> EvaluationOutcome:
    """Evaluate ONE already-frozen Phase-D execution. The only choice is which one.

    Selecting among forty executions that already exist does not define scoring semantics, so it
    is the single accepted argument. Everything that DOES define semantics — the store, the
    revision, the service principal, the campaign, the scoring contract, the pinned scorer, the
    runtime images, every filesystem root — is derived here from pinned sources, and all of it is
    established before a truth path is constructed.
    """
    from minos_engine.storage.database import create_db_engine

    engine = create_db_engine()
    try:
        return _evaluate_with_trust(
            engine=engine,
            execution_result_id=execution_result_id,
            expected_database=PHASE_D_EVALUATOR_DATABASE,
            expected_revision=PHASE_D_EVALUATOR_REVISION,
        )
    finally:
        engine.dispose()


def _evaluate_with_trust(
    *,
    engine: Engine,
    execution_result_id: str,
    expected_database: str,
    expected_revision: str,
    oracle: Any = None,
    authority: Any = None,
) -> EvaluationOutcome:
    """The service core. Private; store identity, oracle and authority are parameters ONLY here.

    A scratch proof needs to drive this exact sequence against a scratch database with a
    deterministic oracle and a synthetic pinned scorer — the real one confirms the real finalists
    and must not be spent on a fixture. None of the three is reachable from the public entry,
    which compiles the store in, loads the pinned authority itself and builds the oracle from the
    verified checkout, so widening them here does not widen the production trust surface.
    """
    from minos_engine.evaluation.artifact_publisher import (
        EvaluationArtifactPublisher,
        evaluation_artifact_root_from_env,
    )
    from minos_engine.evaluation.minos_subnet_oracle import MinosSubnetOracle
    from minos_engine.evaluation.orchestrator import EvaluationProvisioning
    from minos_engine.evaluation.validation_orchestrator import evaluate_validation_execution

    # ---- authority, in order, all of it before any truth path exists -----------------------
    with engine.connect() as conn:
        _authorize_evaluator_connection(
            conn, database_name=expected_database, revision=expected_revision
        )
        expected_plan_hash = _frozen_phase_d_plan_hash(_required_file(ENV_FINALIST_FREEZE_PATH))
        _require_exact_phase_d_execution(
            conn,
            execution_result_id=execution_result_id,
            expected_plan_hash=expected_plan_hash,
        )

    resolved_authority = authority if authority is not None else _require_scoring_authority()
    provisioning = EvaluationProvisioning(
        practice_dataset_root=_required_directory(ENV_EVALUATION_PRACTICE_ROOT),
        reference_root=_required_directory(ENV_EVALUATION_REFERENCE_ROOT),
        work_dir=_required_directory(ENV_EVALUATION_WORK_ROOT),
    )
    publisher = EvaluationArtifactPublisher(evaluation_artifact_root_from_env())
    resolved_oracle = (
        oracle if oracle is not None else MinosSubnetOracle.from_env(resolved_authority)
    )

    # ---- only now: the audited component seam, which opens truth ---------------------------
    return evaluate_validation_execution(
        engine,
        execution_result_id=execution_result_id,
        authority=resolved_authority,
        oracle=resolved_oracle,
        publisher=publisher,
        provisioning=provisioning,
    )
