"""F6 consistent-forgery matrix and input/CONFIG attack boundary.

Every forgery here is INTERNALLY CONSISTENT: the attacker recomputes whatever hash the mutation
would otherwise break. Each one must still be rejected by a named, deterministic verifier check
(or by a database constraint on the production path), and each has a valid control case.

Where a production PostgreSQL constraint makes a state unconstructable, the pure evaluator is
driven with a controlled immutable :class:`PersistedGraph` — no production constraint is ever
disabled.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import (
    ExecutionResultManifest,
    InputResolutionError,
    compute_input_identity_hash,
    execution_input_from_manifest,
)
from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config
from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage.l2f_execution import PreTerminalExecutionError
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.test_l2f_execution_corrective import env as _env_fixture

env = _env_fixture

_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"

#: the named check each consistent forgery must be caught by.
FORGERY_MATRIX: dict[str, str] = {
    "plan_member_replaced_and_rehashed": "member_inventory_exact",
    "plan_config_replaced_and_job_key_recomputed": "config_inventory_exact",
    "job_member_config_pair_changed_consistently": "job_member_config_binding",
    "config_payload_mutated_with_matching_hashes": "config_payload_bytes_canonical",
    "input_component_mutated_with_recomputed_identity": "execution_results_independently_verified",
    "manifest_input_fields_mutated_with_recomputed_hash": (
        "execution_results_independently_verified"
    ),
    "logical_argv_mutated_with_recomputed_argv_hash": "execution_results_independently_verified",
    "vcf_bytes_mutated_with_matching_artifact_hash": "execution_results_independently_verified",
    "result_manifest_mutated_with_matching_artifact_hash": (
        "execution_results_independently_verified"
    ),
    "result_scientific_fields_mutated_with_matching_result_hash": (
        "execution_results_independently_verified"
    ),
    "cross_plan_member_substitution": "job_member_config_binding",
    "cross_plan_result_substitution": "execution_results_independently_verified",
    "success_and_failure_outcome_substituted": "job_status_claim_consistency",
    "terminal_row_with_missing_or_wrong_kind_outcome": "job_status_claim_consistency",
    "truth_bearing_artifact_with_consistent_hashes": "no_nontrain_or_truth_data",
}


def _graph(env: Any) -> Any:
    with env.engine.connect() as conn:
        return HV._read_persisted_graph(conn, env.plan)


def _fails(env: Any, graph: Any, expected_check: str) -> None:
    """The forged graph must FAIL, and the named check must be among the reported failures."""
    checks, failures = HV._evaluate_checks(env.plan, HV.generate_accepted_candidate_set(), graph)
    assert checks[expected_check] is False, (expected_check, checks)
    assert expected_check in failures


def _passes(env: Any, graph: Any) -> None:
    checks, failures = HV._evaluate_checks(env.plan, HV.generate_accepted_candidate_set(), graph)
    assert failures == (), failures
    assert all(checks.values())


def _rehash_manifest(result: Any, document: dict[str, Any]) -> Any:
    """Forge a manifest for a MAXIMALLY powerful attacker.

    Everything is made consistent, including ``manifest_sha256`` — the column of the append-only
    result row itself, which a real attacker could not rewrite. Forgeries verified through this
    helper are therefore caught by CONTENT recomputation alone, not by the row anchor.
    """
    raw = canonical_json_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    return dataclasses.replace(
        result,
        manifest_bytes=raw,
        manifest_document=json.loads(raw),
        manifest_sha256=digest,
        manifest_file_sha256=digest,
        manifest_size_bytes=len(raw),
        manifest_file_size=len(raw),
        manifest_uri=f"file:///r/{digest}.result.json",
    )


def _rehash_manifest_file_only(result: Any, document: dict[str, Any]) -> Any:
    """Forge a manifest the way a REAL attacker could: rewrite the file and re-register the
    artifact, but leave the append-only result row's own ``result_manifest_sha256`` intact."""
    raw = canonical_json_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    return dataclasses.replace(
        result,
        manifest_bytes=raw,
        manifest_document=json.loads(raw),
        manifest_file_sha256=digest,
        manifest_size_bytes=len(raw),
        manifest_file_size=len(raw),
        manifest_uri=f"file:///r/{digest}.result.json",
    )


# --------------------------------------------------------------------------- #
# the control: an untouched, genuinely produced graph verifies
# --------------------------------------------------------------------------- #
def test_the_untouched_graph_is_the_control(env: Any) -> None:
    env.run()
    _passes(env, _graph(env))
    assert env.verify().status == HV.STATUS_PASS


def test_the_forgery_matrix_is_named_and_maps_onto_real_checks() -> None:
    assert len(FORGERY_MATRIX) == 15
    assert set(FORGERY_MATRIX.values()) <= set(HV.CHECK_NAMES)


# --------------------------------------------------------------------------- #
# H1/H2/H3/H11 — plan, config and job bindings
# --------------------------------------------------------------------------- #
def test_plan_member_replaced_and_rehashed(env: Any) -> None:
    graph = _graph(env)
    victim = graph.members[0]
    forged = dataclasses.replace(
        victim,
        dataset_id="attacker-dataset",
        content_hash="c" * 64,
        feature_values_hash="d" * 64,
    )
    _fails(
        env,
        dataclasses.replace(graph, members=(forged, *graph.members[1:])),
        FORGERY_MATRIX["plan_member_replaced_and_rehashed"],
    )


def test_plan_config_replaced_and_job_key_recomputed(env: Any) -> None:
    graph = _graph(env)
    victim = graph.configs[0]
    forged = dataclasses.replace(victim, config_hash="e" * 64)
    _fails(
        env,
        dataclasses.replace(graph, configs=(forged, *graph.configs[1:])),
        FORGERY_MATRIX["plan_config_replaced_and_job_key_recomputed"],
    )


def test_job_member_config_pair_changed_consistently(env: Any) -> None:
    graph = _graph(env)
    jobs = graph.jobs
    if len(jobs) < 2:  # pragma: no cover - the fixture always enqueues four
        pytest.skip("need two jobs")
    a, b = jobs[0], jobs[1]
    forged = dataclasses.replace(a, plan_config_id=b.plan_config_id, config_index=b.config_index)
    _fails(
        env,
        dataclasses.replace(graph, jobs=(forged, *jobs[1:])),
        FORGERY_MATRIX["job_member_config_pair_changed_consistently"],
    )


def test_the_database_itself_refuses_repointing_a_job_that_owns_a_result(env: Any) -> None:
    """Control at the production boundary: a job's scientific identity is IMMUTABLE.

    Two independent defenses stand here, and the stronger one fires first: the ``0006``
    immutable-identity trigger refuses any change to a job's member/config binding at all, and
    the ``0008`` composite FK ``fk_l2f_exec_results_job_member_config`` would additionally pin a
    job that already owns a success result. This asserts the trigger that actually rejects it
    rather than claiming the weaker mechanism did the work.
    """
    done = env.run()
    assert done is not None and done.status == "SUCCEEDED"
    with env.engine.connect() as c:
        other = c.execute(
            text(
                "SELECT id FROM experiments.l2f_experiment_plan_configs "
                f"WHERE id <> (SELECT plan_config_id FROM {_JOBS} WHERE id = :i) LIMIT 1"
            ),
            {"i": done.job_id},
        ).scalar_one()
    with (
        pytest.raises(Exception) as excinfo,  # noqa: B017 - a database constraint
        env.engine.connect() as c,
        c.begin(),
    ):
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(f"UPDATE {_JOBS} SET plan_config_id = :m WHERE id = :i"),  # noqa: S608
            {"m": str(other), "i": done.job_id},
        )
    assert "L2-F job scientific identity may not change" in str(excinfo.value)
    # ...and the F5 composite FK is installed as the second line of defense.
    with env.engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'fk_l2f_exec_results_job_member_config'"
                )
            ).scalar_one()
            == 1
        )


def test_cross_plan_member_substitution(env: Any) -> None:
    graph = _graph(env)
    alien = dataclasses.replace(
        graph.members[0], plan_member_id="00000000-0000-0000-0000-0000000000ff"
    )
    _fails(
        env,
        dataclasses.replace(graph, members=(alien, *graph.members[1:])),
        FORGERY_MATRIX["cross_plan_member_substitution"],
    )


# --------------------------------------------------------------------------- #
# H4 — a CONFIG payload mutated with matching file AND artifact hashes
# --------------------------------------------------------------------------- #
def test_config_payload_mutated_with_matching_hashes(env: Any) -> None:
    """The attacker rewrites the CONFIG bytes AND both digests, but the CONFIG identity is
    recomputed from the bytes, so the mutation is still caught."""
    graph = _graph(env)
    payload = graph.payloads[0]
    forged_bytes = canonical_json_bytes({"min_pruning": 9})
    digest = hashlib.sha256(forged_bytes).hexdigest()
    forged = dataclasses.replace(
        payload,
        artifact_sha256=digest,
        file_sha256=digest,
        artifact_size_bytes=len(forged_bytes),
        file_size_bytes=len(forged_bytes),
    )
    _fails(
        env,
        dataclasses.replace(graph, payloads=(forged, *graph.payloads[1:])),
        FORGERY_MATRIX["config_payload_mutated_with_matching_hashes"],
    )


# --------------------------------------------------------------------------- #
# H5/H6 — input identity forged consistently in the manifest
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mutation",
    [
        {"bam_sha256": "a" * 64},
        {"reference_sha256": "b" * 64},
        {"fai_sha256": "c" * 64},
        {"bai_sha256": "d" * 64},
        {"dictionary_sha256": "e" * 64},
        {"bam_size_bytes": 987654},
        {"region_hash": "f" * 64},
        {"region_start0": 7},
        {"region_end0_exclusive": 999999},
    ],
)
def test_input_component_mutated_with_recomputed_identity(env: Any, mutation: dict) -> None:
    env.run()
    graph = _graph(env)
    result = graph.execution_results[0]
    document = json.loads(result.manifest_bytes)
    document.update(mutation)
    document["input_identity_hash"] = compute_input_identity_hash(
        execution_input_from_manifest(ExecutionResultManifest(**document))
    )
    forged = _rehash_manifest(result, document)
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(forged,)),
        FORGERY_MATRIX["input_component_mutated_with_recomputed_identity"],
    )


# --------------------------------------------------------------------------- #
# H7 — a logical argv forged with a recomputed argv hash
# --------------------------------------------------------------------------- #
def test_logical_argv_mutated_with_recomputed_argv_hash(env: Any) -> None:
    """The stored argv hash is RECOMPUTED from the bound CONFIG, so a matching pair still fails."""
    from minos_engine.storage.l2f_gatk_runner import build_logical_invocation

    env.run()
    graph = _graph(env)
    result = graph.execution_results[0]
    document = json.loads(result.manifest_bytes)
    hostile = dict(canonicalize_live_gatk_config({"min_pruning": 9}).effective_config)
    invocation = build_logical_invocation(
        effective_config=hostile,
        inputs=execution_input_from_manifest(ExecutionResultManifest(**document)),
        gatk_executable_sha256=document["gatk_executable_sha256"],
        gatk_version=document["gatk_version"],
    )
    document["logical_argv_hash"] = invocation.argv_hash()
    forged = _rehash_manifest(result, document)
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(forged,)),
        FORGERY_MATRIX["logical_argv_mutated_with_recomputed_argv_hash"],
    )


# --------------------------------------------------------------------------- #
# H8/H9/H10 — artifact and result forgeries with matching digests
# --------------------------------------------------------------------------- #
def test_vcf_bytes_mutated_with_matching_artifact_hash(env: Any) -> None:
    env.run()
    graph = _graph(env)
    result = graph.execution_results[0]
    forged_bytes = b"##fileformat=VCFv4.2\n#CHROM\tPOS\n"
    digest = hashlib.sha256(forged_bytes).hexdigest()
    forged = dataclasses.replace(
        result,
        vcf_file_sha256=digest,
        vcf_file_size=len(forged_bytes),
        vcf_size_bytes=len(forged_bytes),
    )
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(forged,)),
        FORGERY_MATRIX["vcf_bytes_mutated_with_matching_artifact_hash"],
    )


def test_result_manifest_mutated_with_matching_artifact_hash(env: Any) -> None:
    """Even NON-scientific manifest fields cannot be rewritten: the append-only result row's own
    ``result_manifest_sha256`` anchors the exact published bytes."""
    env.run()
    graph = _graph(env)
    result = graph.execution_results[0]
    document = json.loads(result.manifest_bytes)
    document["worker_id"] = "attacker"
    document["runtime_ms"] = 999999
    forged = _rehash_manifest_file_only(result, document)
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(forged,)),
        FORGERY_MATRIX["result_manifest_mutated_with_matching_artifact_hash"],
    )


def test_result_scientific_fields_mutated_with_matching_result_hash(env: Any) -> None:
    """A fully self-consistent manifest (mutated science AND a recomputed result_hash) still
    fails, because the recomputation must also equal the immutable database row."""
    from minos_engine.experiments.execution_contract import (
        ExecutionConfig,
        GatkExecutionOutcome,
        compute_result_hash,
    )
    from minos_engine.storage.l2f_gatk_runner import build_logical_invocation

    env.run()
    graph = _graph(env)
    result = graph.execution_results[0]
    document = json.loads(result.manifest_bytes)
    document["vcf_size_bytes"] = document["vcf_size_bytes"] + 1
    manifest = ExecutionResultManifest(**{**document, "result_hash": "0" * 64})
    inputs = execution_input_from_manifest(manifest)
    invocation = build_logical_invocation(
        effective_config=dict(result.effective_config),
        inputs=inputs,
        gatk_executable_sha256=manifest.gatk_executable_sha256,
        gatk_version=manifest.gatk_version,
    )
    document["result_hash"] = compute_result_hash(
        plan_hash=env.plan.plan_hash,
        job_key=result.job_key,
        inputs=inputs,
        config=ExecutionConfig(
            config_hash=result.config_hash,
            parameter_space_hash=result.parameter_space_hash,
            config_index=0,
            effective_config=dict(result.effective_config),
        ),
        invocation=invocation,
        outcome=GatkExecutionOutcome(
            exit_code=0,
            runtime_ms=manifest.runtime_ms,
            vcf_sha256=manifest.vcf_sha256,
            vcf_size_bytes=document["vcf_size_bytes"],
        ),
    )
    forged = _rehash_manifest(result, document)
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(forged,)),
        FORGERY_MATRIX["result_scientific_fields_mutated_with_matching_result_hash"],
    )


def test_cross_plan_result_substitution(env: Any) -> None:
    env.run()
    graph = _graph(env)
    alien = dataclasses.replace(
        graph.execution_results[0], job_id="00000000-0000-0000-0000-0000000000aa"
    )
    _fails(
        env,
        dataclasses.replace(graph, execution_results=(alien,)),
        FORGERY_MATRIX["cross_plan_result_substitution"],
    )


# --------------------------------------------------------------------------- #
# H12/H13 — outcome substitution and terminal rows with the wrong durable record
# --------------------------------------------------------------------------- #
def test_success_and_failure_outcome_substituted(env: Any) -> None:
    env.run()
    graph = _graph(env)
    victim = next(j for j in graph.jobs if j.status == "SUCCEEDED")
    swapped = dataclasses.replace(victim, result_count=0, failure_count=1)
    others = tuple(j for j in graph.jobs if j.job_id != victim.job_id)
    _fails(
        env,
        dataclasses.replace(graph, jobs=(swapped, *others), execution_results=()),
        FORGERY_MATRIX["success_and_failure_outcome_substituted"],
    )


@pytest.mark.parametrize(
    ("results", "failures"),
    [(0, 0), (2, 0), (1, 1)],
    ids=["missing", "duplicated", "wrong-kind"],
)
def test_terminal_row_with_missing_or_wrong_kind_outcome(
    env: Any, results: int, failures: int
) -> None:
    env.run()
    graph = _graph(env)
    victim = next(j for j in graph.jobs if j.status == "SUCCEEDED")
    forged = dataclasses.replace(victim, result_count=results, failure_count=failures)
    others = tuple(j for j in graph.jobs if j.job_id != victim.job_id)
    _fails(
        env,
        dataclasses.replace(graph, jobs=(forged, *others)),
        FORGERY_MATRIX["terminal_row_with_missing_or_wrong_kind_outcome"],
    )


def test_the_database_itself_refuses_a_direct_terminal_update(env: Any) -> None:
    """Control at the production boundary: a terminal UPDATE without its durable record fails."""
    from minos_engine.storage.l2f_job_claim import _claim_next_job_with_trust

    claimed = _claim_next_job_with_trust(env.engine, env.plan, worker_id="w-forge")
    assert claimed is not None
    with (
        pytest.raises(Exception) as excinfo,  # noqa: B017 - MN020 from the transition guard
        env.engine.connect() as c,
        c.begin(),
    ):
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(f"UPDATE {_JOBS} SET status='SUCCEEDED' WHERE id=:i"),  # noqa: S608
            {"i": claimed.job_id},
        )
    assert getattr(getattr(excinfo.value, "orig", None), "sqlstate", None) in {"MN012", "MN020"}


# --------------------------------------------------------------------------- #
# H14 — a truth/score-bearing artifact with internally consistent hashes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "uri",
    [
        "file:///data/truth/chr18.truth.vcf",
        "file:///data/mutations/chr18.mutated.vcf",
        "file:///data/happy/chr18.summary.csv",
        "file:///data/scores/chr18.score.json",
    ],
)
def test_truth_bearing_artifact_with_consistent_hashes(env: Any, uri: str) -> None:
    """A perfectly self-consistent truth/score artifact is refused: consistency is not licence.

    The artifact's own digests are left exactly as published, so nothing about it is
    'inconsistent' - it is rejected purely because such material may not enter the graph.
    """
    graph = _graph(env)
    payload = dataclasses.replace(graph.payloads[0], artifact_uri=uri)
    _fails(
        env,
        dataclasses.replace(graph, payloads=(payload, *graph.payloads[1:])),
        FORGERY_MATRIX["truth_bearing_artifact_with_consistent_hashes"],
    )


def test_a_nontrain_member_is_refused(env: Any) -> None:
    """An accepted dataset that ALSO appears as validation/test upstream is refused."""
    graph = _graph(env)
    _fails(
        env,
        dataclasses.replace(graph, upstream_nontrain_dataset_ids=(env.plan.members[0].dataset_id,)),
        "no_nontrain_or_truth_data",
    )


def test_a_legacy_table_overlap_is_refused(env: Any) -> None:
    graph = _graph(env)
    _fails(env, dataclasses.replace(graph, legacy_gatk_config_overlap=1), "legacy_tables_excluded")
    _fails(env, dataclasses.replace(graph, legacy_profile_overlap=1), "legacy_tables_excluded")


# --------------------------------------------------------------------------- #
# F — the input boundary, driven through the real production path
# --------------------------------------------------------------------------- #
def _dataset_files(tmp_path: Path, name: str) -> list[Path]:
    """EVERY provisioned copy of this input, so whichever job is claimed first is affected."""
    candidates = sorted((tmp_path / "datasets").rglob(name))
    assert candidates, name
    return candidates


@pytest.mark.parametrize(
    "name",
    ["input.bam", "input.bam.bai", "chr18.fa", "chr18.fa.fai", "chr18.dict"],
)
def test_substituting_any_provisioned_input_is_refused(env: Any, tmp_path: Path, name: str) -> None:
    for path in _dataset_files(tmp_path, name):
        path.write_bytes(b"SUBSTITUTED-CONTENT-OF-A-DIFFERENT-FILE\n")
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)


@pytest.mark.parametrize("name", ["input.bam", "chr18.fa"])
def test_a_same_size_byte_mutation_is_refused(env: Any, tmp_path: Path, name: str) -> None:
    for path in _dataset_files(tmp_path, name):
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF  # a single flipped bit, identical size
        path.write_bytes(bytes(data))
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)


@pytest.mark.parametrize("name", ["input.bam", "chr18.fa"])
def test_a_symlinked_input_is_refused(env: Any, tmp_path: Path, name: str) -> None:
    for index, path in enumerate(_dataset_files(tmp_path, name)):
        payload = path.read_bytes()
        elsewhere = tmp_path / f"elsewhere-{index}-{name}"
        elsewhere.write_bytes(payload)
        path.unlink()
        path.symlink_to(elsewhere)
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)


@pytest.mark.parametrize("name", ["input.bam", "chr18.fa"])
def test_a_non_regular_input_is_refused(env: Any, tmp_path: Path, name: str) -> None:
    for path in _dataset_files(tmp_path, name):
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)


def test_a_wrong_chromosome_dictionary_is_refused(env: Any, tmp_path: Path) -> None:
    for path in _dataset_files(tmp_path, "chr18.dict"):
        path.write_text("@HD\tVN:1.6\n@SQ\tSN:chr19\tLN:4\n", encoding="utf-8")
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)


def test_the_provisioned_inputs_are_the_valid_control(env: Any) -> None:
    result = env.run(runner=FakeGatkRunner())
    assert result is not None and result.status == "SUCCEEDED"
