"""THE frozen four-finalist outcome, bound to the evidence that produced it.

L2-F2-E ended by writing one non-Git outcome artifact naming the exact four configurations that
L2-F2-F must validate. This module is the only way that outcome enters source: it loads the
artifact, verifies it against every identity the frozen protocol pins, and returns a typed value.

The point is not convenience. Four naked hashes in a constant would be indistinguishable from four
hashes someone preferred, so nothing here is accepted on its own authority:

* the artifact is verified by SHA-256 over its exact bytes, against a digest supplied by the
  caller — a byte that changes anywhere changes the digest and the load fails;
* the artifact must carry the Phase-C closure digest too, so the finalists cannot be detached from
  the completed 500-observation ledger that justified them;
* every scientific identity the artifact claims — protocol, Phase-B completion, Phase-C candidate
  set, Phase-C plan, parameter space, execution environment, scoring contract, MINOS_SUBNET — is
  compared to what the caller already holds, so a finalist set from some other search cannot be
  substituted;
* the four hashes must be four, distinct, ordered exactly as recorded, include the seed, and be
  drawn from the candidates the finished ledger left alive.

Every failure is a typed refusal. There is no "warn and continue" path, because a validation
campaign that ran the wrong four configurations would be indistinguishable from one that ran the
right four until someone re-derived the ranking months later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "FINALIST_FREEZE_SCHEMA",
    "FinalistFreeze",
    "FinalistFreezeError",
    "load_finalist_freeze",
    "verify_finalist_freeze_document",
]

FINALIST_FREEZE_SCHEMA = "l2f2-phase-c-validation-finalists-v1"

#: how many configurations L2-F2-F validates. The protocol fixes this; it is not a preference.
_FINALIST_COUNT = 4

#: the identities a finalist freeze must agree with before it may steer a validation campaign.
#: all are sha256 digests except the MINOS_SUBNET commit, which is a 40-hex git object name.
_BOUND_SHA256_IDENTITIES = (
    "baseline_protocol_hash",
    "phase_b_completion_hash",
    "phase_c_candidate_set_hash",
    "phase_c_plan_hash",
    "parameter_space_hash",
    "execution_environment_hash",
    "scoring_contract_hash",
)
_BOUND_GIT_IDENTITIES = ("minos_subnet_sha",)
_BOUND_IDENTITIES = _BOUND_SHA256_IDENTITIES + _BOUND_GIT_IDENTITIES


class FinalistFreezeError(MinosEngineError):
    """A frozen finalist outcome is missing, altered, or does not describe this search."""


@dataclass(frozen=True, slots=True)
class FinalistFreeze:
    """The verified four-finalist outcome. Identity only — no score, no ranking input."""

    schema: str
    artifact_path: str
    artifact_sha256: str
    phase_c_closure_sha256: str
    baseline_protocol_hash: str
    phase_b_completion_hash: str
    phase_c_candidate_set_hash: str
    phase_c_plan_hash: str
    parameter_space_hash: str
    execution_environment_hash: str
    scoring_contract_hash: str
    minos_subnet_sha: str
    ordered_finalists: tuple[str, ...]
    seed_config_hash: str
    inherited_candidate_index: dict[str, int]
    finished_ledger_alive: tuple[str, ...]
    finished_ledger_eliminated: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.ordered_finalists) != _FINALIST_COUNT:  # pragma: no cover - built verified
            raise FinalistFreezeError("a verified freeze always carries exactly four finalists")

    @property
    def finalist_count(self) -> int:
        return len(self.ordered_finalists)

    def inherited_index_of(self, config_hash: str) -> int:
        """The inherited Phase-B design position — the scientific index, never the local one."""
        try:
            return self.inherited_candidate_index[config_hash]
        except KeyError:  # pragma: no cover - callers iterate ordered_finalists
            raise FinalistFreezeError(
                f"{config_hash} is not a finalist of this frozen outcome"
            ) from None


def _require(document: Any, key: str) -> Any:
    if not isinstance(document, dict) or key not in document:
        raise FinalistFreezeError(f"the finalist freeze is missing {key!r}")
    return document[key]


def _require_digest(document: Any, key: str, *, length: int, label: str) -> str:
    value = _require(document, key)
    if not isinstance(value, str) or len(value) != length:
        raise FinalistFreezeError(f"{key!r} is not {label}")
    try:
        int(value, 16)
    except ValueError:
        raise FinalistFreezeError(f"{key!r} is not {label}") from None
    return value


def _require_hash(document: Any, key: str) -> str:
    return _require_digest(document, key, length=64, label="a sha256 digest")


def _require_git_sha(document: Any, key: str) -> str:
    return _require_digest(document, key, length=40, label="a git object name")


def verify_finalist_freeze_document(
    document: Any,
    *,
    artifact_path: str,
    artifact_sha256: str,
    expected_artifact_sha256: str,
    expected_phase_c_closure_sha256: str | None = None,
    expected_identities: dict[str, str] | None = None,
    expected_finalists: tuple[str, ...] | None = None,
) -> FinalistFreeze:
    """Verify a already-parsed freeze document. Separated so tests need no filesystem.

    ``artifact_sha256`` is what the bytes actually hashed to; ``expected_artifact_sha256`` is what
    the caller was told to expect. They are compared here rather than by the caller so that no code
    path can load a freeze without the comparison happening.
    """
    if artifact_sha256 != expected_artifact_sha256:
        raise FinalistFreezeError(
            f"the finalist freeze at {artifact_path} hashes to {artifact_sha256}, but "
            f"{expected_artifact_sha256} was expected; its bytes have changed"
        )

    schema = _require(document, "schema")
    if schema != FINALIST_FREEZE_SCHEMA:
        raise FinalistFreezeError(
            f"the finalist freeze declares schema {schema!r}, not {FINALIST_FREEZE_SCHEMA!r}"
        )

    closure = _require(document, "phase_c_closure_artifact")
    if not isinstance(closure, dict):
        raise FinalistFreezeError("phase_c_closure_artifact is not an object")
    closure_sha = _require_hash(closure, "sha256")
    if (
        expected_phase_c_closure_sha256 is not None
        and closure_sha != expected_phase_c_closure_sha256
    ):
        raise FinalistFreezeError(
            f"the finalist freeze binds Phase-C closure {closure_sha}, but "
            f"{expected_phase_c_closure_sha256} was expected; these finalists were not derived "
            "from the completed ledger this campaign holds"
        )

    identities = {name: _require_hash(document, name) for name in _BOUND_SHA256_IDENTITIES}
    identities.update({name: _require_git_sha(document, name) for name in _BOUND_GIT_IDENTITIES})
    for name, expected in (expected_identities or {}).items():
        if name not in identities:
            raise FinalistFreezeError(f"cannot bind unknown identity {name!r}")
        if identities[name] != expected:
            raise FinalistFreezeError(
                f"the finalist freeze binds {name} {identities[name]}, but this campaign holds "
                f"{expected}; it belongs to a different search"
            )

    ordered = _require(document, "validation_finalists_ordered")
    if not isinstance(ordered, list) or not all(isinstance(h, str) for h in ordered):
        raise FinalistFreezeError("validation_finalists_ordered is not a list of hashes")
    finalists = tuple(ordered)
    if len(finalists) != _FINALIST_COUNT:
        raise FinalistFreezeError(
            f"a frozen validation outcome names exactly {_FINALIST_COUNT} finalists, "
            f"found {len(finalists)}"
        )
    if len(set(finalists)) != _FINALIST_COUNT:
        raise FinalistFreezeError("the frozen finalist list repeats a configuration")
    declared_count = document.get("validation_finalist_count")
    if declared_count is not None and declared_count != _FINALIST_COUNT:
        raise FinalistFreezeError(
            f"the freeze declares {declared_count} finalists but the protocol fixes "
            f"{_FINALIST_COUNT}"
        )
    if expected_finalists is not None and finalists != tuple(expected_finalists):
        raise FinalistFreezeError(
            "the frozen finalist tuple does not match the expected one, in value or in order; "
            "the finalist set is an input to validation, never an output of it"
        )

    seed = _require_hash(document, "seed_config_hash")
    if seed not in finalists:
        raise FinalistFreezeError(
            "the seed is not among the frozen finalists; the frozen promotion rule never drops it"
        )

    alive = tuple(_require(document, "finished_ledger_alive_hashes"))
    eliminated = tuple(_require(document, "finished_ledger_eliminated_hashes"))
    if set(alive) & set(eliminated):
        raise FinalistFreezeError("a configuration is recorded as both alive and eliminated")
    for config_hash in finalists:
        if config_hash in set(eliminated):
            raise FinalistFreezeError(
                f"finalist {config_hash} is recorded as eliminated by the finished ledger; an "
                "eliminated configuration is never validated"
            )
        if config_hash not in set(alive):
            raise FinalistFreezeError(
                f"finalist {config_hash} is not among the candidates the finished ledger left alive"
            )

    detail = document.get("finalist_detail") or []
    index: dict[str, int] = {}
    for entry in detail:
        if not isinstance(entry, dict):
            continue
        config_hash = entry.get("config_hash")
        inherited = entry.get("inherited_phase_b_index")
        if isinstance(config_hash, str) and isinstance(inherited, int):
            index[config_hash] = inherited
    missing = [h for h in finalists if h not in index]
    if missing:
        raise FinalistFreezeError(
            f"the freeze does not record an inherited Phase-B index for {len(missing)} finalist(s)"
        )

    return FinalistFreeze(
        schema=schema,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        phase_c_closure_sha256=closure_sha,
        ordered_finalists=finalists,
        seed_config_hash=seed,
        inherited_candidate_index={h: index[h] for h in finalists},
        finished_ledger_alive=alive,
        finished_ledger_eliminated=eliminated,
        **identities,
    )


def load_finalist_freeze(
    path: str | Path,
    *,
    expected_artifact_sha256: str,
    expected_phase_c_closure_sha256: str | None = None,
    expected_identities: dict[str, str] | None = None,
    expected_finalists: tuple[str, ...] | None = None,
) -> FinalistFreeze:
    """Load and verify the frozen finalist outcome from disk. Read-only; never rewrites it."""
    artifact = Path(path)
    try:
        raw = artifact.read_bytes()
    except OSError as exc:
        raise FinalistFreezeError(
            f"the finalist freeze at {artifact} is unreadable: {exc}"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalistFreezeError(f"the finalist freeze at {artifact} is not JSON: {exc}") from exc
    return verify_finalist_freeze_document(
        document,
        artifact_path=str(artifact),
        artifact_sha256=digest,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_phase_c_closure_sha256=expected_phase_c_closure_sha256,
        expected_identities=expected_identities,
        expected_finalists=expected_finalists,
    )
