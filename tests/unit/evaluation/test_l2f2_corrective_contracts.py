"""L2-F2-A corrective Tier-1 controls — publisher safety, parser fidelity, record identity.

No database, no Docker, no GATK, no hap.py, no truth corpus. Everything here is filesystem and
pure computation, so these run in the cheap tier and still pin the properties that make the
production path safe.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from minos_engine.evaluation.artifact_publisher import (
    EVALUATION_METRICS_PROVENANCE,
    EvaluationArtifactPublisher,
    EvaluationPublishError,
    evaluation_artifact_root_from_env,
)
from minos_engine.evaluation.contracts import (
    EVALUATION_METRICS_MEDIA_TYPE,
    ComparisonScope,
    EvaluationInputs,
    TruthIdentity,
    build_metrics_artifact_bytes,
)
from minos_engine.evaluation.evaluator import (
    EvaluationRecordError,
    build_evaluation_record,
    evaluate_metrics,
)
from minos_engine.evaluation.happy_metrics import (
    compute_mutation_only_metrics,
    parse_assessed_only_metrics,
    parse_happy_outputs,
    parse_region_overcall_metrics,
    parse_summary_csv,
)
from minos_engine.evaluation.happy_runner import HappyOutputError
from minos_engine.evaluation.scoring_contract import load_scoring_authority

_H = {c: c * 64 for c in "0123456789abcdef"}
_PAYLOAD = b'{"schema_version":"l2f2-evaluation-metrics-v1"}'


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _root(tmp_path: Path, *, mode: int = 0o2750) -> Path:
    root = tmp_path / "evaluation_artifacts"
    root.mkdir(exist_ok=True)
    os.chmod(root, mode)
    return root


def _final(tmp_path: Path, payload: bytes = _PAYLOAD) -> Path:
    return _root(tmp_path) / f"{hashlib.sha256(payload).hexdigest()}.json"


# --------------------------------------------------------------------------- #
# PUBLISHER — the audited protocol, not a second weaker one
# --------------------------------------------------------------------------- #
def test_the_evaluation_publisher_uses_the_audited_shared_protocol(tmp_path: Path) -> None:
    """Structural: ONE implementation, shared with the already-audited L2-F1 result publisher.

    If a future change gives evaluation its own link/verify routine, this fails — which is the
    whole point of factoring rather than copying.
    """
    from minos_engine.storage.content_addressed_publisher import ContentAddressedStore
    from minos_engine.storage.l2f_result_publisher import ResultArtifactPublisher

    result_root = tmp_path / "resroot"
    result_root.mkdir()
    os.chmod(result_root, 0o2750)

    evaluation_publisher = EvaluationArtifactPublisher(_root(tmp_path))
    result_publisher = ResultArtifactPublisher(result_root)

    assert isinstance(evaluation_publisher._store, ContentAddressedStore)
    assert isinstance(result_publisher._store, ContentAddressedStore)
    assert type(evaluation_publisher._store) is type(result_publisher._store)


def test_a_published_metrics_file_has_exactly_the_frozen_credentials(tmp_path: Path) -> None:
    published = EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)
    info = published.path.lstat()
    assert not stat.S_ISLNK(info.st_mode)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_uid == os.getuid()
    assert info.st_gid == _root(tmp_path).stat().st_gid
    assert published.path.name.endswith(".json")
    assert published.media_type == EVALUATION_METRICS_MEDIA_TYPE
    assert published.provenance == EVALUATION_METRICS_PROVENANCE
    assert published.size_bytes == len(_PAYLOAD)


def test_a_restrictive_umask_cannot_weaken_the_published_mode(tmp_path: Path) -> None:
    """The mode is set with fchmod on the temp inode, so the process umask is irrelevant."""
    previous = os.umask(0o077)
    try:
        published = EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(published.path.stat().st_mode) == 0o640


def test_a_symlink_at_the_final_path_is_refused_and_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_bytes(_PAYLOAD)
    _final(tmp_path).symlink_to(target)

    with pytest.raises(EvaluationPublishError, match="symlink|regular file"):
        EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)
    assert _final(tmp_path).is_symlink(), "the planted symlink must be left untouched"


def test_a_preexisting_object_with_wrong_bytes_is_refused(tmp_path: Path) -> None:
    """A file occupying a content address whose bytes are NOT that content is a refusal."""
    path = _final(tmp_path)
    path.write_bytes(b"not the claimed content")
    os.chmod(path, 0o640)

    with pytest.raises(EvaluationPublishError, match="content hash"):
        EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)
    assert path.read_bytes() == b"not the claimed content", "the existing object is unchanged"


def test_a_preexisting_object_with_the_wrong_mode_is_refused(tmp_path: Path) -> None:
    path = _final(tmp_path)
    path.write_bytes(_PAYLOAD)
    os.chmod(path, 0o644)

    with pytest.raises(EvaluationPublishError, match="wrong mode"):
        EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)


def test_a_preexisting_correct_object_is_verified_and_reused(tmp_path: Path) -> None:
    publisher = EvaluationArtifactPublisher(_root(tmp_path))
    first = publisher.publish(_PAYLOAD)
    before = first.path.lstat()

    second = publisher.publish(_PAYLOAD)

    assert second.created is False
    after = second.path.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), "same inode reused"


def test_concurrent_identical_publishers_converge_on_one_inode(tmp_path: Path) -> None:
    publisher = EvaluationArtifactPublisher(_root(tmp_path))
    results: list[Any] = []
    barrier = threading.Barrier(4)

    def _publish() -> None:
        barrier.wait()
        results.append(publisher.publish(_PAYLOAD))

    threads = [threading.Thread(target=_publish) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert len(results) == 4
    assert len({(r.path.stat().st_dev, r.path.stat().st_ino) for r in results}) == 1
    assert sum(1 for r in results if r.created) == 1, "exactly one publisher created the inode"
    assert len(list(_root(tmp_path).iterdir())) == 1, "no temp inode was left behind"


def test_a_failed_publish_leaves_no_partial_temp_inode(tmp_path: Path) -> None:
    path = _final(tmp_path)
    path.write_bytes(b"conflicting")
    os.chmod(path, 0o640)

    with pytest.raises(EvaluationPublishError):
        EvaluationArtifactPublisher(_root(tmp_path)).publish(_PAYLOAD)

    leftovers = [p.name for p in _root(tmp_path).iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_rollback_only_removes_an_inode_this_call_created(tmp_path: Path) -> None:
    publisher = EvaluationArtifactPublisher(_root(tmp_path))
    created = publisher.publish(_PAYLOAD)
    reused = publisher.publish(_PAYLOAD)

    publisher.unpublish_if_created(reused)  # created=False -> must be a no-op
    assert created.path.exists(), "a reused object must never be unpublished"

    publisher.unpublish_if_created(created)
    assert not created.path.exists()


def test_the_artifact_root_env_resolver_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINOS_L2F2_EVALUATION_ARTIFACT_ROOT", raising=False)
    with pytest.raises(EvaluationPublishError, match="is not set"):
        evaluation_artifact_root_from_env()

    monkeypatch.setenv("MINOS_L2F2_EVALUATION_ARTIFACT_ROOT", str(_root(tmp_path, mode=0o755)))
    with pytest.raises(EvaluationPublishError, match="mode"):
        evaluation_artifact_root_from_env()

    monkeypatch.setenv("MINOS_L2F2_EVALUATION_ARTIFACT_ROOT", str(_root(tmp_path)))
    assert evaluation_artifact_root_from_env() == _root(tmp_path)


# --------------------------------------------------------------------------- #
# PARSER — faithful to the audited upstream pipeline
# --------------------------------------------------------------------------- #
_SUMMARY = (
    "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.TOTAL,QUERY.FP,QUERY.UNK,"
    "METRIC.Recall,METRIC.Precision,METRIC.Frac_NA,METRIC.F1_Score,"
    "TRUTH.TOTAL.TiTv_ratio,QUERY.TOTAL.TiTv_ratio,"
    "TRUTH.TOTAL.het_hom_ratio,QUERY.TOTAL.het_hom_ratio\n"
    "SNP,ALL,999,999,0,9999,0,9000,0.11,0.11,0.99,0.11,9.0,9.0,9.0,9.0\n"
    "SNP,PASS,100,90,10,120,5,0,0.9,0.95,0.5,0.92,2.0,2.1,1.5,1.6\n"
    "INDEL,ALL,99,99,0,999,0,900,0.11,0.11,0.99,0.11,,,9.0,9.0\n"
    "INDEL,PASS,10,8,2,11,1,0,0.8,0.9,0.4,0.85,,,1.0,1.1\n"
)

_VCF = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n"
    "chr18\t1100\t.\tC\tA\t.\tPASS\t.\tBD:BVT:BI:BLT\tFN:SNP:tv:homalt\t.:.:.:.\n"
    "chr18\t1200\t.\tG\tT\t.\tPASS\t.\tBD:BVT:BI:BLT\t.:.:.:.\tFP:SNP:tv:het\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:INDEL:.:het\tTP:INDEL:.:het\n"
)

_MUTATIONS = (
    "#CHROM\tPOS\tID\tREF\tALT\nchr18\t1000\t.\tA\tG\nchr18\t1100\t.\tC\tA\nchr18\t2000\t.\tAT\tA\n"
)


def _write_outputs(tmp_path: Path, *, summary: str = _SUMMARY, vcf: str = _VCF) -> Path:
    prefix = tmp_path / "happy_out"
    Path(f"{prefix}.summary.csv").write_text(summary, encoding="utf-8")
    Path(f"{prefix}.vcf.gz").write_bytes(gzip.compress(vcf.encode("utf-8")))
    (tmp_path / "mutations.vcf.gz").write_bytes(gzip.compress(_MUTATIONS.encode("utf-8")))
    return prefix


def test_only_pass_rows_enter_the_base_metrics(tmp_path: Path) -> None:
    """The ALL rows are deliberately ignored — upstream scores the PASS row alone."""
    metrics = parse_summary_csv(Path(f"{_write_outputs(tmp_path)}.summary.csv"))
    assert metrics["f1_snp"] == pytest.approx(0.92)
    assert metrics["truth_total_snp"] == pytest.approx(100.0)
    assert metrics["f1_indel"] == pytest.approx(0.85)
    assert metrics["truth_total_indel"] == pytest.approx(10.0)
    assert metrics["weighted_f1"] == pytest.approx(0.7 * 0.92 + 0.3 * 0.85)


def test_a_missing_or_empty_summary_is_unusable_output_not_a_zero_score(tmp_path: Path) -> None:
    with pytest.raises(HappyOutputError, match="missing or unsafe"):
        parse_summary_csv(tmp_path / "absent.summary.csv")

    empty = tmp_path / "empty.summary.csv"
    empty.write_text("Type,Filter,METRIC.F1_Score\n", encoding="utf-8")
    with pytest.raises(HappyOutputError, match="no SNP/INDEL rows"):
        parse_summary_csv(empty)


def test_assessed_only_metrics_replace_the_region_polluted_summary_values(
    tmp_path: Path,
) -> None:
    """summary.csv counts the WHOLE query VCF; only assessed variants are scientifically valid."""
    prefix = _write_outputs(tmp_path)
    assessed = parse_assessed_only_metrics(Path(f"{prefix}.vcf.gz"))
    assert assessed is not None
    # assessed query-side SNPs: one TP + one FP = 2 (the FN is truth-side only)
    assert assessed["query_total_snp"] == 2
    assert assessed["query_total_indel"] == 1
    assert assessed["frac_na_snp"] == 0.0
    assert assessed["titv_query_snp"] == pytest.approx(1.0)  # 1 ti / 1 tv
    assert assessed["titv_truth_snp"] == pytest.approx(1.0)
    assert assessed["hethom_truth_snp"] == pytest.approx(1.0)  # 1 het / 1 homalt
    # no query-side homalt SNP -> the ratio is omitted rather than fabricated
    assert "hethom_query_snp" not in assessed


def test_mutation_only_metrics_recount_against_the_targets(tmp_path: Path) -> None:
    prefix = _write_outputs(tmp_path)
    metrics = compute_mutation_only_metrics(Path(f"{prefix}.vcf.gz"), tmp_path / "mutations.vcf.gz")
    assert (metrics["tp_snp"], metrics["fn_snp"], metrics["fp_snp"]) == (1.0, 1.0, 0.0)
    assert metrics["tp_indel"] == 1.0
    assert metrics["truth_total_snp"] == 2.0
    assert metrics["query_total_snp"] == 1.0
    assert metrics["recall_snp"] == pytest.approx(0.5)
    assert metrics["precision_snp"] == pytest.approx(1.0)
    assert metrics["f1_snp"] == pytest.approx(2 / 3)


def test_an_off_target_false_positive_is_not_counted_as_a_mutation_miss(
    tmp_path: Path,
) -> None:
    """The FP at chr18:1200 is not a target, so it must not enter the mutation-only counts."""
    prefix = _write_outputs(tmp_path)
    metrics = compute_mutation_only_metrics(Path(f"{prefix}.vcf.gz"), tmp_path / "mutations.vcf.gz")
    assert metrics["fp_snp"] == 0.0
    # ... but it IS counted by the region-wide overcall guardrail
    overcall = parse_region_overcall_metrics(Path(f"{prefix}.vcf.gz"), 3.0, 2.0)
    assert overcall is not None
    assert overcall["region_fp_snp"] == 1.0
    assert overcall["region_fp_total"] == 1.0


@pytest.mark.parametrize(
    ("region_fp", "truth_total", "snp_truth_total", "expected"),
    [
        (0, 10.0, 5.0, 0.0),
        (100, 10.0, 5.0, 0.0),  # fp_per_target=10.0 is NOT > 10.0
        (110, 10.0, 5.0, 4.0),  # (11 - 10) * 4
        (10000, 10.0, 5.0, 45.0),  # clamped at 45
    ],
)
def test_the_overcall_penalty_reproduces_the_upstream_guardrail(
    tmp_path: Path, region_fp: int, truth_total: float, snp_truth_total: float, expected: float
) -> None:
    lines = [
        "##fileformat=VCFv4.2",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY",
    ]
    lines.extend(
        f"chr18\t{3000 + i}\t.\tG\tT\t.\tPASS\t.\tBD:BVT\t.:.\tFP:SNP" for i in range(region_fp)
    )
    path = tmp_path / "overcall.vcf.gz"
    path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode("utf-8")))

    overcall = parse_region_overcall_metrics(path, truth_total, snp_truth_total)

    assert overcall is not None
    assert overcall["overcall_penalty"] == pytest.approx(expected)


def test_the_full_parse_applies_every_stage_in_the_audited_order(tmp_path: Path) -> None:
    prefix = _write_outputs(tmp_path)
    parsed = parse_happy_outputs(prefix, tmp_path / "mutations.vcf.gz")

    # mutation-only wins over assessed-only, which wins over the raw summary
    assert parsed.happy_metrics["truth_total_snp"] == 2.0
    assert parsed.happy_metrics["f1_snp"] == pytest.approx(2 / 3)
    # ratios survive from the assessed stage (mutation-only does not compute them)
    assert parsed.happy_metrics["titv_query_snp"] == pytest.approx(1.0)
    # the summary's polluted ratio is gone
    assert parsed.happy_metrics["titv_query_snp"] != pytest.approx(2.1)
    assert parsed.happy_metrics["overcall_penalty"] == 0.0
    assert parsed.mutation_only_metrics and parsed.assessed_only_metrics and parsed.overcall


def test_an_unreadable_annotated_vcf_is_bounded_unusable_output(tmp_path: Path) -> None:
    prefix = _write_outputs(tmp_path)
    Path(f"{prefix}.vcf.gz").write_bytes(b"not gzip at all")

    with pytest.raises(HappyOutputError):
        parse_happy_outputs(prefix, tmp_path / "mutations.vcf.gz")


def test_mutations_without_any_target_are_unusable_output(tmp_path: Path) -> None:
    prefix = _write_outputs(tmp_path)
    (tmp_path / "mutations.vcf.gz").write_bytes(gzip.compress(b"#only a header\n"))

    with pytest.raises(HappyOutputError, match="no target mutations"):
        parse_happy_outputs(prefix, tmp_path / "mutations.vcf.gz")


# --------------------------------------------------------------------------- #
# RECORD + AUTHORITY — one construction path, one identity
# --------------------------------------------------------------------------- #
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


_METRICS = {
    "f1_snp": 0.95,
    "f1_indel": 0.9,
    "recall_snp": 0.95,
    "recall_indel": 0.9,
    "truth_total_snp": 1000,
    "truth_total_indel": 100,
    "query_total_snp": 1000,
    "query_total_indel": 100,
    "fp_snp": 2,
    "fp_indel": 1,
}


def _built(tmp_path: Path, **over: Any) -> Any:
    authority = load_scoring_authority(_repo_root())
    inputs = _inputs()
    artifact, breakdown, admission, _contract = evaluate_metrics(
        inputs=inputs,
        happy_metrics=dict(_METRICS),
        mutation_only_metrics={},
        assessed_only_metrics={},
        overcall={},
        authority=authority,
    )
    published = EvaluationArtifactPublisher(_root(tmp_path)).publish(
        build_metrics_artifact_bytes(artifact)
    )
    kwargs: dict[str, Any] = {
        "execution_result_id": "11111111-1111-1111-1111-111111111111",
        "inputs": inputs,
        "artifact": artifact,
        "breakdown": breakdown,
        "admission_code": admission,
        "authority": authority,
        "metrics_artifact_id": "22222222-2222-2222-2222-222222222222",
        "metrics": published,
    }
    kwargs.update(over)
    return build_evaluation_record(**kwargs)


def test_a_valid_record_derives_its_own_hash_and_contract(tmp_path: Path) -> None:
    record = _built(tmp_path)
    assert len(record.evaluation_hash) == 64
    assert len(record.scoring_contract_hash) == 64
    assert record.artifact.scoring_contract_hash == record.scoring_contract_hash


def test_the_evaluation_hash_is_computed_from_exactly_this_record(tmp_path: Path) -> None:
    """Change any scientific component of the record and the identity must move with it."""
    import dataclasses

    record = _built(tmp_path)
    baseline = record.evaluation_hash

    moved = dataclasses.replace(
        record, breakdown=record.breakdown.model_copy(update={"minos_score": 0.123})
    )
    assert moved.evaluation_hash != baseline

    relabelled = dataclasses.replace(record, admission_code="NONPOSITIVE_SCORE")
    assert relabelled.evaluation_hash != baseline


def test_no_public_path_accepts_an_independently_chosen_hash_or_contract() -> None:
    """Sections 25/26: scores and identity may not arrive from two different places."""
    import inspect

    from minos_engine.evaluation import evaluator

    parameters = inspect.signature(evaluator.record_evaluation_result).parameters
    assert set(parameters) == {"engine", "record"}
    for forbidden in ("evaluation_hash", "scoring_contract_hash", "metrics_artifact_sha256"):
        assert forbidden not in parameters, forbidden


def test_an_artifact_scored_under_a_different_contract_cannot_become_a_record(
    tmp_path: Path,
) -> None:
    authority = load_scoring_authority(_repo_root())
    forged = authority.model_copy(update={"upstream_commit": "f" * 40})
    with pytest.raises(EvaluationRecordError, match="different scoring contract"):
        _built(tmp_path, authority=forged)


def test_an_artifact_describing_a_different_execution_cannot_become_a_record(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvaluationRecordError, match="different execution"):
        _built(tmp_path, inputs=_inputs(execution_result_hash=_H["c"]))


def test_a_document_that_is_not_the_artifacts_bytes_cannot_become_a_record(
    tmp_path: Path,
) -> None:
    other = EvaluationArtifactPublisher(_root(tmp_path)).publish(b'{"different":"document"}')
    with pytest.raises(EvaluationRecordError, match="not this artifact's bytes"):
        _built(tmp_path, metrics=other)


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("media_type", "application/json", "media type"),
        ("provenance", "l2f:gatk-vcf", "provenance"),
    ],
)
def test_a_misclassified_document_cannot_become_a_record(
    tmp_path: Path, field: str, value: str, needle: str
) -> None:
    import dataclasses

    authority = load_scoring_authority(_repo_root())
    inputs = _inputs()
    artifact, _b, _a, _c = evaluate_metrics(
        inputs=inputs,
        happy_metrics=dict(_METRICS),
        mutation_only_metrics={},
        assessed_only_metrics={},
        overcall={},
        authority=authority,
    )
    published = EvaluationArtifactPublisher(_root(tmp_path)).publish(
        build_metrics_artifact_bytes(artifact)
    )
    with pytest.raises(EvaluationRecordError, match=needle):
        _built(tmp_path, metrics=dataclasses.replace(published, **{field: value}))
