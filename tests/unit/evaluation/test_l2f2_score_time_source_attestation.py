"""The source that actually SCORED must be the pinned authority — not merely the source that was
there beforehand.

A pre-flight verification is made before the scoring subprocess exists, so on its own it can only
ever say "this checkout *was* correct". It cannot speak for the bytes that process imported and
ran. Between the pre-flight and the score, a checkout can be edited — by a concurrent operator, a
rebuild, an errant sync — and if the durable evaluation then records the *pre-flight* digests, it
attributes a real scientific result to source bytes that did not produce it.

That is the exact gap these tests close. The identity is established three times over — pre-flight
here, the bridge's own derivation inside the subprocess, and a fresh re-derivation after it exits
— and every one of them must agree with the committed authority.

One control matters more than the rest: **unchanged container references are not evidence.** A
mutation that leaves hap.py and bcftools untouched while changing the scoring source must still be
rejected, because those references are exactly what the previous corrective already checked.

No Docker, no hap.py, no biological data: a tiny synthetic Git checkout stands in for upstream so
every branch is deterministic.
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
    MinosSubnetOracle,
    MinosSubnetSourceAttestationError,
)
from minos_engine.evaluation.runtime_images import LocalImage, RuntimeImageAbsentError
from minos_engine.evaluation.scoring_contract import (
    RuntimeImageIdentity,
    load_scoring_authority,
)

_HAPPY_REF = "fake/happy@sha256:" + "a" * 64
_BCFTOOLS_TAG = "fake/bcftools:1.20--test"
_BCFTOOLS_DIGEST = "fake/bcftools@sha256:" + "b" * 64

#: the synthetic scorer reads its answer out of the "query VCF", so no biology is involved. It
#: also optionally rewrites an authority file MID-SCORE, which is how the during-score mutation
#: control is driven.
_SCORING_PY = """\
import json
import pathlib

BCFTOOLS_DOCKER_IMAGE = "fake/bcftools:1.20--test"
HAPPY_DOCKER_IMAGE = "fake/happy@sha256:" + "a" * 64


class HappyScorer:
    def __init__(self, docker_image=None):
        self.docker_image = docker_image or HAPPY_DOCKER_IMAGE

    def score_vcf(self, truth_vcf, query_vcf, reference_fasta=None, confident_bed=None,
                  region=None, reference_sdf=None, mutations_vcf=None):
        plan = json.loads(pathlib.Path(query_vcf).read_text())
        mutate = plan.get("mutate_during_score")
        if mutate:
            target = pathlib.Path(__file__).parent.parent / mutate
            target.write_text(target.read_text() + "\\n# mutated mid-score\\n")
        return dict(plan["metrics"]) if plan["metrics"] is not None else None


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

_METRICS = {"_advanced_score_100": 84.0, "f1_snp": 0.97, "f1_indel": 0.9}

_AUTHORITY_FILES = ("utils/scoring.py", "neurons/validator.py", "templates/tool_params.py")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_checkout(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    for package, module, body in (
        ("utils", "scoring.py", _SCORING_PY),
        ("neurons", "validator.py", _VALIDATOR_PY),
        ("templates", "tool_params.py", _TOOL_PARAMS_PY),
    ):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text(
            _UTILS_INIT if package == "utils" else "", encoding="utf-8"
        )
        (root / package / module).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "synthetic upstream")
    return _git(root, "rev-parse", "HEAD")


def _authority_for(root: Path, commit: str) -> Any:
    import hashlib

    def digest(relative: str) -> str:
        return hashlib.sha256((root / relative).read_bytes()).hexdigest()

    return load_scoring_authority().model_copy(
        update={
            "upstream_commit": commit,
            "scoring_py_sha256": digest("utils/scoring.py"),
            "validator_py_sha256": digest("neurons/validator.py"),
            "tool_params_py_sha256": digest("templates/tool_params.py"),
            "happy": RuntimeImageIdentity(upstream_ref=_HAPPY_REF, resolved_digest=_HAPPY_REF),
            "bcftools": RuntimeImageIdentity(
                upstream_ref=_BCFTOOLS_TAG, resolved_digest=_BCFTOOLS_DIGEST
            ),
        }
    )


def _inspector(reference: str) -> LocalImage:
    resolved = {_HAPPY_REF: _HAPPY_REF, _BCFTOOLS_TAG: _BCFTOOLS_DIGEST}
    if reference not in resolved:
        raise RuntimeImageAbsentError(f"{reference!r} is not present on this host")
    return LocalImage(reference, "sha256:" + "e" * 64, (resolved[reference],))


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    root = tmp_path / "upstream"
    commit = _make_checkout(root)
    monkeypatch.setenv(ENV_MINOS_SUBNET_PYTHON, sys.executable)
    return root, commit, _authority_for(root, commit)


def _oracle(root: Path, authority: Any) -> MinosSubnetOracle:
    return MinosSubnetOracle(
        authority=authority, root=root, timeout_seconds=300, image_inspector=_inspector
    )


def _score(
    oracle: MinosSubnetOracle, tmp_path: Path, *, mutate_during_score: str | None = None
) -> Any:
    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    query = tmp_path / "query.vcf.gz"
    query.write_text(json.dumps({"metrics": _METRICS, "mutate_during_score": mutate_during_score}))
    return oracle.score(
        truth_vcf=tmp_path / "truth.vcf.gz",
        query_vcf=query,
        mutations_vcf=tmp_path / "mutations.vcf.gz",
        reference_fasta=tmp_path / "chr18.fa",
        reference_sdf=tmp_path / "chr18.sdf",
        confident_bed=None,
        region="chr18:1-1000",
        work_dir=work,
    )


def _append(root: Path, relative: str) -> None:
    """Change one authority file WITHOUT touching either container reference."""
    path = root / relative
    path.write_text(path.read_text() + "\n# mutated between preflight and score\n")


# --------------------------------------------------------------------------- #
# positive: an unchanged checkout attests identically at every stage
# --------------------------------------------------------------------------- #
def test_pre_bridge_and_post_identities_all_agree(upstream: Any, tmp_path: Path) -> None:
    root, commit, authority = upstream
    result = _score(_oracle(root, authority), tmp_path)

    assert result.upstream_commit == commit == authority.upstream_commit
    assert result.upstream_source_sha256 == {
        "utils/scoring.py": authority.scoring_py_sha256,
        "neurons/validator.py": authority.validator_py_sha256,
        "templates/tool_params.py": authority.tool_params_py_sha256,
    }
    # and the exact upstream science still passes through untouched.
    assert result.advanced_score_100 == 84.0
    assert result.minos_score == pytest.approx(0.84)
    assert result.minos_score_accepted is True
    assert result.admitted is True
    assert result.admission_code == "ADMITTED"
    assert result.metrics == _METRICS


def test_the_bridge_derives_its_own_attestation_at_three_points(
    upstream: Any, tmp_path: Path
) -> None:
    """Before imports, after imports, after scoring — all from the root, none from the caller."""
    root, commit, authority = upstream
    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    provenance = _oracle(root, authority).probe_runtime(work_dir=work)
    assert provenance["modules_within_root"] is True
    for name, path in provenance["module_files"].items():
        assert Path(path).is_relative_to(root), name


# --------------------------------------------------------------------------- #
# negatives: a source mutation the old path would have mis-attributed
# --------------------------------------------------------------------------- #
def _mutating_between_preflight_and_score(oracle: Any, root: Path, relative: str) -> None:
    """Edit the source in the window the defect lives in: AFTER the pre-flight verification has
    already succeeded, but BEFORE the scoring subprocess starts."""
    real = oracle.verify_runtime_provenance

    def _then_mutate(**kwargs: Any) -> Any:
        resolved = real(**kwargs)
        _append(root, relative)
        return resolved

    object.__setattr__(oracle, "verify_runtime_provenance", _then_mutate)


@pytest.mark.parametrize("relative", _AUTHORITY_FILES)
def test_a_mutation_between_preflight_and_score_is_rejected(
    upstream: Any, tmp_path: Path, relative: str
) -> None:
    """THE defect. The pre-flight succeeds against a pristine checkout, the source then changes,
    and the score runs against the changed bytes. The result must NOT be stamped with the
    pre-flight digests — it must not be produced at all."""
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)

    # the pre-flight genuinely succeeds first, exactly as it does in production.
    pre = oracle.verify()
    assert pre.source_sha256["utils/scoring.py"] == authority.scoring_py_sha256

    _mutating_between_preflight_and_score(oracle, root, relative)
    with pytest.raises(MinosSubnetSourceAttestationError):
        _score(oracle, tmp_path)


@pytest.mark.parametrize("relative", ["utils/scoring.py", "templates/tool_params.py"])
def test_a_mutation_during_the_score_itself_is_rejected(
    upstream: Any, tmp_path: Path, relative: str
) -> None:
    """The bridge re-hashes after scoring, so a mid-score edit cannot pass."""
    root, _commit, authority = upstream
    with pytest.raises(MinosSubnetSourceAttestationError, match="during scoring|refused its own"):
        _score(_oracle(root, authority), tmp_path, mutate_during_score=relative)


def test_a_mutation_after_the_bridge_returns_is_rejected(upstream: Any, tmp_path: Path) -> None:
    """The parent re-derives the identity AFTER the subprocess exits, so a late edit is caught."""
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    real_invoke = oracle._invoke_bridge

    def _mutating(request: dict[str, Any], **kwargs: Any) -> Any:
        response = real_invoke(request, **kwargs)
        if request.get("mode") == "score":
            _append(root, "neurons/validator.py")
        return response

    object.__setattr__(oracle, "_invoke_bridge", _mutating)
    with pytest.raises(
        MinosSubnetSourceAttestationError, match="after scoring|changed while scoring"
    ):
        _score(oracle, tmp_path)


def test_unchanged_container_refs_do_not_excuse_a_changed_source(
    upstream: Any, tmp_path: Path
) -> None:
    """The control that matters: the previous corrective checked exactly these two references.

    A mutation that leaves both untouched while changing the scientific source must still be
    rejected, or the container check would be mistaken for a source check.
    """
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    before = (root / "utils" / "scoring.py").read_text()
    _mutating_between_preflight_and_score(oracle, root, "utils/scoring.py")

    with pytest.raises(MinosSubnetSourceAttestationError):
        _score(oracle, tmp_path)

    after = (root / "utils" / "scoring.py").read_text()
    # both container references are byte-identical across the mutation, so the previous
    # corrective's checks would have seen nothing wrong at all.
    for reference in ('"fake/bcftools:1.20--test"', '"fake/happy@sha256:" + "a" * 64'):
        assert reference in before and reference in after
    assert before != after


def test_a_bridge_reporting_the_wrong_head_is_rejected(upstream: Any, tmp_path: Path) -> None:
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    real_invoke = oracle._invoke_bridge

    def _forging(request: dict[str, Any], **kwargs: Any) -> Any:
        response = real_invoke(request, **kwargs)
        response["source_attestation"]["git_head"] = "f" * 40
        return response

    object.__setattr__(oracle, "_invoke_bridge", _forging)
    with pytest.raises(MinosSubnetSourceAttestationError, match="git HEAD"):
        _score(oracle, tmp_path)


@pytest.mark.parametrize("stage", ["before_import", "after_import", "after_score"])
def test_a_bridge_reporting_a_wrong_source_hash_is_rejected(
    upstream: Any, tmp_path: Path, stage: str
) -> None:
    """Correct HEAD is not enough — every snapshot must match the authority byte for byte."""
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    real_invoke = oracle._invoke_bridge

    def _forging(request: dict[str, Any], **kwargs: Any) -> Any:
        response = real_invoke(request, **kwargs)
        if request.get("mode") == "score":
            response["source_attestation"][stage]["utils/scoring.py"] = "c" * 64
        return response

    object.__setattr__(oracle, "_invoke_bridge", _forging)
    with pytest.raises(MinosSubnetSourceAttestationError, match="does not record|disagree"):
        _score(oracle, tmp_path)


def test_a_missing_attestation_is_rejected(upstream: Any, tmp_path: Path) -> None:
    """A score with no attestation cannot be attributed to any source bytes at all."""
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    real_invoke = oracle._invoke_bridge

    def _stripping(request: dict[str, Any], **kwargs: Any) -> Any:
        response = real_invoke(request, **kwargs)
        if request.get("mode") == "score":
            response.pop("source_attestation", None)
        return response

    object.__setattr__(oracle, "_invoke_bridge", _stripping)
    with pytest.raises(MinosSubnetSourceAttestationError, match="no source attestation"):
        _score(oracle, tmp_path)


@pytest.mark.parametrize("module", ["utils.scoring", "neurons.validator"])
def test_a_module_imported_from_outside_the_pinned_root_is_rejected(
    upstream: Any, tmp_path: Path, module: str
) -> None:
    """A shadowing path on sys.path must not be able to supply the scientific implementation."""
    root, _commit, authority = upstream
    oracle = _oracle(root, authority)
    real_invoke = oracle._invoke_bridge

    def _shadowing(request: dict[str, Any], **kwargs: Any) -> Any:
        response = real_invoke(request, **kwargs)
        response["provenance"]["module_files"][module] = "/usr/lib/python3/site-packages/x.py"
        response["provenance"]["modules_within_root"] = False
        return response

    object.__setattr__(oracle, "_invoke_bridge", _shadowing)
    with pytest.raises(MinosSubnetSourceAttestationError, match="beneath the pinned root"):
        _score(oracle, tmp_path)


def test_the_pinned_root_wins_over_a_shadowing_checkout(upstream: Any, tmp_path: Path) -> None:
    """The isolation property, driven for real: a rival checkout on PYTHONPATH must not supply
    the scientific implementation.

    The bridge puts the pinned root FIRST on ``sys.path``, so the generic names resolve there
    even when another complete checkout is both the working directory and on ``PYTHONPATH``. The
    attestation then proves where they actually came from — which is what makes this checkable
    rather than merely intended.
    """
    root, _commit, _authority = upstream
    shadow = tmp_path / "shadow"
    _make_checkout(shadow)

    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    request_path, response_path = work / "request.json", work / "response.json"
    request_path.write_text(json.dumps({"upstream_root": str(root), "mode": "probe"}))
    from minos_engine.evaluation import _minos_subnet_bridge

    completed = subprocess.run(
        [sys.executable, _minos_subnet_bridge.__file__, str(request_path), str(response_path)],
        cwd=str(shadow),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(shadow)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(response_path.read_text())
    assert "source_authority_error" not in response, response
    module_files = response["provenance"]["module_files"]
    for name, path in module_files.items():
        assert Path(path).is_relative_to(root), f"{name} resolved to {path}"
        assert not Path(path).is_relative_to(shadow), f"{name} came from the shadow checkout"
    assert response["provenance"]["modules_within_root"] is True


def test_the_bridge_refuses_an_out_of_root_import(tmp_path: Path) -> None:
    """And if a module ever DID resolve outside the pinned root, the subprocess refuses itself.

    Driven by pointing the bridge at a root whose authority files exist but whose packages are
    importable only from elsewhere, so the in-root check is the thing that fires.
    """
    real = tmp_path / "real"
    _make_checkout(real)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    _git(decoy, "config", "user.email", "fixture@example.invalid")
    _git(decoy, "config", "user.name", "fixture")
    # the decoy carries the authority FILES but no importable packages of its own.
    for relative in _AUTHORITY_FILES:
        target = decoy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((real / relative).read_text(), encoding="utf-8")
    _git(decoy, "add", "-A")
    _git(decoy, "commit", "-q", "-m", "decoy")

    work = tmp_path / "attempt"
    work.mkdir(exist_ok=True)
    request_path, response_path = work / "request.json", work / "response.json"
    request_path.write_text(json.dumps({"upstream_root": str(decoy), "mode": "probe"}))
    from minos_engine.evaluation import _minos_subnet_bridge

    completed = subprocess.run(
        [sys.executable, _minos_subnet_bridge.__file__, str(request_path), str(response_path)],
        cwd=str(real),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(real)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(response_path.read_text())
    assert "source_authority_error" in response, response
    assert "outside the pinned root" in response["source_authority_error"]
    assert response["scored"] is False
    assert "metrics" not in response
