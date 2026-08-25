"""THE production scoring authority adapter — calls the pinned MINOS_SUBNET implementation.

MINOS_ENGINE does not define the Minos score. It never did in principle, and after this module
it does not in practice either: the final score, the normalization and the admission decision all
come from executing the exact upstream bytes recorded in
``manifests/l2f2_scoring_authority_v1.json``.

The division of responsibility is strict:

* **MINOS_SUBNET** owns the science — ``HappyScorer.score_vcf``, ``AdvancedScorer``, the
  validator's admission helpers, and whatever internal tooling those choose to invoke (hap.py,
  Docker, bcftools, RTG). MINOS_ENGINE never rewrites, replaces, optimizes or second-guesses any
  of it, and never selects a different internal implementation.
* **MINOS_ENGINE** owns everything around it — which execution is being scored, whether truth may
  be touched at all, byte identity of every input, workspace isolation, persistence, replay and
  the experiment objective.

Two properties make that credible rather than aspirational. **Provenance is verified, not
trusted**: the upstream root must be a Git checkout whose HEAD is exactly the authority commit
*and* whose three authority files hash exactly as the manifest says — a branch name, a directory
name, an mtime or a caller-supplied hash proves nothing here. And **the import is isolated**: the
upstream package names (``utils``, ``templates``, ``neurons``, ``base``) are generic enough to
collide with anything, so they are never imported into the evaluator process; a separate
interpreter runs :mod:`_minos_subnet_bridge` with its working directory inside the verified
checkout.

There is deliberately no vendored copy, no network import and no install step at scoring time.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - fixed argv, shell=False, no caller-supplied executable
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.evaluation.runtime_images import (
    LocalImage,
    RuntimeImageError,
    RuntimeImageInspector,
    verify_runtime_image,
)
from minos_engine.evaluation.scoring_contract import AdmissionCode, ScoringAuthority

__all__ = [
    "ENV_MINOS_SUBNET_PYTHON",
    "ENV_MINOS_SUBNET_ROOT",
    "UPSTREAM_AUTHORITY_FILES",
    "MinosSubnetAuthorityError",
    "MinosSubnetExecutionError",
    "MinosSubnetOracle",
    "MinosSubnetOracleError",
    "MinosSubnetOracleResult",
    "MinosSubnetRuntimeProvenanceError",
    "MinosSubnetTimeoutError",
    "VerifiedUpstreamRoot",
    "verify_upstream_root",
]

#: the provisioned, pinned upstream checkout. A detached worktree at the authority commit.
ENV_MINOS_SUBNET_ROOT = "MINOS_L2F_MINOS_SUBNET_ROOT"

#: optional explicit interpreter. Normally unset: the interpreter is derived from the verified
#: root (``<root>/.venv/bin/python``), so it is pinned by the same provisioning step as the source.
ENV_MINOS_SUBNET_PYTHON = "MINOS_L2F_MINOS_SUBNET_PYTHON"

#: exactly the files the scoring authority pins. Every one is re-hashed on every scoring call.
UPSTREAM_AUTHORITY_FILES: tuple[str, ...] = (
    "utils/scoring.py",
    "neurons/validator.py",
    "templates/tool_params.py",
)

_BRIDGE = Path(__file__).with_name("_minos_subnet_bridge.py")
_BRIDGE_SCHEMA = "l2f2-minos-subnet-bridge-v1"
_CHUNK = 1024 * 1024

#: the upstream scorer can legitimately take a long time; it is still bounded.
DEFAULT_TIMEOUT_SECONDS = 7200

#: the ONLY variables the bridge subprocess inherits. Nothing about MINOS_ENGINE's own database,
#: artifact roots or credentials is visible to it.
_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "DOCKER_HOST")


class MinosSubnetOracleError(MinosEngineError):
    """Base error for the pinned upstream scoring oracle."""


class MinosSubnetAuthorityError(MinosSubnetOracleError):
    """The upstream checkout is not provably the pinned scoring authority."""


class MinosSubnetExecutionError(MinosSubnetOracleError):
    """The isolated upstream bridge could not be run to completion."""


class MinosSubnetTimeoutError(MinosSubnetExecutionError):
    """The pinned upstream scorer exceeded its bounded runtime."""


class MinosSubnetRuntimeProvenanceError(MinosSubnetAuthorityError):
    """The containers the pinned scorer would run are not the ones the authority audited."""


@dataclass(frozen=True)
class VerifiedUpstreamRoot:
    """A checkout PROVEN to be the pinned authority: commit and every source hash re-derived."""

    path: Path
    commit: str
    source_sha256: dict[str, str]
    interpreter: Path


@dataclass(frozen=True)
class MinosSubnetOracleResult:
    """Exactly what the pinned upstream implementation returned. Nothing here is recomputed.

    ``metrics`` is the upstream metrics dictionary verbatim. ``advanced_score_100`` is the return
    value of ``AdvancedScorer.compute_advanced_score``. ``minos_score`` is the normalized value
    the validator's own call site constructs and submits to ``_valid_round_score`` — identical to
    that helper's return value whenever it accepts. ``minos_score_accepted`` records whether it
    did; ``admitted`` and ``admission_code`` reflect the validator's control flow through its own
    helpers, never a local rule.
    """

    scored: bool
    metrics: dict[str, Any]
    advanced_score_100: float | None
    minos_score: float | None
    minos_score_accepted: bool
    zero_input_fingerprint: bool
    admitted: bool
    admission_code: AdmissionCode | None
    upstream_commit: str
    upstream_source_sha256: dict[str, str]
    upstream_provenance: dict[str, Any]
    #: the LITERAL references the pinned source uses, and the immutable content each was proven
    #: to resolve to locally before scoring. Never conflated: a tag is not a digest.
    happy_upstream_ref: str
    happy_resolved_digest: str
    bcftools_upstream_ref: str
    bcftools_resolved_digest: str

    @property
    def overcall_penalty(self) -> float:
        """The penalty upstream itself applied, read from the upstream metrics — never recomputed."""
        return float(self.metrics.get("overcall_penalty", 0.0) or 0.0)


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(root: Path) -> str:
    """The checkout's own HEAD, read through git so a hand-written file cannot fake it."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MinosSubnetAuthorityError(f"cannot read git HEAD of {root}: {exc}") from exc
    if completed.returncode != 0:
        raise MinosSubnetAuthorityError(
            f"{root} is not a git checkout or worktree (git rev-parse HEAD failed)"
        )
    return completed.stdout.strip()


def _require_clean_authority_files(root: Path) -> None:
    """Refuse a checkout whose authority files differ from what its own HEAD committed."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", "-C", str(root), "status", "--porcelain", "--", *UPSTREAM_AUTHORITY_FILES],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MinosSubnetAuthorityError(f"cannot read git status of {root}: {exc}") from exc
    if completed.returncode != 0:
        raise MinosSubnetAuthorityError(f"git status failed in {root}")
    dirty = [line for line in completed.stdout.splitlines() if line.strip()]
    if dirty:
        raise MinosSubnetAuthorityError(
            f"the pinned scoring authority files are modified in {root}: {dirty}"
        )


def _resolve_interpreter(root: Path) -> Path:
    """The interpreter that can import the upstream dependencies, pinned to the same checkout."""
    explicit = os.environ.get(ENV_MINOS_SUBNET_PYTHON, "").strip()
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            raise MinosSubnetAuthorityError(
                f"{ENV_MINOS_SUBNET_PYTHON} must be an absolute path, got {explicit!r}"
            )
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise MinosSubnetAuthorityError(
                f"{ENV_MINOS_SUBNET_PYTHON}={candidate} is not an executable file"
            )
        return candidate
    bundled = root / ".venv" / "bin" / "python"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    raise MinosSubnetAuthorityError(
        f"no upstream interpreter: {bundled} does not exist and {ENV_MINOS_SUBNET_PYTHON} is "
        "not set. The pinned MINOS_SUBNET checkout must carry the environment that can import "
        "its own dependencies; MINOS_ENGINE never installs them at scoring time"
    )


def verify_upstream_root(root: Path, authority: ScoringAuthority) -> VerifiedUpstreamRoot:
    """Prove a checkout IS the pinned scoring authority, or refuse it.

    Every clause is independently necessary. The commit fixes which upstream tree is intended;
    the per-file hashes fix that the tree on disk is actually that tree; the clean-status check
    catches an edit that git knows about but the hashes might not if the file were restored. A
    branch name, a directory name, an mtime, or a hash the caller supplied prove nothing and are
    never consulted.
    """
    if not root.is_absolute():
        raise MinosSubnetAuthorityError(f"{ENV_MINOS_SUBNET_ROOT} must be absolute, got {root}")
    if root.is_symlink() or not root.is_dir():
        raise MinosSubnetAuthorityError(
            f"{root} must be an existing non-symlink directory (a symlinked root would let the "
            "verified tree be swapped after verification)"
        )

    head = _git_head(root)
    if head != authority.upstream_commit:
        raise MinosSubnetAuthorityError(
            f"upstream checkout {root} is at {head}, but the scoring authority pins "
            f"{authority.upstream_commit}"
        )
    _require_clean_authority_files(root)

    expected = {
        "utils/scoring.py": authority.scoring_py_sha256,
        "neurons/validator.py": authority.validator_py_sha256,
        "templates/tool_params.py": authority.tool_params_py_sha256,
    }
    observed: dict[str, str] = {}
    for relative in UPSTREAM_AUTHORITY_FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise MinosSubnetAuthorityError(f"authority file {path} is missing or a symlink")
        digest = _sha256_file(path)
        if digest != expected[relative]:
            raise MinosSubnetAuthorityError(
                f"authority file {relative} hashes {digest}, expected {expected[relative]}"
            )
        observed[relative] = digest

    return VerifiedUpstreamRoot(
        path=root,
        commit=head,
        source_sha256=observed,
        interpreter=_resolve_interpreter(root),
    )


def _root_from_env() -> Path:
    raw = os.environ.get(ENV_MINOS_SUBNET_ROOT, "").strip()
    if not raw:
        raise MinosSubnetAuthorityError(
            f"{ENV_MINOS_SUBNET_ROOT} is not set; production scoring requires the provisioned "
            "pinned MINOS_SUBNET checkout"
        )
    return Path(raw)


@dataclass(frozen=True)
class MinosSubnetOracle:
    """The production scoring oracle. Verifies the pinned checkout, then calls it in isolation."""

    authority: ScoringAuthority
    root: Path
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    #: TEST-ONLY seam. Production leaves this ``None`` and the real Docker inspector is used;
    #: substituting it lets the whole verification POLICY be exercised without a daemon. It is
    #: never reachable from :meth:`from_env`, which is the production construction path.
    image_inspector: RuntimeImageInspector | None = None

    @classmethod
    def from_env(cls, authority: ScoringAuthority, *, timeout_seconds: int | None = None) -> Any:
        """Construct from the provisioned environment. No caller-supplied path, hash or seam."""
        return cls(
            authority=authority,
            root=_root_from_env(),
            timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        )

    def verify(self) -> VerifiedUpstreamRoot:
        return verify_upstream_root(self.root, self.authority)

    def probe_runtime(self, *, work_dir: Path) -> dict[str, Any]:
        """Ask the pinned source what containers it would run — WITHOUT scoring anything."""
        verified = self.verify()
        response = self._invoke_bridge(
            {"upstream_root": str(verified.path), "mode": "probe"},
            work_dir=work_dir,
            verified=verified,
        )
        return dict(response.get("provenance") or {})

    def verify_runtime_provenance(self, *, work_dir: Path) -> dict[str, LocalImage]:
        """Prove, BEFORE any biological byte is read, which container bytes upstream will run.

        Three separate claims, each of which can fail on its own:

        1. the pinned source's own literal references are exactly the ones the authority records
           — so upstream has not changed which container it names;
        2. each of those references names an image that exists on this host — MINOS_ENGINE never
           pulls during scoring, because a scoring call must not fetch new bytes off the network;
        3. each resolves to exactly the immutable digest the authority audited — so a moving tag
           cannot silently swap the implementation underneath a fixed name.

        Upstream's commands are untouched throughout. This verifies; it never substitutes.
        """
        provenance = self.probe_runtime(work_dir=work_dir)
        expected = {
            "hap.py": (self.authority.happy, provenance.get("happy_upstream_ref")),
            "bcftools": (self.authority.bcftools, provenance.get("bcftools_upstream_ref")),
        }
        resolved: dict[str, LocalImage] = {}
        for label, (identity, observed) in expected.items():
            if observed != identity.upstream_ref:
                raise MinosSubnetRuntimeProvenanceError(
                    f"the pinned source names {label} as {observed!r}, but the scoring authority "
                    f"records {identity.upstream_ref!r}"
                )
            try:
                resolved[label] = verify_runtime_image(
                    identity.upstream_ref,
                    expected_digest=identity.resolved_digest,
                    label=label,
                    inspector=self.image_inspector,
                )
            except RuntimeImageError as exc:
                raise MinosSubnetRuntimeProvenanceError(str(exc)) from exc
        return resolved

    def score(
        self,
        *,
        truth_vcf: Path,
        query_vcf: Path,
        mutations_vcf: Path,
        reference_fasta: Path,
        reference_sdf: Path | None,
        confident_bed: Path | None,
        region: str,
        work_dir: Path,
    ) -> MinosSubnetOracleResult:
        """Run the pinned upstream scorer over already-verified, already-sandboxed inputs.

        Every path must live inside the caller's fresh attempt workspace: the upstream scorer
        legitimately writes intermediates beside the files it is given, and MINOS_ENGINE's
        registered evidence must never be what it writes beside.
        """
        verified = self.verify()
        # BEFORE any biological byte is read: prove the containers the pinned source will run are
        # the audited ones. Fails closed; upstream's own commands are never altered.
        self.verify_runtime_provenance(work_dir=work_dir)
        request = {
            "upstream_root": str(verified.path),
            "truth_vcf": str(truth_vcf),
            "query_vcf": str(query_vcf),
            "mutations_vcf": str(mutations_vcf),
            "reference_fasta": str(reference_fasta),
            "reference_sdf": str(reference_sdf) if reference_sdf is not None else None,
            "confident_bed": str(confident_bed) if confident_bed is not None else None,
            "region": region,
        }
        response = self._invoke_bridge(request, work_dir=work_dir, verified=verified)
        return self._build_result(response, verified)

    def _invoke_bridge(
        self, request: dict[str, Any], *, work_dir: Path, verified: VerifiedUpstreamRoot
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(dir=str(work_dir), prefix="oracle-") as channel:
            request_path = Path(channel) / "request.json"
            response_path = Path(channel) / "response.json"
            request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
            env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
            env["PYTHONPATH"] = str(verified.path)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            try:
                completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
                    [
                        str(verified.interpreter),
                        str(_BRIDGE),
                        str(request_path),
                        str(response_path),
                    ],
                    cwd=str(verified.path),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise MinosSubnetTimeoutError(
                    f"the pinned MINOS_SUBNET scorer exceeded {self.timeout_seconds}s"
                ) from exc
            except OSError as exc:
                raise MinosSubnetExecutionError(f"cannot run the upstream bridge: {exc}") from exc
            if completed.returncode != 0 or not response_path.is_file():
                raise MinosSubnetExecutionError(
                    f"the upstream bridge exited {completed.returncode} without a response "
                    f"(stderr sha256 {sha256_hex(completed.stderr.encode('utf-8'))})"
                )
            raw = response_path.read_text(encoding="utf-8")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MinosSubnetExecutionError("the upstream bridge response is not JSON") from exc
        if response.get("bridge_schema") != _BRIDGE_SCHEMA:
            raise MinosSubnetExecutionError(
                f"unexpected bridge schema {response.get('bridge_schema')!r}"
            )
        if "error" in response:
            raise MinosSubnetExecutionError(
                f"the pinned MINOS_SUBNET implementation raised:\n{response['error']}"
            )
        return dict(response)

    def _build_result(
        self, response: dict[str, Any], verified: VerifiedUpstreamRoot
    ) -> MinosSubnetOracleResult:
        provenance = dict(response.get("provenance") or {})
        # the scoring run must have used the same literal references the pre-flight verified; a
        # divergence between probe and score would mean the checkout changed underneath us.
        for label, identity, key in (
            ("hap.py", self.authority.happy, "happy_upstream_ref"),
            ("bcftools", self.authority.bcftools, "bcftools_upstream_ref"),
        ):
            observed = provenance.get(key)
            if observed != identity.upstream_ref:
                raise MinosSubnetRuntimeProvenanceError(
                    f"the upstream scorer used {label} reference {observed!r}, but the scoring "
                    f"authority records {identity.upstream_ref!r}"
                )
        metrics = response.get("metrics")
        return MinosSubnetOracleResult(
            scored=bool(response.get("scored")),
            metrics=dict(metrics) if isinstance(metrics, dict) else {},
            advanced_score_100=response.get("advanced_score_100"),
            minos_score=response.get("minos_score"),
            minos_score_accepted=bool(response.get("minos_score_accepted")),
            zero_input_fingerprint=bool(response.get("zero_input_fingerprint")),
            admitted=bool(response.get("admitted")),
            admission_code=response.get("admission_code"),
            upstream_commit=verified.commit,
            upstream_source_sha256=dict(verified.source_sha256),
            upstream_provenance=provenance,
            happy_upstream_ref=self.authority.happy.upstream_ref,
            happy_resolved_digest=self.authority.happy.resolved_digest,
            bcftools_upstream_ref=self.authority.bcftools.upstream_ref,
            bcftools_resolved_digest=self.authority.bcftools.resolved_digest,
        )
