"""F5-B pure execution contracts + frozen result_hash — unit tests (no I/O, no process)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.execution_contract import (
    EXECUTION_RESULT_SCHEMA,
    RESULT_HASH_DOMAIN,
    ExecutionConfig,
    ExecutionFailure,
    ExecutionInput,
    ExecutionResultManifest,
    GatkExecutionOutcome,
    LogicalGatkInvocation,
    build_result_manifest_bytes,
    compute_result_hash,
)

_H = {c: c * 64 for c in "0123456789abcdef"}


def _inputs(**over: Any) -> ExecutionInput:
    base: dict[str, Any] = {
        "dataset_id": "minos-chr18-0001",
        "round_id": "r1",
        "chromosome": "chr18",
        "profile_id": "p1",
        "content_hash": _H["1"],
        "feature_values_hash": _H["2"],
        "bam_sha256": _H["3"],
        "bai_sha256": _H["4"],
        "reference_sha256": _H["5"],
        "fai_sha256": _H["6"],
        "dictionary_sha256": _H["7"],
        "bam_size_bytes": 1024,
        "region_hash": _H["8"],
        "region_start0": 100,
        "region_end0_exclusive": 200,
    }
    base.update(over)
    return ExecutionInput(**base)


def _config(**over: Any) -> ExecutionConfig:
    base: dict[str, Any] = {
        "config_hash": _H["9"],
        "parameter_space_hash": _H["a"],
        "config_index": 0,
        "effective_config": {"min_pruning": 2},
    }
    base.update(over)
    return ExecutionConfig(**base)


def _invocation(**over: Any) -> LogicalGatkInvocation:
    base: dict[str, Any] = {
        "tool": "HaplotypeCaller",
        "region_token": "chr18:101-200",
        "logical_argv": ("HaplotypeCaller", "-R", "<reference.fa>"),
        "gatk_executable_sha256": _H["b"],
        "gatk_runtime_bundle_sha256": _H["d"],
        "gatk_version": "4.5.0.0",
    }
    base.update(over)
    return LogicalGatkInvocation(**base)


def _outcome(**over: Any) -> GatkExecutionOutcome:
    base: dict[str, Any] = {
        "exit_code": 0,
        "runtime_ms": 1234,
        "vcf_sha256": _H["c"],
        "vcf_size_bytes": 512,
    }
    base.update(over)
    return GatkExecutionOutcome(**base)


def _result_hash(**over: Any) -> str:
    kwargs: dict[str, Any] = {
        "plan_hash": _H["d"],
        "job_key": _H["e"],
        "inputs": _inputs(),
        "config": _config(),
        "invocation": _invocation(),
        "outcome": _outcome(),
    }
    kwargs.update(over)
    return compute_result_hash(**kwargs)


# --------------------------------------------------------------------------- #
# frozen result_hash formula
# --------------------------------------------------------------------------- #
def test_result_hash_is_deterministic_and_domain_separated() -> None:
    first, second = _result_hash(), _result_hash()
    assert first == second and len(first) == 64
    # a bare canonical hash WITHOUT the domain prefix must differ.
    content = {"schema_version": EXECUTION_RESULT_SCHEMA}
    assert first != sha256_hex(canonical_json_bytes(content))
    assert RESULT_HASH_DOMAIN == "minos:l2f-gatk-execution-result:v1\n"


@pytest.mark.parametrize(
    "mutation",
    [
        {"plan_hash": _H["0"]},
        {"job_key": _H["0"]},
        {"inputs": _inputs(dataset_id="other")},
        {"inputs": _inputs(profile_id="other")},
        {"inputs": _inputs(content_hash=_H["0"])},
        {"inputs": _inputs(feature_values_hash=_H["0"])},
        {"inputs": _inputs(bam_sha256=_H["0"])},
        {"inputs": _inputs(bai_sha256=_H["0"])},
        {"inputs": _inputs(reference_sha256=_H["0"])},
        {"inputs": _inputs(fai_sha256=_H["0"])},
        {"inputs": _inputs(region_hash=_H["0"])},
        {"inputs": _inputs(region_start0=1)},
        {"inputs": _inputs(region_end0_exclusive=999)},
        {"inputs": _inputs(chromosome="chr19")},
        {"config": _config(config_hash=_H["0"])},
        {"config": _config(parameter_space_hash=_H["0"])},
        {"invocation": _invocation(logical_argv=("HaplotypeCaller", "-R", "<other>"))},
        {"invocation": _invocation(gatk_executable_sha256=_H["0"])},
        {"invocation": _invocation(gatk_version="9.9.9")},
        {"outcome": _outcome(vcf_sha256=_H["0"])},
        {"outcome": _outcome(vcf_size_bytes=1)},
    ],
)
def test_every_scientific_field_changes_the_result_hash(mutation: dict[str, Any]) -> None:
    assert _result_hash(**mutation) != _result_hash()


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": _outcome(runtime_ms=999999)},
        {"outcome": _outcome(exit_code=0, stderr_sha256=_H["f"])},
        {"inputs": _inputs(round_id="r99")},
        {"inputs": _inputs(dictionary_sha256=_H["0"])},
        {"inputs": _inputs(bam_size_bytes=99)},
        {"config": _config(config_index=7)},
    ],
)
def test_non_scientific_fields_do_not_change_the_result_hash(mutation: dict[str, Any]) -> None:
    """Runtime, stderr, round id, dictionary provenance, BAM size and candidate index are NOT
    part of the frozen scientific identity."""
    assert _result_hash(**mutation) == _result_hash()


def test_result_hash_excludes_host_paths_uuids_timestamps_and_worker() -> None:
    """The manifest may carry them; the result_hash preimage must not."""
    manifest = ExecutionResultManifest(
        schema_version="l2f-gatk-execution-result-v1",
        plan_hash=_H["d"],
        job_id="11111111-2222-3333-4444-555555555555",
        job_key=_H["e"],
        dataset_id="minos-chr18-0001",
        round_id="r1",
        profile_id="p1",
        content_hash=_H["1"],
        feature_values_hash=_H["2"],
        config_hash=_H["9"],
        parameter_space_hash=_H["a"],
        input_identity_hash=_inputs().identity_hash(),
        bam_sha256=_H["3"],
        bai_sha256=_H["4"],
        reference_sha256=_H["5"],
        fai_sha256=_H["6"],
        dictionary_sha256=_H["7"],
        bam_size_bytes=1024,
        region_hash=_H["8"],
        region_start0=100,
        region_end0_exclusive=200,
        chromosome="chr18",
        logical_argv_hash=_invocation().argv_hash(),
        gatk_executable_sha256=_H["b"],
        gatk_runtime_bundle_sha256=_H["c"],
        gatk_version="4.5.0.0",
        vcf_sha256=_H["c"],
        vcf_size_bytes=512,
        result_hash=_result_hash(),
        runtime_ms=1234,
        worker_id="worker-1",
        generated_at="2026-08-21T00:00:00+00:00",
    )
    raw = build_result_manifest_bytes(manifest)
    doc = json.loads(raw)
    # the manifest carries the non-scientific fields...
    for key in ("job_id", "runtime_ms", "worker_id", "generated_at", "result_hash"):
        assert key in doc
    # ...and every scientific input component, so result_hash is recomputable from the manifest.
    for key in ("bam_sha256", "reference_sha256", "region_hash", "region_start0", "chromosome"):
        assert key in doc
    # ...and its own artifact SHA is a DIFFERENT value from result_hash.
    assert sha256_hex(raw) != manifest.result_hash
    assert canonical_json_bytes(doc) == raw


def test_input_identity_and_argv_hashes_are_domain_separated() -> None:
    a, b = _inputs().identity_hash(), _invocation().argv_hash()
    assert len(a) == 64 and len(b) == 64 and a != b


# --------------------------------------------------------------------------- #
# strict contracts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["", "XYZ", "A" * 64, _H["0"][:63], 123, None])
def test_hex64_fields_reject_malformed_values(bad: Any) -> None:
    with pytest.raises(ValidationError):
        _inputs(bam_sha256=bad)


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        ExecutionInput(**{**_inputs().model_dump(), "surprise": 1})


def test_contracts_are_frozen() -> None:
    inputs = _inputs()
    with pytest.raises(ValidationError):
        inputs.dataset_id = "mutated"  # type: ignore[misc]


def test_vcf_size_must_be_positive_and_runtime_nonnegative() -> None:
    with pytest.raises(ValidationError):
        _outcome(vcf_size_bytes=0)
    with pytest.raises(ValidationError):
        _outcome(runtime_ms=-1)


def test_failure_code_vocabulary_is_bounded() -> None:
    for code in (
        "PREPARATION_FAILED",
        "GATK_NONZERO_EXIT",
        "GATK_TIMEOUT",
        "GATK_OUTPUT_INVALID",
        "GATK_OUTPUT_MISSING",
        "EXECUTION_ERROR",
    ):
        assert ExecutionFailure(failure_code=code).failure_code == code  # type: ignore[arg-type]
    for bad in ("CANCELLED", "SOMETHING_ELSE", ""):
        with pytest.raises(ValidationError):
            ExecutionFailure(failure_code=bad)  # type: ignore[arg-type]


def test_failure_record_carries_no_stderr_bytes_or_free_text() -> None:
    failure = ExecutionFailure(failure_code="GATK_NONZERO_EXIT", exit_code=3, stderr_sha256=_H["f"])
    fields = set(failure.model_dump())
    assert fields == {"failure_code", "exit_code", "stderr_sha256"}
    assert "message" not in fields and "stderr" not in fields
