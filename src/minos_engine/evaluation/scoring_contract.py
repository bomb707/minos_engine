"""L2-F2 scoring contract — the frozen identity of the Minos scoring semantics we reproduce.

This module owns *evaluation semantics only*. It deliberately contains no baseline-ranking
policy: the robust objective, tie-breaks and search budget (owner decisions D1-D8) belong to
L2-F2-B and must never enter ``scoring_contract_hash``, or a later objective change would
retroactively invalidate every stored evaluation.

The authority is the audited upstream commit recorded in
``manifests/l2f2_scoring_authority_v1.json``. Nothing here reads the upstream checkout at
runtime: CI has no ``minos_subnet`` clone, so the committed manifest plus the committed golden
parity fixture are the only authorities.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "SCORING_AUTHORITY_MANIFEST",
    "SCORING_CONTRACT_DOMAIN",
    "SCORING_CONTRACT_VERSION",
    "AdmissionCode",
    "ScoringAuthority",
    "ScoringContractError",
    "compute_scoring_contract_hash",
    "load_scoring_authority",
]

#: the local contract version. Bumping it starts a NEW evaluation compatibility domain; historical
#: evaluations stay valid under their own hash and are never rewritten.
SCORING_CONTRACT_VERSION = "l2f2-minos-scoring-v1"

#: domain separation for the contract identity.
SCORING_CONTRACT_DOMAIN = "minos:l2f2-scoring-contract:v1\n"

SCORING_AUTHORITY_MANIFEST = "manifests/l2f2_scoring_authority_v1.json"

#: the bounded admission vocabulary. It reproduces what the validator does with a score, so a
#: result the validator would have SKIPPED is never recorded as ``ADMITTED``.
AdmissionCode = Literal[
    "ADMITTED",
    "NONPOSITIVE_SCORE",
    "OUT_OF_RANGE_SCORE",
    "ZERO_INPUT_FINGERPRINT",
]

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScoringContractError(MinosEngineError):
    """The scoring authority manifest is absent, malformed or internally inconsistent."""


class ScoringAuthority(BaseModel):
    """The committed, independently audited identity of the scoring semantics we reproduce."""

    model_config = _STRICT

    schema_version: Literal["l2f2-scoring-authority-v1"] = "l2f2-scoring-authority-v1"
    upstream_repository: str = Field(min_length=1)
    upstream_commit: str = Field(min_length=40, max_length=40)
    scoring_py_sha256: str = Field(min_length=64, max_length=64)
    validator_py_sha256: str = Field(min_length=64, max_length=64)
    tool_params_py_sha256: str = Field(min_length=64, max_length=64)
    happy_image: str = Field(min_length=1)
    bcftools_image: str = Field(min_length=1)
    semantics: dict[str, Any]

    def contract_content(self) -> dict[str, Any]:
        """Exactly what ``scoring_contract_hash`` covers — semantics, never ranking policy."""
        return {
            "bcftools_image": self.bcftools_image,
            "contract_version": SCORING_CONTRACT_VERSION,
            "happy_image": self.happy_image,
            "scoring_py_sha256": self.scoring_py_sha256,
            "semantics": self.semantics,
            "tool_params_py_sha256": self.tool_params_py_sha256,
            "upstream_commit": self.upstream_commit,
            "upstream_repository": self.upstream_repository,
            "validator_py_sha256": self.validator_py_sha256,
        }


def _require_digest_pinned(image: str, *, label: str) -> None:
    """A container identity is only reproducible when it is pinned by digest, never by tag."""
    if "@sha256:" not in image:
        raise ScoringContractError(
            f"{label} image {image!r} is tag-pinned; the scoring authority requires an immutable "
            "@sha256 digest so an upstream retag cannot silently change evaluation semantics"
        )


def load_scoring_authority(root: Path | None = None) -> ScoringAuthority:
    """Read the committed authority manifest. Fails closed on anything unpinned."""
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = root or repository_root()
    path = base / SCORING_AUTHORITY_MANIFEST
    if not path.is_file():
        raise ScoringContractError(f"scoring authority manifest is missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringContractError(f"scoring authority manifest is not valid JSON: {exc}") from exc

    try:
        upstream = raw["upstream"]
        sources = upstream["source_sha256"]
        containers = raw["containers"]
        authority = ScoringAuthority(
            schema_version=raw["schema_version"],
            upstream_repository=upstream["repository"],
            upstream_commit=upstream["commit"],
            scoring_py_sha256=sources["utils/scoring.py"],
            validator_py_sha256=sources["neurons/validator.py"],
            tool_params_py_sha256=sources["templates/tool_params.py"],
            happy_image=containers["happy"],
            bcftools_image=containers["bcftools"],
            semantics=raw["semantics"],
        )
    except KeyError as exc:
        raise ScoringContractError(f"scoring authority manifest is missing {exc}") from exc

    _require_digest_pinned(authority.happy_image, label="hap.py")
    _require_digest_pinned(authority.bcftools_image, label="bcftools")
    return authority


def compute_scoring_contract_hash(authority: ScoringAuthority) -> str:
    """The domain-separated identity every evaluation is stored under.

    Two evaluations may only be compared when this hash matches: it binds the upstream scorer
    bytes, both container digests and the semantic rules. A future scorer change produces a new
    hash and therefore a new evaluation, never an overwrite.
    """
    return sha256_hex(
        SCORING_CONTRACT_DOMAIN.encode("utf-8") + canonical_json_bytes(authority.contract_content())
    )
