"""The PRODUCTION BASELINE-QUALIFIED observer. It owns the only door to a trusted result.

The trust boundary that matters is not "can a caller construct the wrapper" — it is "can a
caller decide what the wrapper says". An earlier revision had a module-private
``_mint_trusted(result)`` that wrapped ANY caller-built ``BaselineQualificationResult``, so a
caller could assert ``test_untouched=True``, ``scorer_source_identities_verified=True`` and a
TRAIN summary of its own invention, and reach the gate assembler without the source having
observed a single byte of real evidence. The private marker was a wrapper helper, not a
capability.

Here the mint token is reachable only from inside this module, and the ONLY code path that mints
is :func:`run_baseline_qualified_qualification`, which builds every field of the result from
observed evidence. There is no function anywhere that turns an arbitrary result into a trusted
one. A caller supplies operational roots and nothing else — no scores, no counts, no hashes, and
in particular no boolean claiming that something was verified.

Each observer below returns what it measured, and raises rather than reporting ``False`` when it
cannot measure: a qualification that cannot see its evidence must not produce a confident HOLD
either, because a HOLD is also a claim about the campaign.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.qualification.l2f2_baseline_qualified_contract import (
    ACCEPTED_BCFTOOLS_DIGEST,
    ACCEPTED_HAPPY_DIGEST,
    BaselineQualificationResult,
    TrainEvidenceSummary,
    candidate_design_identity,
    objective_identity,
)

__all__ = [
    "CLOSURE_AUTHORITY_SOURCE",
    "BaselineQualificationObservationError",
    "TrainEvidenceAuthorityMissing",
    "TrustedBaselineQualification",
    "observe_evidence_hashes",
    "observe_harness_prerequisite",
    "observe_source_provenance",
    "observe_test_seal",
    "observe_train_evidence",
    "run_baseline_qualified_qualification",
]

#: the accepted Phase-D closure authority source. A qualified source must descend it.
CLOSURE_AUTHORITY_SOURCE: Final = "b61e2adfb3f871b4e0a1738ae12c1b9f0b7f9130"

TRAIN_DATABASE: Final = "minos_l2f2_baseline"
TRAIN_REVISION: Final = "0020_l2f2_phase_c_execution"

#: the accepted split gate whose PASS establishes the TEST seal without opening TEST.
SPLIT_SEAL_GATE: Final = "gates/split-frozen-v2.json"
SPLIT_SEAL_GATE_HASH: Final = "6bd9f472720d56055e57ada0a6e955a8ab0b617a0fe849021a5b0ddfafd19392"
SPLIT_SEAL_CHECK: Final = "sealed_test_access_denied_passed"

ACCEPTED_MINOS_SUBNET_SHA: Final = "649bb92c6abccebde58a736a2b2af7fd77a701c1"
ACCEPTED_SCORING_PY_SHA256: Final = (
    "7b5aa187adda5978adc029abcd4c96b7b78eafeb9c5641153955175cd0b7b658"
)
ACCEPTED_VALIDATOR_PY_SHA256: Final = (
    "2ac0841231a58794097ba40d245f27eaa44e1bd1b66134a17dece96a1a37f33e"
)
ACCEPTED_TOOL_PARAMS_PY_SHA256: Final = (
    "6e9648fb6d6bda1ed5411eff01c38596cc869e2f7ae9e5de855e8413f10e0765"
)

ACCEPTED_EVIDENCE_SHA256: Final[dict[str, str]] = {
    "phase_d_activation_evidence": "e58fa267130f9671dc7bd7991a5ea15e16ff8edef80a5ed189270d74baa536a2",
    "phase_d_execution_evidence": "1ebc6aeaac7aaf7cd2323623ab7b110e0e4596b67376caebff08f1887a45e000",
    "phase_d_sentinel_evidence": "db8ebc4387b2a3a2f343fc17f0e23c24f0a8c12c11cb0980741193367764d637",
    "phase_d_complete_matrix_evidence": "35431e546b511ad3a802266d1de71991230119f7727770fc057c7f179c56f798",
    "phase_d_closure_artifact": "4eaf622baa5755829e936588003277aa277b9d999db089ddc2c94adae4bb9f89",
    "phase_d_closure_evidence": "90f0f53577c78ded8e876cad35ed30e4ba0ba784316635a0d424aebee2f6bb24",
}


class BaselineQualificationObservationError(MinosEngineError):
    """The production observer could not establish something it is required to observe."""


class TrainEvidenceAuthorityMissing(BaselineQualificationObservationError):
    """No accepted read-only boundary can derive the TRAIN evidence summary.

    Raised rather than worked around. Widening ``experiments.*`` for the evaluator, or migrating
    the scientifically closed TRAIN store, are both decisions for the owner — and §8 requires the
    gap be reported before either is taken.
    """


# --------------------------------------------------------------------------------------------
# the mint capability. Reachable from nowhere else.
# --------------------------------------------------------------------------------------------
class _MintToken:
    """Unforgeable in practice: the only instance never leaves this module."""

    __slots__ = ()


_MINT: Final = _MintToken()


class TrustedBaselineQualification:
    """A qualification result THIS module observed. Only it can exist as evidence of a PASS."""

    __slots__ = ("result",)

    result: BaselineQualificationResult

    def __init__(self, token: _MintToken, result: BaselineQualificationResult) -> None:
        if token is not _MINT:
            raise BaselineQualificationObservationError(
                "TrustedBaselineQualification is minted by the production qualifier alone; a "
                "caller-built qualification result can never assemble a PASS gate"
            )
        object.__setattr__(self, "result", result)


# --------------------------------------------------------------------------------------------
# observers
# --------------------------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise BaselineQualificationObservationError(
            f"git {' '.join(args)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def observe_source_provenance(root: Path) -> dict[str, Any]:
    """HEAD, its tree, worktree cleanliness and ancestry — measured, never supplied."""
    from minos_engine.qualification import git_tree

    if not git_tree.is_git_repo(root):
        raise BaselineQualificationObservationError(f"{root} is not a git repository")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    clean = _git(root, "status", "--porcelain=v1") == ""
    if not git_tree.is_commit(root, CLOSURE_AUTHORITY_SOURCE):
        raise BaselineQualificationObservationError(
            f"the accepted closure authority source {CLOSURE_AUTHORITY_SOURCE} is not present"
        )
    return {
        "qualified_source_git_sha": commit,
        "qualified_source_tree_sha": tree,
        "worktree_clean": clean,
        "descends_closure_authority_source": git_tree.is_ancestor(
            root, CLOSURE_AUTHORITY_SOURCE, commit
        ),
    }


def observe_harness_prerequisite(root: Path) -> dict[str, Any]:
    """Verify the committed HARNESS-READY gate through its own accepted offline verifier."""
    from minos_engine.qualification.l2f_harness_ready_runner import (
        verify_committed_harness_ready_gate,
    )

    outcome = verify_committed_harness_ready_gate(
        base_dir=root,
        gate_path="gates/harness-ready.json",
        qualification_path="reports/layer2/harness-ready-result.json",
    )
    gate = json.loads((root / "gates/harness-ready.json").read_text(encoding="utf-8"))
    return {
        "harness_ready_gate_hash": str(gate.get("gate_hash", "")),
        "harness_ready_qualification_hash": str(
            (gate.get("input_hashes") or {}).get("qualification_hash", "")
        ),
        "harness_ready_gate_verified": bool(outcome.get("ok", False)),
    }


def observe_scorer_authority(root: Path) -> dict[str, Any]:
    """Derive the scorer identities from the committed scoring authority manifest."""
    from minos_engine.evaluation.scoring_contract import (
        compute_scoring_contract_hash,
        load_scoring_authority,
    )

    authority = load_scoring_authority(root)
    contract = compute_scoring_contract_hash(authority)
    # every identity is compared to the accepted one; nothing is asserted by a caller.
    verified = (
        authority.happy.resolved_digest == ACCEPTED_HAPPY_DIGEST
        and authority.bcftools.resolved_digest == ACCEPTED_BCFTOOLS_DIGEST
        and authority.upstream_commit == ACCEPTED_MINOS_SUBNET_SHA
        # EXACT source identities. "three strings of length 64" is a shape test, not identity.
        and authority.scoring_py_sha256 == ACCEPTED_SCORING_PY_SHA256
        and authority.validator_py_sha256 == ACCEPTED_VALIDATOR_PY_SHA256
        and authority.tool_params_py_sha256 == ACCEPTED_TOOL_PARAMS_PY_SHA256
    )
    return {
        "scoring_contract_hash": contract,
        "minos_subnet_sha": authority.upstream_commit,
        "happy_resolved_digest": authority.happy.resolved_digest,
        "bcftools_resolved_digest": authority.bcftools.resolved_digest,
        "scorer_source_identities_verified": verified,
    }


def _verified_closure_content(closure_artifact: Path) -> dict[str, Any]:
    """The verified closure content, so the disjointness proof reads the SAME artifact."""
    from minos_engine.baseline.baseline_selected import verify_closure_artifact

    return verify_closure_artifact(closure_artifact)


def observe_closure_and_selected(root: Path, closure_artifact: Path) -> dict[str, Any]:
    """Verify the closure artifact and the committed freeze, and read the result FROM them."""
    from minos_engine.baseline.baseline_selected import (
        compute_baseline_selected_hash,
        load_committed_baseline_selected,
        verify_closure_artifact,
    )
    from minos_engine.baseline.phase_d_observations import (
        PhaseDClosure,
        compute_phase_d_closure_hash,
    )

    # verify_closure_artifact already recomputes and matches the closure hash; recompute it here
    # too so the value stored in the qualification comes from the artifact's own bytes rather
    # than from a constant that happens to sit beside them.
    content = verify_closure_artifact(closure_artifact)
    restored = dict(content)
    for field in ("candidates", "observations", "ordered_ranking"):
        if isinstance(restored.get(field), list):
            restored[field] = tuple(restored[field])
    closure_hash = compute_phase_d_closure_hash(PhaseDClosure.model_validate(restored))
    committed = load_committed_baseline_selected(root)
    selected = str(content["selected_config_hash"])
    winner = next(c for c in content["candidates"] if c["config_hash"] == selected)
    frozen = committed["content"]
    statistics_agree = (
        all(
            winner[field] == frozen[f"selected_{field}"]
            for field in ("cvar", "floor", "mean", "failure_rate", "objective")
        )
        and winner["mean_gatk_runtime_ms"] == frozen["selected_mean_gatk_runtime_ms"]
    )
    return {
        "baseline_selected_hash": str(committed["baseline_selected_hash"]),
        "baseline_selected_manifest_verified": (
            committed["baseline_selected_hash"] == compute_baseline_selected_hash()
        ),
        "phase_d_closure_hash": closure_hash,
        "phase_d_closure_artifact_sha256": sha256_hex(closure_artifact.read_bytes()),
        "closure_artifact_verified": True,
        "selection_interpretation_hash": str(content["selection_interpretation_hash"]),
        "baseline_protocol_hash": str(content["baseline_protocol_hash"]),
        "execution_environment_hash": str(content["execution_environment_hash"]),
        "selected_config_hash": selected,
        "selected_rank": int(winner["rank"]),
        "selected_inherited_candidate_index": int(winner["inherited_candidate_index"]),
        "selected_statistics_verified": statistics_agree,
        "seed_config_hash": str(content["seed_config_hash"]),
        "seed_rank": int(content["seed_rank"]),
        "candidate_count": int(content["candidate_count"]),
        "member_count": int(content["member_count"]),
        "observation_count": int(content["observation_count"]),
        "all_candidates_complete": all(
            c["observed_count"] == content["member_count"] for c in content["candidates"]
        ),
        "validation_infrastructure_incidents": sum(
            int(c["infrastructure_incident_count"]) for c in content["candidates"]
        ),
    }


def observe_protocol_identities(root: Path) -> dict[str, Any]:
    """Derive the objective and candidate-design identities from the committed protocol."""
    from minos_engine.baseline.protocol import load_committed_protocol

    content = dict((load_committed_protocol(root).get("content")) or {})
    return {
        "objective_identity": objective_identity(content),
        "candidate_design_identity": candidate_design_identity(content),
    }


def observe_test_seal(root: Path) -> dict[str, Any]:
    """Establish the TEST seal from accepted gate metadata, without opening TEST.

    No TEST identity is enumerated, no TEST path resolved, no TEST truth or feature value read.
    What is verified is that the accepted SPLIT-FROZEN-V2 gate PASSed with its sealed-access check
    true, and that this qualification performs no TEST operation of its own.
    """
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import load_gate, verify_gate_integrity

    gate = load_gate(root / SPLIT_SEAL_GATE)
    integrity = verify_gate_integrity(gate)
    sealed = (
        integrity.ok
        and gate.status is GateStatus.PASS
        and gate.gate_hash == SPLIT_SEAL_GATE_HASH
        and bool(gate.mandatory_checks.get(SPLIT_SEAL_CHECK))
    )
    return {
        "test_untouched": sealed,
        "test_seal_evidence": {
            "split_frozen_v2_gate_hash": gate.gate_hash,
            "sealed_check": SPLIT_SEAL_CHECK,
        },
    }


def observe_train_validation_disjointness(
    *, train_dataset_ids: frozenset[str], closure_content: dict[str, Any]
) -> bool:
    """A real set intersection: 50 TRAIN identities against the closure's 10 VALIDATION ones.

    The earlier implementation checked only that validation ids began with ``minos-chr``, which is
    a naming observation and not a disjointness proof at all. This compares the actual sets.

    No TEST identity is needed or enumerated: proving TRAIN and VALIDATION do not overlap says
    nothing about TEST and requires nothing from it.
    """
    validation = {str(o["dataset_id"]) for o in closure_content.get("observations") or ()}
    if len(validation) != 10:
        raise BaselineQualificationObservationError(
            f"the closure binds {len(validation)} VALIDATION members, expected 10"
        )
    if len(train_dataset_ids) != 50:
        raise BaselineQualificationObservationError(
            f"the TRAIN membership holds {len(train_dataset_ids)} identities, expected 50"
        )
    return train_dataset_ids.isdisjoint(validation)


def observe_evidence_hashes(evidence_paths: dict[str, Path]) -> dict[str, str]:
    """Rehash the accepted operational evidence and require the EXACT accepted six.

    Hashing whatever the caller happened to name would let a qualification bind six unrelated
    files. The key set and every digest must match the accepted map, so a substituted or renamed
    artifact is a refusal rather than a differently-shaped success.
    """
    missing = sorted(set(ACCEPTED_EVIDENCE_SHA256) - set(evidence_paths))
    extra = sorted(set(evidence_paths) - set(ACCEPTED_EVIDENCE_SHA256))
    if missing or extra:
        raise BaselineQualificationObservationError(
            f"operational evidence set is wrong: missing {missing}, unexpected {extra}"
        )
    observed: dict[str, str] = {}
    for name, path in evidence_paths.items():
        if path.is_symlink() or not path.is_file():
            raise BaselineQualificationObservationError(
                f"evidence artifact {name} at {path} is missing or a symlink"
            )
        digest = sha256_hex(path.read_bytes())
        if digest != ACCEPTED_EVIDENCE_SHA256[name]:
            raise BaselineQualificationObservationError(
                f"evidence artifact {name} hashes {digest}, expected "
                f"{ACCEPTED_EVIDENCE_SHA256[name]}"
            )
        observed[name] = digest
    return observed


def observe_train_evidence(*, database_url: str | None = None) -> TrainEvidenceSummary:
    """Derive the TRAIN evidence summary through the AUTHENTICATED observation surface.

    The evaluator never touches an ``experiments.*`` table here — it cannot, and a spy test proves
    this function issues no such statement. It calls one argument-free ``SECURITY DEFINER``
    function whose schema, arity, owner, definer flag, volatility, language, ``search_path``, ACL
    and body digest are all verified against the source-controlled definition before execution.

    The function returns SOURCE FACTS; the canonical set digests are computed here in Python so
    the identity in the qualification hash is produced by the same algorithm everywhere else uses,
    not by a SQL re-implementation of it.
    """
    from sqlalchemy import create_engine, text

    from minos_engine.qualification.l2f2_train_qualification_surface import (
        TRAIN_DATABASE,
        TRAIN_REVISION,
        TrainQualificationSurfaceError,
        observe,
    )

    if database_url is None:
        raise BaselineQualificationObservationError(
            "a TRAIN observation connection is required; the qualifier does not guess one"
        )
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            database = str(conn.execute(text("SELECT current_database()")).scalar_one())
            if database != TRAIN_DATABASE:
                raise BaselineQualificationObservationError(
                    f"the TRAIN observer refuses database {database!r}; it observes only "
                    f"{TRAIN_DATABASE!r}"
                )
            try:
                observed = observe(conn)
            except TrainQualificationSurfaceError as exc:
                raise BaselineQualificationObservationError(
                    f"the TRAIN observation surface is not authentic or not authorised: {exc}"
                ) from exc
    finally:
        engine.dispose()

    if observed.get("revision") != TRAIN_REVISION:
        raise BaselineQualificationObservationError(
            f"TRAIN revision is {observed.get('revision')!r}, expected {TRAIN_REVISION!r}"
        )

    contracts = list(observed.get("scoring_contract_hashes") or ())
    environments = list(observed.get("execution_environment_hashes") or ())
    if len(contracts) != 1 or len(environments) != 1:
        raise BaselineQualificationObservationError(
            f"TRAIN binds {len(contracts)} scoring contracts and {len(environments)} execution "
            "environments; exactly one of each is required"
        )
    summary = TrainEvidenceSummary(
        revision=str(observed["revision"]),
        plan_hashes=tuple(str(h) for h in observed["plan_hashes"]),
        logical_job_count=int(observed["logical_job_count"]),
        terminal_job_count=int(observed["terminal_job_count"]),
        nonterminal_job_count=int(observed["nonterminal_job_count"]),
        succeeded_without_evaluation=int(observed["succeeded_without_evaluation"]),
        evaluation_count=int(observed["evaluation_count"]),
        evaluation_failure_count=int(observed["evaluation_failure_count"]),
        evaluation_set_sha256=_set_digest(observed["evaluation_hashes"]),
        execution_failure_set_sha256=_set_digest(observed["execution_failure_job_keys"]),
        execution_failure_codes={
            str(k): int(v) for k, v in dict(observed["execution_failure_codes"]).items()
        },
        distinct_scoring_contracts=len(contracts),
        scoring_contract_hash=str(contracts[0]),
        distinct_execution_environments=len(environments),
        execution_environment_hash=str(environments[0]),
    )

    from minos_engine.qualification.l2f2_train_evidence import verify_train_evidence

    checks = verify_train_evidence(summary.as_observed())
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        raise BaselineQualificationObservationError(
            f"the observed TRAIN evidence fails {failed}; a qualification never proceeds on "
            "evidence it could not verify"
        )
    return summary


def observe_train_dataset_ids(*, database_url: str) -> frozenset[str]:
    """The 50 Phase-C TRAIN member ids, used transiently to prove disjointness.

    They are never persisted into the qualification: only the derived boolean is.
    """
    from sqlalchemy import create_engine

    from minos_engine.qualification.l2f2_train_qualification_surface import observe

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            observed = observe(conn)
    finally:
        engine.dispose()
    identities = frozenset(str(d) for d in observed.get("phase_c_dataset_ids") or ())
    if len(identities) != 50:
        raise BaselineQualificationObservationError(
            f"the Phase-C TRAIN membership holds {len(identities)} identities, expected 50"
        )
    return identities


def _set_digest(values: Any) -> str:
    """The canonical set identity: sha256 over the comma-joined, sorted members."""
    joined = ",".join(sorted(str(v) for v in values))
    return sha256_hex(joined.encode("utf-8"))


# --------------------------------------------------------------------------------------------
# the single production entry
# --------------------------------------------------------------------------------------------
def run_baseline_qualified_qualification(
    *,
    root: Path | None = None,
    closure_artifact: Path,
    evidence_paths: dict[str, Path],
    train_database_url: str | None = None,
) -> TrustedBaselineQualification:
    """Observe everything, then mint. The caller supplies locations and nothing else.

    No parameter carries a score, a count, a hash or a claim that something was verified: every
    such field below is measured by an observer above. If any observer cannot measure, this
    raises — a qualification that could not see its evidence must not report a confident HOLD
    either.
    """
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = root or repository_root()

    if train_database_url is None:
        raise BaselineQualificationObservationError(
            "a TRAIN observation connection is required; the qualifier does not guess one"
        )

    observed: dict[str, Any] = {}
    observed.update(observe_source_provenance(base))
    observed.update(observe_harness_prerequisite(base))
    observed.update(observe_protocol_identities(base))
    observed.update(observe_scorer_authority(base))

    closure_content = _verified_closure_content(closure_artifact)
    observed.update(observe_closure_and_selected(base, closure_artifact))
    observed.update(observe_test_seal(base))
    observed["evidence_sha256"] = observe_evidence_hashes(evidence_paths)

    # the TRAIN side, through the authenticated ephemeral surface
    observed["train"] = observe_train_evidence(database_url=train_database_url)
    train_ids = observe_train_dataset_ids(database_url=train_database_url)
    observed["train_and_validation_identities_disjoint"] = observe_train_validation_disjointness(
        train_dataset_ids=train_ids, closure_content=closure_content
    )

    return TrustedBaselineQualification(_MINT, BaselineQualificationResult(**observed))
