"""The pinned MINOS_SUBNET scoring oracle — provenance, isolation and exact passthrough.

No real hap.py, no real Docker and no biological data run here. A tiny synthetic "upstream"
package stands in for the real checkout so the ADAPTER can be exercised exhaustively: what it
verifies before it will run anything, that it executes the upstream code in a separate
interpreter rooted at that checkout, and that whatever upstream returns arrives unchanged.

The real pinned source is covered separately: its three authority digests are checked against the
committed manifest, and the environment/canary run exercises the real implementation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from minos_engine.evaluation.minos_subnet_oracle import (
    ENV_MINOS_SUBNET_PYTHON,
    ENV_MINOS_SUBNET_ROOT,
    UPSTREAM_AUTHORITY_FILES,
    MinosSubnetAuthorityError,
    MinosSubnetExecutionError,
    MinosSubnetOracle,
    MinosSubnetRuntimeProvenanceError,
    verify_upstream_root,
)
from minos_engine.evaluation.runtime_images import (
    LocalImage,
    RuntimeImageAbsentError,
    RuntimeImageError,
)
from minos_engine.evaluation.scoring_contract import (
    RuntimeImageIdentity,
    ScoringAuthority,
    load_scoring_authority,
)

_HAPPY_REF = "fake/happy@sha256:" + "a" * 64
_HAPPY_DIGEST = _HAPPY_REF
_BCFTOOLS_TAG = "fake/bcftools:1.20--test"
_BCFTOOLS_DIGEST = "fake/bcftools@sha256:" + "b" * 64


def _inspector(resolved: dict[str, str]) -> Any:
    """A Docker-free inspection seam that resolves exactly what the test says it resolves."""

    def _inspect(reference: str) -> LocalImage:
        if reference not in resolved:
            raise RuntimeImageAbsentError(f"{reference!r} is not present on this host")
        digests = resolved[reference]
        return LocalImage(
            reference=reference,
            image_id="sha256:" + "e" * 64,
            repo_digests=() if digests is None else (digests,),
        )

    return _inspect


def _good_inspector() -> Any:
    return _inspector({_HAPPY_REF: _HAPPY_DIGEST, _BCFTOOLS_TAG: _BCFTOOLS_DIGEST})


# --------------------------------------------------------------------------- #
# a synthetic upstream checkout: real git, real packages, trivial "science"
# --------------------------------------------------------------------------- #
_SCORING_PY = """\
BCFTOOLS_DOCKER_IMAGE = "fake/bcftools:1.20--test"


class HappyScorer:
    def __init__(self, docker_image=None):
        self.docker_image = docker_image or ("fake/happy@sha256:" + "a" * 64)

    def score_vcf(self, truth_vcf, query_vcf, reference_fasta=None, confident_bed=None,
                  region=None, reference_sdf=None, mutations_vcf=None):
        import json as _json
        import pathlib as _pathlib
        plan = _json.loads(_pathlib.Path(query_vcf).read_text())
        if plan["metrics"] is None:
            return None
        metrics = dict(plan["metrics"])
        metrics["seen"] = {
            "truth_vcf": truth_vcf, "query_vcf": query_vcf, "reference_fasta": reference_fasta,
            "confident_bed": confident_bed, "region": region, "reference_sdf": reference_sdf,
            "mutations_vcf": mutations_vcf,
        }
        return metrics


class AdvancedScorer:
    @staticmethod
    def compute_advanced_score(metrics):
        return float(metrics["_advanced_score_100"])
"""

_UTILS_INIT = "from .scoring import AdvancedScorer, HappyScorer, BCFTOOLS_DOCKER_IMAGE\n"

_VALIDATOR_PY = """\
import math


def _valid_round_score(value, *, label):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score <= 0.0 or score > 1.0:
        return None
    return score


def _is_zero_input_advanced_fingerprint(metrics, combined_final):
    return (
        (metrics.get("f1_snp") or 0.0) == 0.0
        and (metrics.get("f1_indel") or 0.0) == 0.0
        and 0.24999 <= combined_final <= 0.25001
    )
"""

_TOOL_PARAMS_PY = "def validate_region(region):\n    return {'valid': True, 'error': None}\n"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _write_upstream(root: Path, *, scoring: str = _SCORING_PY) -> None:
    (root / "utils").mkdir(parents=True, exist_ok=True)
    (root / "neurons").mkdir(parents=True, exist_ok=True)
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "utils" / "__init__.py").write_text(_UTILS_INIT)
    (root / "utils" / "scoring.py").write_text(scoring)
    (root / "neurons" / "__init__.py").write_text("")
    (root / "neurons" / "validator.py").write_text(_VALIDATOR_PY)
    (root / "templates" / "__init__.py").write_text("")
    (root / "templates" / "tool_params.py").write_text(_TOOL_PARAMS_PY)


def _make_checkout(root: Path, *, scoring: str = _SCORING_PY) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _write_upstream(root, scoring=scoring)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture upstream")
    return _git(root, "rev-parse", "HEAD")


def _authority_for(root: Path, commit: str) -> ScoringAuthority:
    import hashlib

    def digest(relative: str) -> str:
        return hashlib.sha256((root / relative).read_bytes()).hexdigest()

    real = load_scoring_authority()
    return real.model_copy(
        update={
            "upstream_commit": commit,
            "scoring_py_sha256": digest("utils/scoring.py"),
            "validator_py_sha256": digest("neurons/validator.py"),
            "tool_params_py_sha256": digest("templates/tool_params.py"),
            "happy": RuntimeImageIdentity(upstream_ref=_HAPPY_REF, resolved_digest=_HAPPY_DIGEST),
            # tag-pinned upstream, digest-resolved locally — the real bcftools shape.
            "bcftools": RuntimeImageIdentity(
                upstream_ref=_BCFTOOLS_TAG, resolved_digest=_BCFTOOLS_DIGEST
            ),
        }
    )


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A synthetic pinned checkout, plus the interpreter that will run the bridge inside it.

    The synthetic upstream package has no third-party dependencies, so this interpreter can be
    the test one — which is the point: the bridge still runs as a SEPARATE process rooted at the
    checkout, so ``utils`` / ``neurons`` / ``templates`` resolve there and never in this repo.
    """
    root = tmp_path / "upstream"
    commit = _make_checkout(root)
    monkeypatch.setenv(ENV_MINOS_SUBNET_PYTHON, sys.executable)
    return root, commit, _authority_for(root, commit)


def _plan(tmp_path: Path, metrics: dict[str, Any] | None) -> Path:
    """The synthetic scorer reads its answer out of the 'query VCF' — no biology involved."""
    path = tmp_path / "query.vcf.gz"
    path.write_text(json.dumps({"metrics": metrics}))
    return path


def _oracle(root: Path, authority: ScoringAuthority, *, inspector: Any = None) -> MinosSubnetOracle:
    return MinosSubnetOracle(
        authority=authority,
        root=root,
        timeout_seconds=300,
        image_inspector=inspector or _good_inspector(),
    )


def _score(oracle: Any, tmp_path: Path, metrics: dict[str, Any] | None) -> Any:
    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    return oracle.score(
        truth_vcf=tmp_path / "truth.vcf.gz",
        query_vcf=_plan(tmp_path, metrics),
        mutations_vcf=tmp_path / "mutations.vcf.gz",
        reference_fasta=tmp_path / "chr18.fa",
        reference_sdf=tmp_path / "chr18.sdf",
        confident_bed=None,
        region="chr18:1-1000",
        work_dir=work,
    )


# --------------------------------------------------------------------------- #
# provenance: the root is verified, never trusted
# --------------------------------------------------------------------------- #
def test_a_matching_checkout_verifies(upstream: Any) -> None:
    root, commit, authority = upstream
    verified = verify_upstream_root(root, authority)
    assert verified.commit == commit
    assert sorted(verified.source_sha256) == sorted(UPSTREAM_AUTHORITY_FILES)


def test_the_real_pinned_source_matches_the_committed_authority() -> None:
    """The committed manifest is the authority; this proves the digests it pins are current."""
    authority = load_scoring_authority()
    assert authority.upstream_commit == "649bb92c6abccebde58a736a2b2af7fd77a701c1"
    assert (
        authority.scoring_py_sha256
        == "7b5aa187adda5978adc029abcd4c96b7b78eafeb9c5641153955175cd0b7b658"
    )
    assert (
        authority.validator_py_sha256
        == "2ac0841231a58794097ba40d245f27eaa44e1bd1b66134a17dece96a1a37f33e"
    )
    assert (
        authority.tool_params_py_sha256
        == "6e9648fb6d6bda1ed5411eff01c38596cc869e2f7ae9e5de855e8413f10e0765"
    )


def test_a_wrong_head_is_refused(upstream: Any) -> None:
    root, _commit, authority = upstream
    (root / "README.md").write_text("unrelated change")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "moves HEAD without touching authority files")
    with pytest.raises(MinosSubnetAuthorityError, match="pins"):
        verify_upstream_root(root, authority)


@pytest.mark.parametrize("relative", UPSTREAM_AUTHORITY_FILES)
def test_a_modified_authority_file_is_refused(upstream: Any, relative: str) -> None:
    """Correct HEAD is not enough: the bytes on disk must be the bytes the authority pins."""
    root, _commit, authority = upstream
    (root / relative).write_text((root / relative).read_text() + "\n# tampered\n")
    with pytest.raises(MinosSubnetAuthorityError, match="modified|hashes"):
        verify_upstream_root(root, authority)


@pytest.mark.parametrize("relative", UPSTREAM_AUTHORITY_FILES)
def test_a_hash_mismatch_is_refused_even_when_git_is_clean(
    upstream: Any, tmp_path: Path, relative: str
) -> None:
    """A changed expectation with an untouched tree is still a refusal."""
    root, commit, authority = upstream
    field = {
        "utils/scoring.py": "scoring_py_sha256",
        "neurons/validator.py": "validator_py_sha256",
        "templates/tool_params.py": "tool_params_py_sha256",
    }[relative]
    wrong = authority.model_copy(update={field: "f" * 64})
    assert wrong.upstream_commit == commit
    with pytest.raises(MinosSubnetAuthorityError, match="hashes"):
        verify_upstream_root(root, wrong)


def test_a_non_git_directory_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    _write_upstream(plain)
    with pytest.raises(MinosSubnetAuthorityError, match="not a git checkout|git rev-parse"):
        verify_upstream_root(plain, load_scoring_authority())


def test_a_symlinked_root_is_refused(upstream: Any, tmp_path: Path) -> None:
    """A symlinked root could be repointed between verification and use."""
    root, _commit, authority = upstream
    link = tmp_path / "link-to-upstream"
    link.symlink_to(root)
    with pytest.raises(MinosSubnetAuthorityError, match="non-symlink"):
        verify_upstream_root(link, authority)


def test_a_relative_root_is_refused(upstream: Any) -> None:
    _root, _commit, authority = upstream
    with pytest.raises(MinosSubnetAuthorityError, match="absolute"):
        verify_upstream_root(Path("minos_subnet"), authority)


def test_a_different_minos_commit_is_refused(upstream: Any, tmp_path: Path) -> None:
    """Local main moving on is not a reason to score under a different implementation."""
    other = tmp_path / "other-upstream"
    other_commit = _make_checkout(other, scoring=_SCORING_PY + "\n# a later upstream revision\n")
    _root, _commit, authority = upstream
    assert other_commit != authority.upstream_commit
    with pytest.raises(MinosSubnetAuthorityError):
        verify_upstream_root(other, authority)


def test_the_root_comes_from_the_environment_not_the_caller(
    upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _commit, authority = upstream
    monkeypatch.delenv(ENV_MINOS_SUBNET_ROOT, raising=False)
    with pytest.raises(MinosSubnetAuthorityError, match=ENV_MINOS_SUBNET_ROOT):
        MinosSubnetOracle.from_env(authority)
    monkeypatch.setenv(ENV_MINOS_SUBNET_ROOT, str(root))
    assert MinosSubnetOracle.from_env(authority).root == root


def test_a_missing_interpreter_is_refused(upstream: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """MINOS_ENGINE never installs upstream's dependencies at scoring time; the pinned checkout
    must carry an interpreter that can already import them."""
    root, _commit, authority = upstream
    monkeypatch.delenv(ENV_MINOS_SUBNET_PYTHON, raising=False)
    assert not (root / ".venv").exists()
    with pytest.raises(MinosSubnetAuthorityError, match="interpreter"):
        verify_upstream_root(root, authority)


def test_a_relative_interpreter_is_refused(upstream: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _commit, authority = upstream
    monkeypatch.setenv(ENV_MINOS_SUBNET_PYTHON, "python3")
    with pytest.raises(MinosSubnetAuthorityError, match="absolute"):
        verify_upstream_root(root, authority)


# --------------------------------------------------------------------------- #
# isolation + exact passthrough
# --------------------------------------------------------------------------- #
_ADMITTED = {
    "_advanced_score_100": 82.5,
    "f1_snp": 0.97,
    "f1_indel": 0.91,
    "overcall_penalty": 1.25,
}


def test_the_upstream_result_arrives_unchanged(upstream: Any, tmp_path: Path) -> None:
    """Metrics, score and admission all cross the boundary verbatim."""
    root, commit, authority = upstream
    result = _score(_oracle(root, authority), tmp_path, _ADMITTED)

    assert result.scored is True
    assert result.metrics == {**_ADMITTED, "seen": result.metrics["seen"]}
    assert result.advanced_score_100 == 82.5
    assert result.minos_score == pytest.approx(0.825)
    assert result.minos_score_accepted is True
    assert result.zero_input_fingerprint is False
    assert result.admitted is True
    assert result.admission_code == "ADMITTED"
    assert result.upstream_commit == commit
    assert result.overcall_penalty == 1.25


def test_the_exact_semantic_arguments_reach_the_upstream_scorer(
    upstream: Any, tmp_path: Path
) -> None:
    """The oracle passes the same arguments the Minos validator supplies to its own scorer."""
    root, _commit, authority = upstream
    result = _score(_oracle(root, authority), tmp_path, _ADMITTED)
    seen = result.metrics["seen"]
    assert seen["region"] == "chr18:1-1000"
    assert seen["confident_bed"] is None  # the mutations VCF defines the scope, as upstream does
    assert seen["truth_vcf"].endswith("truth.vcf.gz")
    assert seen["mutations_vcf"].endswith("mutations.vcf.gz")
    assert seen["reference_fasta"].endswith("chr18.fa")
    assert seen["reference_sdf"].endswith("chr18.sdf")


@pytest.mark.parametrize(
    ("score_100", "expected_code", "admitted"),
    [
        (0.0, "NONPOSITIVE_SCORE", False),
        (100.5, "OUT_OF_RANGE_SCORE", False),
        (73.25, "ADMITTED", True),
    ],
)
def test_the_validator_decides_admission(
    upstream: Any, tmp_path: Path, score_100: float, expected_code: str, admitted: bool
) -> None:
    """Accept/reject comes from the upstream helper, not from a rule in this repository."""
    root, _commit, authority = upstream
    metrics = {**_ADMITTED, "_advanced_score_100": score_100}
    result = _score(_oracle(root, authority), tmp_path, metrics)
    assert result.admission_code == expected_code
    assert result.admitted is admitted
    assert result.minos_score == pytest.approx(score_100 / 100.0)
    assert result.minos_score_accepted is admitted


def test_the_zero_input_fingerprint_comes_from_upstream(upstream: Any, tmp_path: Path) -> None:
    root, _commit, authority = upstream
    metrics = {"_advanced_score_100": 25.0, "f1_snp": 0.0, "f1_indel": 0.0}
    result = _score(_oracle(root, authority), tmp_path, metrics)
    assert result.zero_input_fingerprint is True
    assert result.admitted is False
    assert result.admission_code == "ZERO_INPUT_FINGERPRINT"


def test_upstream_declining_to_score_is_reported_not_invented(
    upstream: Any, tmp_path: Path
) -> None:
    root, _commit, authority = upstream
    result = _score(_oracle(root, authority), tmp_path, None)
    assert result.scored is False
    assert result.metrics == {}
    assert result.advanced_score_100 is None
    assert result.admission_code is None


def test_the_oracle_result_equals_calling_the_same_upstream_directly(
    upstream: Any, tmp_path: Path
) -> None:
    """§25 parity: the wrapper adds nothing. The comparison is against the SAME implementation
    executed directly — never against a second formula written here."""
    root, _commit, authority = upstream
    wrapped = _score(_oracle(root, authority), tmp_path, _ADMITTED)

    direct = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from utils import AdvancedScorer;"
            "import neurons.validator as V;"
            "m=json.loads(sys.argv[2]);"
            "a=AdvancedScorer.compute_advanced_score(m);"
            "c=V._valid_round_score(a/100.0,label='direct');"
            "print(json.dumps({'a':a,'c':c,"
            "'z':bool(V._is_zero_input_advanced_fingerprint(m,c)) if c is not None else False}))",
            str(root),
            json.dumps(_ADMITTED),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    reference = json.loads(direct.stdout)
    assert wrapped.advanced_score_100 == reference["a"]
    assert wrapped.minos_score == pytest.approx(reference["c"])
    assert wrapped.zero_input_fingerprint is reference["z"]


# --------------------------------------------------------------------------- #
# runtime container provenance — verified BEFORE any biological byte is read
# --------------------------------------------------------------------------- #
def test_both_container_identities_are_verified_before_scoring(
    upstream: Any, tmp_path: Path
) -> None:
    """The pre-flight resolves BOTH references, and it happens before score_vcf is ever called."""
    root, _commit, authority = upstream
    seen: list[str] = []

    def _recording(reference: str) -> LocalImage:
        seen.append(reference)
        return _good_inspector()(reference)

    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    resolved = _oracle(root, authority, inspector=_recording).verify_runtime_provenance(
        work_dir=work
    )
    assert sorted(seen) == sorted([_HAPPY_REF, _BCFTOOLS_TAG])
    assert sorted(resolved) == ["bcftools", "hap.py"]
    # the probe scored nothing: no query plan was ever read.
    assert not list(work.glob("*.json"))


def test_an_absent_bcftools_tag_is_refused(upstream: Any, tmp_path: Path) -> None:
    """MINOS_ENGINE never pulls during scoring, so an unprovisioned image fails closed."""
    root, _commit, authority = upstream
    inspector = _inspector({_HAPPY_REF: _HAPPY_DIGEST})
    with pytest.raises(MinosSubnetRuntimeProvenanceError, match="not present on this host"):
        _score(_oracle(root, authority, inspector=inspector), tmp_path, _ADMITTED)


def test_a_bcftools_tag_resolving_to_other_content_is_refused(
    upstream: Any, tmp_path: Path
) -> None:
    """The whole point of resolving a TAG: it may have been moved underneath the same name."""
    root, _commit, authority = upstream
    inspector = _inspector(
        {_HAPPY_REF: _HAPPY_DIGEST, _BCFTOOLS_TAG: "fake/bcftools@sha256:" + "9" * 64}
    )
    with pytest.raises(MinosSubnetRuntimeProvenanceError, match="audited"):
        _score(_oracle(root, authority, inspector=inspector), tmp_path, _ADMITTED)


def test_a_happy_image_resolving_to_other_content_is_refused(upstream: Any, tmp_path: Path) -> None:
    root, _commit, authority = upstream
    inspector = _inspector(
        {_HAPPY_REF: "fake/happy@sha256:" + "9" * 64, _BCFTOOLS_TAG: _BCFTOOLS_DIGEST}
    )
    with pytest.raises(MinosSubnetRuntimeProvenanceError, match="audited"):
        _score(_oracle(root, authority, inspector=inspector), tmp_path, _ADMITTED)


@pytest.mark.parametrize("tool", ["happy", "bcftools"])
def test_an_unexpected_upstream_reference_is_refused(
    upstream: Any, tmp_path: Path, tool: str
) -> None:
    """If the pinned source names a DIFFERENT container than the authority records, refuse."""
    root, _commit, authority = upstream
    identity = getattr(authority, tool)
    substituted = authority.model_copy(
        update={
            tool: identity.model_copy(update={"upstream_ref": "other/thing:9.9"}),
        }
    )
    with pytest.raises(MinosSubnetRuntimeProvenanceError, match="scoring authority records"):
        _score(_oracle(root, substituted), tmp_path, _ADMITTED)


def test_a_docker_inspection_failure_is_refused(upstream: Any, tmp_path: Path) -> None:
    """A timeout or malformed daemon response is a refusal, never an assumed pass."""
    root, _commit, authority = upstream

    def _broken(reference: str) -> LocalImage:
        raise RuntimeImageError("docker image inspect exceeded its timeout")

    with pytest.raises(MinosSubnetRuntimeProvenanceError, match="timeout"):
        _score(_oracle(root, authority, inspector=_broken), tmp_path, _ADMITTED)


def test_the_scored_result_carries_both_identities_for_both_containers(
    upstream: Any, tmp_path: Path
) -> None:
    root, _commit, authority = upstream
    result = _score(_oracle(root, authority), tmp_path, _ADMITTED)
    assert result.happy_upstream_ref == _HAPPY_REF
    assert result.happy_resolved_digest == _HAPPY_DIGEST
    assert result.bcftools_upstream_ref == _BCFTOOLS_TAG
    assert result.bcftools_resolved_digest == _BCFTOOLS_DIGEST
    # a tag is never recorded as a digest.
    assert "@sha256:" not in result.bcftools_upstream_ref
    assert "@sha256:" in result.bcftools_resolved_digest


def test_production_construction_uses_the_real_docker_inspector(
    upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inspection seam is test-only: the production entry point never populates it."""
    root, _commit, authority = upstream
    monkeypatch.setenv(ENV_MINOS_SUBNET_ROOT, str(root))
    assert MinosSubnetOracle.from_env(authority).image_inspector is None


def test_an_upstream_exception_becomes_a_typed_execution_error(
    upstream: Any, tmp_path: Path
) -> None:
    root, _commit, authority = upstream
    with pytest.raises(MinosSubnetExecutionError, match="raised"):
        _score(_oracle(root, authority), tmp_path, {"no_advanced_score_key": 1})


def test_the_bridge_runs_out_of_process_and_never_imports_minos_engine(
    upstream: Any, tmp_path: Path
) -> None:
    """The generic upstream module names must never enter THIS interpreter."""
    root, _commit, authority = upstream
    before = {
        name for name in sys.modules if name.split(".")[0] in {"utils", "neurons", "templates"}
    }
    _score(_oracle(root, authority), tmp_path, _ADMITTED)
    after = {
        name for name in sys.modules if name.split(".")[0] in {"utils", "neurons", "templates"}
    }
    assert after == before, f"upstream modules leaked into the evaluator process: {after - before}"
