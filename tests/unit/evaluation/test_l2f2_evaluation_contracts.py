"""L2-F2-A Tier-1 controls — pure contracts, hashes, scoring parity, partition safety.

No database, no Docker, no GATK, no hap.py, no truth corpus. The scoring parity oracle is the
committed golden fixture generated from the audited upstream checkout, so CI never needs a
``minos_subnet`` clone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from minos_engine.evaluation.contracts import (
    EVALUATION_METRICS_MEDIA_TYPE,
    FAILURE_CODES,
    ComparisonScope,
    EvaluationInputs,
    MetricsArtifact,
    TruthIdentity,
    build_metrics_artifact_bytes,
    compute_evaluation_hash,
)
from minos_engine.evaluation.evaluator import (
    EvaluationArtifactPublisher,
    EvaluationPublishError,
    evaluate_metrics,
)
from minos_engine.evaluation.happy_runner import (
    FakeHappyRunner,
    HappyExecutionError,
    HappyTimeoutError,
    build_happy_argv,
)
from minos_engine.evaluation.minos_score import (
    ScoreComputationError,
    compute_advanced_score,
    decide_admission,
)
from minos_engine.evaluation.scoring_contract import (
    SCORING_CONTRACT_VERSION,
    ScoringContractError,
    compute_scoring_contract_hash,
    load_scoring_authority,
)
from minos_engine.evaluation.truth_registration import (
    ForbiddenPartitionError,
    TruthRegistrationError,
    hash_truth_bundle,
    refuse_non_train_partition,
    resolve_truth_bundle,
)

_H = {c: c * 64 for c in "0123456789abcdef"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _golden() -> dict[str, Any]:
    path = _repo_root() / "tests" / "fixtures" / "evaluation" / "l2f2_scoring_golden_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _inputs(**over: Any) -> EvaluationInputs:
    base: dict[str, Any] = {
        "execution_result_hash": _H["a"],
        "dataset_id": "minos-chr18-028662fb934529d7",
        "partition": "train",
        "vcf_sha256": _H["b"],
        "truth": TruthIdentity(
            truth_vcf_sha256=_H["1"],
            truth_tbi_sha256=_H["2"],
            mutations_vcf_sha256=_H["3"],
            mutations_tbi_sha256=_H["4"],
        ),
        "scope": ComparisonScope(
            chromosome="chr18",
            region_start0=0,
            region_end0_exclusive=80373285,
            region_source="chr18:1-80373285",
        ),
    }
    base.update(over)
    return EvaluationInputs(**base)


# --------------------------------------------------------------------------- #
# SCORING PARITY — against the REAL upstream oracle, via the committed fixture
# --------------------------------------------------------------------------- #
def test_the_golden_fixture_is_provenance_bound_to_the_audited_upstream() -> None:
    """The oracle must name the exact commit and file digests the audit froze."""
    oracle = _golden()["oracle"]
    assert oracle["commit"] == "649bb92c6abccebde58a736a2b2af7fd77a701c1"
    assert (
        oracle["scoring_py_sha256"]
        == "7b5aa187adda5978adc029abcd4c96b7b78eafeb9c5641153955175cd0b7b658"
    )
    assert (
        oracle["validator_py_sha256"]
        == "2ac0841231a58794097ba40d245f27eaa44e1bd1b66134a17dece96a1a37f33e"
    )
    assert "AdvancedScorer" in oracle["generated_by"]


def test_the_golden_corpus_covers_every_required_category() -> None:
    names = {case["name"] for case in _golden()["cases"]}
    for required in (
        "normal_high_accuracy",
        "snp_dominant",
        "indel_dominant",
        "low_recall",
        "high_fp",
        "call_count_overcall",
        "titv_mismatch",
        "hethom_mismatch",
        "zero_truth",
        "zero_input_fingerprint",
        "boundary_perfect",
        "overcall_penalty_zeroes_score",
    ):
        assert required in names, required
    admissions = {case["admission"] for case in _golden()["cases"]}
    assert "ADMITTED" in admissions
    assert "NONPOSITIVE_SCORE" in admissions
    assert "ZERO_INPUT_FINGERPRINT" in admissions


@pytest.mark.parametrize("case", _golden()["cases"], ids=lambda c: str(c["name"]))
def test_local_scorer_matches_the_upstream_oracle_exactly(case: dict[str, Any]) -> None:
    """THE parity control: our score must equal the real upstream score, not merely be close."""
    breakdown = compute_advanced_score(dict(case["metrics"]))
    assert breakdown.minos_score_100 == pytest.approx(case["upstream_score_100"], abs=1e-12)
    assert breakdown.minos_score == pytest.approx(case["validator_normalized_score"], abs=1e-12)
    # the /100 normalization the validator applies, reproduced exactly
    assert breakdown.minos_score == pytest.approx(breakdown.minos_score_100 / 100.0, abs=1e-12)


@pytest.mark.parametrize("case", _golden()["cases"], ids=lambda c: str(c["name"]))
def test_admission_matches_the_validator_for_every_golden_case(case: dict[str, Any]) -> None:
    breakdown = compute_advanced_score(dict(case["metrics"]))
    assert decide_admission(dict(case["metrics"]), breakdown) == case["admission"]


def test_a_non_admitted_result_is_not_the_same_as_a_zero_score() -> None:
    """The zero-input fingerprint scores ~25/100 yet the validator refuses it."""
    case = next(c for c in _golden()["cases"] if c["name"] == "zero_input_fingerprint")
    breakdown = compute_advanced_score(dict(case["metrics"]))
    assert breakdown.minos_score > 0.0
    assert decide_admission(dict(case["metrics"]), breakdown) == "ZERO_INPUT_FINGERPRINT"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc"])
def test_non_finite_scorer_input_is_an_evaluation_failure_not_a_low_score(bad: Any) -> None:
    with pytest.raises(ScoreComputationError):
        compute_advanced_score({"f1_snp": bad, "truth_total_snp": 10})


# --------------------------------------------------------------------------- #
# SCORING CONTRACT
# --------------------------------------------------------------------------- #
def test_the_scoring_contract_hash_is_deterministic_and_domain_separated() -> None:
    authority = load_scoring_authority(_repo_root())
    first = compute_scoring_contract_hash(authority)
    assert first == compute_scoring_contract_hash(load_scoring_authority(_repo_root()))
    assert len(first) == 64
    assert SCORING_CONTRACT_VERSION == "l2f2-minos-scoring-v1"


def test_the_contract_hash_covers_semantics_and_never_ranking_policy() -> None:
    """Objective/ranking choices (D1-D8) must not enter the hash, or a later objective change
    would retroactively invalidate every stored evaluation."""
    content = load_scoring_authority(_repo_root()).contract_content()
    for forbidden in ("objective", "cvar", "alpha", "budget", "tie_break", "ranking", "weights_j"):
        assert not any(forbidden in key.lower() for key in content)


@pytest.mark.parametrize("image_key", ["happy_image", "bcftools_image"])
def test_a_tag_pinned_container_is_refused(image_key: str, tmp_path: Path) -> None:
    """Both container identities must be immutable digests; a tag can be moved underneath us."""
    raw = json.loads(
        (_repo_root() / "manifests" / "l2f2_scoring_authority_v1.json").read_text("utf-8")
    )
    key = "happy" if image_key == "happy_image" else "bcftools"
    raw["containers"][key] = "example/image:1.0"
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "l2f2_scoring_authority_v1.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    with pytest.raises(ScoringContractError, match="tag-pinned"):
        load_scoring_authority(tmp_path)


def test_both_container_digests_are_pinned_in_the_committed_manifest() -> None:
    authority = load_scoring_authority(_repo_root())
    assert authority.happy_image.startswith("genonet/hap-py@sha256:")
    assert authority.bcftools_image.startswith("quay.io/biocontainers/bcftools@sha256:")


# --------------------------------------------------------------------------- #
# EVALUATION IDENTITY
# --------------------------------------------------------------------------- #
def _breakdown() -> Any:
    return compute_advanced_score({"f1_snp": 0.9, "f1_indel": 0.8, "truth_total_snp": 100})


def test_the_evaluation_hash_is_deterministic() -> None:
    args: dict[str, Any] = {
        "inputs": _inputs(),
        "scoring_contract_hash": _H["c"],
        "metrics_artifact_sha256": _H["d"],
        "breakdown": _breakdown(),
        "admission_code": "ADMITTED",
    }
    assert compute_evaluation_hash(**args) == compute_evaluation_hash(**args)


@pytest.mark.parametrize(
    "mutation",
    [
        {"execution_result_hash": _H["f"]},
        {"dataset_id": "other-dataset"},
        {"partition": "validation"},
        {"vcf_sha256": _H["e"]},
    ],
)
def test_every_scientific_input_moves_the_evaluation_hash(mutation: dict[str, Any]) -> None:
    common: dict[str, Any] = {
        "scoring_contract_hash": _H["c"],
        "metrics_artifact_sha256": _H["d"],
        "breakdown": _breakdown(),
        "admission_code": "ADMITTED",
    }
    base = compute_evaluation_hash(inputs=_inputs(), **common)
    assert compute_evaluation_hash(inputs=_inputs(**mutation), **common) != base


def test_truth_bytes_move_the_evaluation_hash() -> None:
    common: dict[str, Any] = {
        "scoring_contract_hash": _H["c"],
        "metrics_artifact_sha256": _H["d"],
        "breakdown": _breakdown(),
        "admission_code": "ADMITTED",
    }
    base = compute_evaluation_hash(inputs=_inputs(), **common)
    other = _inputs(
        truth=TruthIdentity(
            truth_vcf_sha256=_H["9"],
            truth_tbi_sha256=_H["2"],
            mutations_vcf_sha256=_H["3"],
            mutations_tbi_sha256=_H["4"],
        )
    )
    assert compute_evaluation_hash(inputs=other, **common) != base


def test_the_scoring_contract_and_admission_move_the_evaluation_hash() -> None:
    common: dict[str, Any] = {
        "inputs": _inputs(),
        "metrics_artifact_sha256": _H["d"],
        "breakdown": _breakdown(),
    }
    base = compute_evaluation_hash(
        scoring_contract_hash=_H["c"], admission_code="ADMITTED", **common
    )
    assert (
        compute_evaluation_hash(scoring_contract_hash=_H["8"], admission_code="ADMITTED", **common)
        != base
    )
    assert (
        compute_evaluation_hash(
            scoring_contract_hash=_H["c"], admission_code="NONPOSITIVE_SCORE", **common
        )
        != base
    )


def test_no_path_timestamp_or_locator_enters_the_evaluation_identity() -> None:
    """Host paths are what broke F7 when the workspace moved; they stay out of science."""
    import inspect

    from minos_engine.evaluation import contracts

    source = inspect.getsource(contracts.compute_evaluation_hash)
    body = source.split("content = {")[1]
    # storage locators and environment facts must never enter the identity. ``dataset_id`` is a
    # scientific identity (which sample), not a locator, so it legitimately stays.
    for forbidden in (
        "path",
        "uri",
        "created_at",
        "timestamp",
        "hostname",
        "uuid",
        "registry_id",
        "artifact_id",
        "execution_result_id",
        "_uid",
        "_gid",
    ):
        assert forbidden not in body.lower(), forbidden
    assert "dataset_id" in body  # the sample identity itself IS bound


# --------------------------------------------------------------------------- #
# METRICS ARTIFACT
# --------------------------------------------------------------------------- #
def _artifact() -> MetricsArtifact:
    inputs = _inputs()
    artifact, _, _, _ = evaluate_metrics(
        inputs=inputs,
        happy_metrics={"f1_snp": 0.9, "f1_indel": 0.8, "truth_total_snp": 100},
        mutation_only_metrics={"f1": 0.7},
        assessed_only_metrics={"f1": 0.75},
        overcall={"overcall_penalty": 0.0},
        authority=load_scoring_authority(_repo_root()),
    )
    return artifact


def test_the_metrics_artifact_is_canonical_and_deterministic() -> None:
    first = build_metrics_artifact_bytes(_artifact())
    assert first == build_metrics_artifact_bytes(_artifact())
    reparsed = json.loads(first)
    assert reparsed["schema_version"] == "l2f2-evaluation-metrics-v1"


def test_the_metrics_artifact_validates_against_its_committed_schema() -> None:
    import jsonschema

    schema = json.loads(
        (_repo_root() / "schemas" / "l2f2-evaluation-metrics-v1.schema.json").read_text("utf-8")
    )
    jsonschema.validate(json.loads(build_metrics_artifact_bytes(_artifact())), schema)


def test_the_metrics_artifact_carries_identities_never_truth_bytes() -> None:
    document = json.loads(build_metrics_artifact_bytes(_artifact()))
    assert set(document["truth_identity"]) == {
        "truth_vcf_sha256",
        "truth_tbi_sha256",
        "mutations_vcf_sha256",
        "mutations_tbi_sha256",
    }
    blob = json.dumps(document)
    for forbidden in ("##fileformat", "#CHROM", "/home/"):
        assert forbidden not in blob


def test_the_metrics_media_type_is_the_contract_value() -> None:
    assert EVALUATION_METRICS_MEDIA_TYPE == "application/vnd.minos.l2f2-evaluation-metrics+json"


# --------------------------------------------------------------------------- #
# ARTIFACT PUBLISHER
# --------------------------------------------------------------------------- #
def _root(tmp_path: Path, mode: int = 0o2750) -> Path:
    root = tmp_path / "evaluation_artifacts"
    root.mkdir()
    os.chmod(root, mode)
    return root


def test_publishing_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    publisher = EvaluationArtifactPublisher(_root(tmp_path))
    payload = build_metrics_artifact_bytes(_artifact())
    digest, uri = publisher.publish(payload)
    again = publisher.publish(payload)
    assert again == (digest, uri)
    assert (
        Path(tmp_path) / "evaluation_artifacts" / f"{digest}.metrics.json"
    ).read_bytes() == payload


def test_a_wrong_mode_artifact_root_is_refused(tmp_path: Path) -> None:
    publisher = EvaluationArtifactPublisher(_root(tmp_path, mode=0o755))
    with pytest.raises(EvaluationPublishError, match="mode"):
        publisher.publish(b"{}")


def test_a_symlinked_artifact_root_is_refused(tmp_path: Path) -> None:
    real = _root(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(EvaluationPublishError, match="symlink"):
        EvaluationArtifactPublisher(link).publish(b"{}")


def test_a_relative_artifact_root_is_refused() -> None:
    with pytest.raises(EvaluationPublishError, match="absolute"):
        EvaluationArtifactPublisher(Path("relative")).publish(b"{}")


# --------------------------------------------------------------------------- #
# TRUTH REGISTRATION — partition safety and path rules
# --------------------------------------------------------------------------- #
def test_validation_and_test_registration_are_refused_with_a_typed_error() -> None:
    refuse_non_train_partition("train")
    for partition in ("validation", "test"):
        with pytest.raises(ForbiddenPartitionError):
            refuse_non_train_partition(partition)


def _round(tmp_path: Path, round_id: str = "abc123") -> Path:
    directory = tmp_path / f"round_{round_id}"
    directory.mkdir()
    for name in ("truth.vcf.gz", "truth.vcf.gz.tbi", "mutations.vcf.gz", "mutations.vcf.gz.tbi"):
        (directory / name).write_bytes(name.encode())
    return directory


def test_truth_paths_are_derived_from_the_registered_round_never_globbed(tmp_path: Path) -> None:
    _round(tmp_path)
    (tmp_path / "round_unregistered").mkdir()  # must never be discovered
    resolved = resolve_truth_bundle(tmp_path, "abc123")
    assert set(resolved) == {
        "truth.vcf.gz",
        "truth.vcf.gz.tbi",
        "mutations.vcf.gz",
        "mutations.vcf.gz.tbi",
    }
    assert all(p.parent.name == "round_abc123" for p in resolved.values())


@pytest.mark.parametrize("round_id", ["", "../escape", "/absolute", ".hidden"])
def test_an_unsafe_round_id_is_refused(tmp_path: Path, round_id: str) -> None:
    with pytest.raises(TruthRegistrationError):
        resolve_truth_bundle(tmp_path, round_id)


def test_a_relative_dataset_root_is_refused() -> None:
    with pytest.raises(TruthRegistrationError, match="absolute"):
        resolve_truth_bundle(Path("relative"), "abc123")


def test_a_symlinked_truth_file_is_refused(tmp_path: Path) -> None:
    directory = _round(tmp_path)
    target = tmp_path / "elsewhere.vcf.gz"
    target.write_bytes(b"elsewhere")
    (directory / "truth.vcf.gz").unlink()
    (directory / "truth.vcf.gz").symlink_to(target)
    with pytest.raises(TruthRegistrationError, match="symlink"):
        hash_truth_bundle(
            dataset_registry_id="d", dataset_id="ds", round_id="abc123", dataset_root=tmp_path
        )


def test_truth_is_bound_by_content_hash(tmp_path: Path) -> None:
    _round(tmp_path)
    import hashlib

    bundle = hash_truth_bundle(
        dataset_registry_id="d", dataset_id="ds", round_id="abc123", dataset_root=tmp_path
    )
    assert bundle.truth_vcf_sha256 == hashlib.sha256(b"truth.vcf.gz").hexdigest()
    assert bundle.mutations_tbi_sha256 == hashlib.sha256(b"mutations.vcf.gz.tbi").hexdigest()
    # nothing path-shaped leaks into the identity
    assert "/" not in bundle.truth_vcf_sha256


def test_a_missing_truth_file_is_refused(tmp_path: Path) -> None:
    directory = _round(tmp_path)
    (directory / "mutations.vcf.gz.tbi").unlink()
    with pytest.raises(TruthRegistrationError, match="does not exist"):
        hash_truth_bundle(
            dataset_registry_id="d", dataset_id="ds", round_id="abc123", dataset_root=tmp_path
        )


# --------------------------------------------------------------------------- #
# FAILURE VOCABULARY
# --------------------------------------------------------------------------- #
def test_the_failure_vocabulary_matches_migration_0009_exactly() -> None:
    """The Python vocabulary and the SQL CHECK constraint must not drift apart."""
    migration = (
        _repo_root() / "migrations" / "versions" / "0009_l2f_evaluation_results.py"
    ).read_text("utf-8")
    for code in FAILURE_CODES:
        assert f'"{code}"' in migration, code
    assert len(FAILURE_CODES) == 9


# --------------------------------------------------------------------------- #
# HAP.PY RUNNER PORT
# --------------------------------------------------------------------------- #
def test_the_happy_argv_is_digest_pinned_network_isolated_and_read_only(tmp_path: Path) -> None:
    argv = build_happy_argv(
        image="genonet/hap-py@sha256:" + "0" * 64,
        truth_vcf=tmp_path / "t" / "truth.vcf.gz",
        query_vcf=tmp_path / "q" / "query.vcf",
        reference=tmp_path / "r" / "chr18.fa",
        region_bed=tmp_path / "b" / "regions.bed",
        output_prefix=Path("out"),
        work_dir=tmp_path / "w",
    )
    assert argv[0] == "docker" and "--network" in argv and "none" in argv
    assert argv.count("--read-only") == 1
    assert sum(1 for a in argv if a.endswith(":ro")) == 4
    assert not any(a.startswith("-") and " " in a for a in argv)  # no shell fragments


def test_a_tag_pinned_happy_image_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HappyExecutionError, match="digest-pinned"):
        build_happy_argv(
            image="genonet/hap-py:latest",
            truth_vcf=tmp_path / "t.vcf.gz",
            query_vcf=tmp_path / "q.vcf",
            reference=tmp_path / "r.fa",
            region_bed=tmp_path / "b.bed",
            output_prefix=Path("out"),
            work_dir=tmp_path,
        )


def test_the_fake_runner_cannot_be_mistaken_for_the_production_runner(tmp_path: Path) -> None:
    from minos_engine.evaluation.happy_runner import SubprocessDockerHappyRunner

    assert FakeHappyRunner.__name__ != SubprocessDockerHappyRunner.__name__
    with pytest.raises(HappyTimeoutError):
        FakeHappyRunner(raise_timeout=True).run(
            truth_vcf=tmp_path / "t",
            query_vcf=tmp_path / "q",
            reference=tmp_path / "r",
            region_bed=tmp_path / "b",
            output_prefix=Path("out"),
            work_dir=tmp_path,
        )
    with pytest.raises(HappyExecutionError):
        FakeHappyRunner(exit_code=3).run(
            truth_vcf=tmp_path / "t",
            query_vcf=tmp_path / "q",
            reference=tmp_path / "r",
            region_bed=tmp_path / "b",
            output_prefix=Path("out"),
            work_dir=tmp_path,
        )


# --------------------------------------------------------------------------- #
# ARCHITECTURE BOUNDARIES
# --------------------------------------------------------------------------- #
def test_the_evaluation_package_never_imports_gatk_execution_or_the_live_controller() -> None:
    """Truth-aware code must not reach into the truth-free execution path or live wiring."""
    import ast

    package = _repo_root() / "src" / "minos_engine" / "evaluation"
    forbidden = ("l2f_gatk_runner", "l2f_execution", "layer2.service", "controller", "twin")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for bad in forbidden:
                    assert bad not in name, f"{path.name} imports {name}"


def test_the_evaluator_module_states_it_never_runs_gatk() -> None:
    import inspect

    from minos_engine.evaluation import evaluator

    doc = inspect.getdoc(evaluator) or ""
    assert "never runs GATK" in doc
