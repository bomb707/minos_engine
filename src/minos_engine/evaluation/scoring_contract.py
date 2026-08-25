"""L2-F2 scoring contract — the frozen identity of the MINOS_SUBNET scoring authority.

This module owns *the scientific score authority only*. Two exclusions are load-bearing:

* **No ranking policy.** The robust objective, tie-breaks and search budget (protocol decisions
  D1-D8) belong to L2-F2-B and must never enter ``scoring_contract_hash``, or a later objective
  change would retroactively invalidate every stored evaluation.
* **No MINOS_ENGINE persistence envelope.** The metrics artifact schema, the evaluation-hash
  domain, the database media type and the migration revision are how *this repository* stores a
  result. They are not Minos scoring semantics, and binding them into the scientific contract
  makes the contract claim things about MINOS_SUBNET that MINOS_SUBNET never said.

That second exclusion is why there are two contract versions.

``l2f2-minos-scoring-v1`` embedded ``metrics_artifact_schema`` in its semantics. When production
moved to metrics artifact ``v2`` the v1 contract hash became an internally false statement: it
asserted a v1 envelope for rows written in a v2 envelope. ``l2f2-minos-scoring-v2`` removes the
envelope from the scientific contract entirely and adds what genuinely does belong — the literal
container references the pinned upstream source uses, and the immutable content each must resolve
to before a real score is produced.

The v1 authority and its hash are preserved unchanged and remain recomputable for audit; no
evaluation was ever persisted under either version, so nothing scientific is reinterpreted.

Nothing here reads the upstream checkout at runtime: CI has no ``minos_subnet`` clone, so the
committed manifests plus the committed golden parity fixture are the only authorities. Verifying
that a *live* checkout matches this authority is
:mod:`minos_engine.evaluation.minos_subnet_oracle`'s job.
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
    "SCORING_AUTHORITY_MANIFEST_V1",
    "SCORING_CONTRACT_DOMAIN",
    "SCORING_CONTRACT_DOMAIN_V1",
    "SCORING_CONTRACT_VERSION",
    "SCORING_CONTRACT_VERSION_V1",
    "AdmissionCode",
    "HistoricalScoringAuthorityV1",
    "RuntimeImageIdentity",
    "ScoringAuthority",
    "ScoringContractError",
    "compute_scoring_contract_hash",
    "load_historical_scoring_authority_v1",
    "load_scoring_authority",
]

#: THE production contract version. Bumping it starts a NEW evaluation compatibility domain;
#: evaluations stay valid under their own hash and are never rewritten.
SCORING_CONTRACT_VERSION = "l2f2-minos-scoring-v2"
SCORING_CONTRACT_DOMAIN = "minos:l2f2-scoring-contract:v2\n"
SCORING_AUTHORITY_MANIFEST = "manifests/l2f2_scoring_authority_v2.json"

#: the superseded version, retained so its hash stays recomputable for audit. It is never the
#: production default and no new evaluation is written under it.
SCORING_CONTRACT_VERSION_V1 = "l2f2-minos-scoring-v1"
SCORING_CONTRACT_DOMAIN_V1 = "minos:l2f2-scoring-contract:v1\n"
SCORING_AUTHORITY_MANIFEST_V1 = "manifests/l2f2_scoring_authority_v1.json"

#: the bounded admission vocabulary. It records what the validator does with a score, so a result
#: the validator would have SKIPPED is never stored as ``ADMITTED``.
AdmissionCode = Literal[
    "ADMITTED",
    "NONPOSITIVE_SCORE",
    "OUT_OF_RANGE_SCORE",
    "ZERO_INPUT_FINGERPRINT",
]

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScoringContractError(MinosEngineError):
    """The scoring authority manifest is absent, malformed or internally inconsistent."""


class RuntimeImageIdentity(BaseModel):
    """One container the pinned upstream scorer runs, under BOTH of its identities.

    ``upstream_ref`` is the literal string the pinned source itself uses — reproduced verbatim
    and never rewritten. At the pinned commit hap.py is digest-pinned there and bcftools is
    tag-pinned; MINOS_ENGINE does not "fix" the tag, because doing so would change the command
    upstream constructs.

    ``resolved_digest`` is the immutable content that reference must resolve to locally before a
    real score is produced. It is a *precondition MINOS_ENGINE verifies*, never a substitution.
    """

    model_config = _STRICT

    upstream_ref: str = Field(min_length=1)
    resolved_digest: str = Field(min_length=1)

    @property
    def upstream_ref_is_digest_pinned(self) -> bool:
        return "@sha256:" in self.upstream_ref


class ScoringAuthority(BaseModel):
    """THE committed, independently audited identity of the MINOS_SUBNET scoring authority."""

    model_config = _STRICT

    schema_version: Literal["l2f2-scoring-authority-v2"] = "l2f2-scoring-authority-v2"
    upstream_repository: str = Field(min_length=1)
    upstream_commit: str = Field(min_length=40, max_length=40)
    scoring_py_sha256: str = Field(min_length=64, max_length=64)
    validator_py_sha256: str = Field(min_length=64, max_length=64)
    tool_params_py_sha256: str = Field(min_length=64, max_length=64)
    happy: RuntimeImageIdentity
    bcftools: RuntimeImageIdentity
    semantics: dict[str, Any]

    @property
    def contract_domain(self) -> str:
        return SCORING_CONTRACT_DOMAIN

    def contract_content(self) -> dict[str, Any]:
        """Exactly what ``scoring_contract_hash`` covers.

        Upstream source identity, the containers that source runs under both identities, and the
        scientific semantics. Nothing about how MINOS_ENGINE stores the result.
        """
        return {
            "bcftools_resolved_digest": self.bcftools.resolved_digest,
            "bcftools_upstream_ref": self.bcftools.upstream_ref,
            "contract_version": SCORING_CONTRACT_VERSION,
            "happy_resolved_digest": self.happy.resolved_digest,
            "happy_upstream_ref": self.happy.upstream_ref,
            "scoring_py_sha256": self.scoring_py_sha256,
            "semantics": self.semantics,
            "tool_params_py_sha256": self.tool_params_py_sha256,
            "upstream_commit": self.upstream_commit,
            "upstream_repository": self.upstream_repository,
            "validator_py_sha256": self.validator_py_sha256,
        }


class HistoricalScoringAuthorityV1(BaseModel):
    """The superseded v1 authority, frozen exactly as committed. Audit only.

    Its ``contract_content`` is byte-for-byte the v1 definition — including the
    ``metrics_artifact_schema`` entry inside ``semantics`` that made v1 unsuitable once the
    persistence envelope moved on. It is reproduced rather than corrected so the historical hash
    stays recomputable; correcting it in place would silently redefine a published identity.
    """

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

    @property
    def contract_domain(self) -> str:
        return SCORING_CONTRACT_DOMAIN_V1

    def contract_content(self) -> dict[str, Any]:
        return {
            "bcftools_image": self.bcftools_image,
            "contract_version": SCORING_CONTRACT_VERSION_V1,
            "happy_image": self.happy_image,
            "scoring_py_sha256": self.scoring_py_sha256,
            "semantics": self.semantics,
            "tool_params_py_sha256": self.tool_params_py_sha256,
            "upstream_commit": self.upstream_commit,
            "upstream_repository": self.upstream_repository,
            "validator_py_sha256": self.validator_py_sha256,
        }


def _require_digest_pinned(image: str, *, label: str) -> None:
    """A container's CONTENT identity is only reproducible when pinned by digest."""
    if "@sha256:" not in image:
        raise ScoringContractError(
            f"{label} resolved identity {image!r} is tag-pinned; the scoring authority requires "
            "an immutable @sha256 digest so an upstream retag cannot silently change evaluation "
            "semantics"
        )


def _read_manifest(root: Path | None, relative: str) -> dict[str, Any]:
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    path = (root or repository_root()) / relative
    if not path.is_file():
        raise ScoringContractError(f"scoring authority manifest is missing: {path}")
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringContractError(f"scoring authority manifest is not valid JSON: {exc}") from exc
    return raw


def load_scoring_authority(root: Path | None = None) -> ScoringAuthority:
    """Read THE committed production authority. Fails closed on anything unpinned."""
    raw = _read_manifest(root, SCORING_AUTHORITY_MANIFEST)
    try:
        upstream = raw["upstream"]
        sources = upstream["source_sha256"]
        runtime = raw["runtime"]
        authority = ScoringAuthority(
            schema_version=raw["schema_version"],
            upstream_repository=upstream["repository"],
            upstream_commit=upstream["commit"],
            scoring_py_sha256=sources["utils/scoring.py"],
            validator_py_sha256=sources["neurons/validator.py"],
            tool_params_py_sha256=sources["templates/tool_params.py"],
            happy=RuntimeImageIdentity(**runtime["happy"]),
            bcftools=RuntimeImageIdentity(**runtime["bcftools"]),
            semantics=raw["semantics"],
        )
    except KeyError as exc:
        raise ScoringContractError(f"scoring authority manifest is missing {exc}") from exc

    _require_digest_pinned(authority.happy.resolved_digest, label="hap.py")
    _require_digest_pinned(authority.bcftools.resolved_digest, label="bcftools")
    if "metrics_artifact_schema" in authority.semantics:
        raise ScoringContractError(
            "the scientific scoring contract must not bind MINOS_ENGINE's metrics artifact "
            "schema; that is a persistence envelope, not a Minos scoring semantic"
        )
    return authority


def load_historical_scoring_authority_v1(
    root: Path | None = None,
) -> HistoricalScoringAuthorityV1:
    """Read the superseded v1 authority so its published hash stays recomputable."""
    raw = _read_manifest(root, SCORING_AUTHORITY_MANIFEST_V1)
    try:
        upstream = raw["upstream"]
        sources = upstream["source_sha256"]
        containers = raw["containers"]
        return HistoricalScoringAuthorityV1(
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
        raise ScoringContractError(f"v1 scoring authority manifest is missing {exc}") from exc


def compute_scoring_contract_hash(
    authority: ScoringAuthority | HistoricalScoringAuthorityV1,
) -> str:
    """The domain-separated identity every evaluation is stored under.

    Two evaluations may only be compared when this hash matches: it binds the upstream scorer
    bytes, the containers that scorer runs, and the semantic rules. A future scorer change
    produces a new hash and therefore a new evaluation, never an overwrite. Each authority
    version supplies its own domain and content, so a v1 authority can never be hashed under the
    v2 domain or vice versa.
    """
    return sha256_hex(
        authority.contract_domain.encode("utf-8")
        + canonical_json_bytes(authority.contract_content())
    )
