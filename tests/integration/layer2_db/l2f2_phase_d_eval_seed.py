"""Seed TWO validation campaigns that differ only in plan identity.

The point of the wrong-plan negative is that the impostor is otherwise perfect: the same ten
frozen members, the same four frozen configurations, the same parameter space, the same
partition. Only ``plan_hash`` separates it from the campaign whose finalists were actually
selected. A seeder that made the impostor sloppy in some other way would let the test pass for
the wrong reason.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from minos_engine.baseline.validation_members import build_validation_schedule
from minos_engine.storage.l2f_execution_contract import (
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)

_NS = uuid.UUID("0000000f-2f2f-2f2f-2f2f-0000000000ff")
_IMPOSTOR_PLAN_HASH = hashlib.sha256(b"impostor-validation-campaign").hexdigest()

# Synthetic call/truth content. Shaped so the SYNTHETIC pinned scorer can parse it; it carries no
# real variant information and is never a substitute for the frozen validation truth.
_CALLED_VCF_TEMPLATE = (
    "##fileformat=VCFv4.2\n"
    "##source=minos-scratch-{label}\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\n"
)

_TRUTH_LINES = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:SNP:ti:het\tTP:SNP:ti:het\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\tBD:BVT:BI:BLT\tTP:INDEL:.:het\tTP:INDEL:.:het\n"
)

_MUTATION_LINES = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr18\t1000\t.\tA\tG\t.\tPASS\t.\n"
    "chr18\t2000\t.\tAT\tA\t.\tPASS\t.\n"
)


def _gz(value: str) -> bytes:
    """Genuinely gzipped, with a fixed mtime so the bytes — and the identity — are stable."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(value.encode("utf-8"))
    return buffer.getvalue()


def provision_validation_truth(engine: Any, root: Path) -> int:
    """Write synthetic truth for every VALIDATION round, then register it through the ACCEPTED
    validation registrar. No real truth file is read, written or registered here."""
    from sqlalchemy import text as _text

    from minos_engine.evaluation.truth_registration import register_validation_truth_identities

    root.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        rounds = [
            str(r)
            for r in conn.execute(
                _text("SELECT round_id FROM evaluation.l2f_validation_truth_registration_targets")
            ).scalars()
        ]
    for round_id in rounds:
        directory = root / f"round_{round_id}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "truth.vcf.gz").write_bytes(_gz(_TRUTH_LINES))
        (directory / "truth.vcf.gz.tbi").write_bytes(b"\x00tbi-truth")
        (directory / "mutations.vcf.gz").write_bytes(_gz(_MUTATION_LINES))
        (directory / "mutations.vcf.gz.tbi").write_bytes(b"\x00tbi-mutations")
    register_validation_truth_identities(engine, dataset_root=root)
    return len(rounds)


def U(label: str) -> str:
    return str(uuid.uuid5(_NS, label))


def H(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _insert(conn: Any, schema: str, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c == "profile_document" else f":{c}" for c in row)
    conn.execute(text(f"INSERT INTO {schema}.{table} ({cols}) VALUES ({vals})"), row)  # noqa: S608


def provision_reference_root(root: Path) -> dict[str, str]:
    """Write the pinned validator's own reference layout and return each FASTA's REAL digest.

    The ledger then records what is actually on disk, so the orchestrator's genome-binding check
    is a real comparison rather than two copies of the same invented constant.
    """
    digests: dict[str, str] = {}
    for chromosome in sorted({m.chromosome for m in build_validation_schedule().members}):
        directory = root / chromosome
        directory.mkdir(parents=True, exist_ok=True)
        fasta = directory / f"{chromosome}.fa"
        payload = f">{chromosome}\n{'ACGT' * 16}\n".encode()
        fasta.write_bytes(payload)
        (directory / f"{chromosome}.sdf").mkdir(exist_ok=True)
        digests[chromosome] = hashlib.sha256(payload).hexdigest()
    return digests


def seed_two_validation_campaigns(
    engine: Any, authority: Any, tmp_path: Path, *, decide_one_job: bool = True
) -> dict[str, Any]:
    """Build the genuine Phase-D campaign and a plan-identical impostor beside it."""
    members = authority.schedule.members
    configs = list(authority.ordered_config_hashes)
    space = authority.parameter_space_hash
    schedule = {m.dataset_id: m for m in build_validation_schedule().members}
    assert len(schedule) == 10
    reference = provision_reference_root(tmp_path / "reference")

    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        art = U("art:generic")
        _insert(
            conn,
            "catalog",
            "artifacts",
            {
                "id": art,
                "uri": "mem://gen",
                "sha256": H("gen"),
                "media_type": "application/octet-stream",
            },
        )
        split = U("split")
        _insert(
            conn,
            "catalog",
            "split_snapshots",
            {
                "id": split,
                "epoch": 1,
                "salt": "s",
                "split_policy_version": "v2",
                "policy_hash": H("p"),
                "manifest_hash": H("m"),
                "registry_snapshot_hash": H("r"),
                "ancestor_v1_dataset_registry_hash": H("a"),
                "parent_registry_snapshot_hash": None,
                "parent_manifest_hash": None,
                "parent_snapshot_id": None,
                "parent_epoch": None,
                "transition_count": 0,
                "sample_count": 10,
                "count_train": 0,
                "count_validation": 10,
                "count_test": 0,
            },
        )
        snap = U("snap")
        _insert(
            conn,
            "profiling",
            "profile_snapshots",
            {
                "id": snap,
                "epoch": 1,
                "split_snapshot_id": split,
                "split_manifest_hash": H("m"),
                "registry_snapshot_hash": H("r"),
                "member_count": 10,
                "snapshot_hash": H("snapshot"),
            },
        )

        registry: dict[str, str] = {}
        for m in members:
            dsr = U(f"dsr:{m.dataset_id}")
            registry[m.dataset_id] = dsr
            _insert(
                conn,
                "catalog",
                "dataset_registry",
                {
                    "id": dsr,
                    "dataset_id": m.dataset_id,
                    "round_id": m.round_id,
                    "chromosome": m.chromosome,
                    "region_source": "scratch",
                    "region_start0": 0,
                    "region_end0_exclusive": 10,
                    "region_length_bp": 10,
                    "region_coordinate_system": "zero-based-half-open",
                    "region_hash": H(f"reg:{m.dataset_id}"),
                    "bam_sha256": H(f"bam:{m.dataset_id}"),
                    "bai_sha256": H(f"bai:{m.dataset_id}"),
                    "reference_sha256": reference[m.chromosome],
                    "fai_sha256": H(f"fai:{m.chromosome}"),
                    "bam_size_bytes": 10,
                    "parameter_space_hash": H("ps"),
                    "feature_registry_hash": H("fr"),
                    "identity_tuple_hash": m.identity_tuple_hash,
                    "manifest_hash": H("m"),
                    "split_algorithm_version": "v2",
                    "split_salt": "s",
                    "allocation_digest": H(f"alloc:{m.dataset_id}"),
                },
            )
            _insert(
                conn,
                "catalog",
                "split_allocations",
                {
                    "id": U(f"alloc:{dsr}"),
                    "dataset_registry_id": dsr,
                    "partition": "validation",
                    "sort_order": 0,
                    "manifest_hash": H("m"),
                },
            )
            _insert(
                conn,
                "profiling",
                "bam_profiles",
                {
                    "id": U(f"bam:{m.dataset_id}"),
                    "dataset_registry_id": dsr,
                    "profile_id": f"profile-{m.dataset_id}",
                    "bam_sha256": H(f"bam:{m.dataset_id}"),
                    "bai_sha256": H(f"bai:{m.dataset_id}"),
                    "reference_sha256": reference[m.chromosome],
                    "fai_sha256": H(f"fai:{m.chromosome}"),
                    "region_hash": H(f"reg:{m.dataset_id}"),
                    "identity_tuple_hash": m.identity_tuple_hash,
                    "m5_status": "ABSENT",
                    "integrity_degraded": True,
                    "attestation_hash": H(f"att:{m.dataset_id}"),
                    "registry_snapshot_hash": H("r"),
                    "profile_status": "COMPLETE",
                    "profiler_version": "layer1-profiler-v1",
                    "profiler_config_hash": H("pc"),
                    "windows_row_count": 10,
                    "feature_values_hash": H(f"fvh:{m.dataset_id}"),
                    "l1_feature_values_hash": H(f"l1:{m.dataset_id}"),
                    "eligible_value_count": 10,
                    "profile_document": json.dumps({"d": m.dataset_id}),
                    "profile_sha256": H(f"ps:{m.dataset_id}"),
                    "profile_manifest_sha256": H(f"pm:{m.dataset_id}"),
                    "windows_sha256": H(f"w:{m.dataset_id}"),
                    "profile_artifact_id": art,
                    "profile_manifest_artifact_id": art,
                    "windows_artifact_id": art,
                    "ingestion_key": H(f"ing:{m.dataset_id}"),
                    "content_hash": H(f"content:{m.dataset_id}"),
                },
            )
            _insert(
                conn,
                "profiling",
                "profile_snapshot_members",
                {
                    "id": U(f"psm:{dsr}"),
                    "profile_snapshot_id": snap,
                    "bam_profile_id": U(f"bam:{m.dataset_id}"),
                    "dataset_registry_id": dsr,
                    "partition": "validation",
                    "feature_values_hash": H(f"fvh:{m.dataset_id}"),
                },
            )

        payloads: dict[str, str] = {}
        for cfg in configs:
            aid = conn.execute(
                text(
                    "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
                    "VALUES (:u, :s, 'application/vnd.minos.l2f-config+json', 1, 'seed') RETURNING id"
                ),
                {"u": f"file:///seed/{cfg}.json", "s": cfg},
            ).scalar_one()
            payloads[cfg] = str(
                conn.execute(
                    text(
                        "INSERT INTO experiments.l2f_config_payloads "
                        "  (config_hash, parameter_space_hash, schema_version, media_type, artifact_id) "
                        "VALUES (:h, :p, 'l2f-config-payload-v1', "
                        "        'application/vnd.minos.l2f-config+json', :a) RETURNING id"
                    ),
                    {"h": cfg, "p": space, "a": aid},
                ).scalar_one()
            )

        result_ids: dict[str, str] = {}
        for label, plan_hash in (
            ("genuine", authority.plan_hash),
            ("impostor", _IMPOSTOR_PLAN_HASH),
        ):
            plan_id = conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_plans ("
                    "  profile_snapshot_id, partition, snapshot_hash, split_manifest_hash, "
                    "  registry_snapshot_hash, gatk_registry_hash, parameter_space_hash, "
                    "  experiment_parameter_policy_hash, candidate_set_hash, train_member_count, "
                    "  candidate_count, logical_job_count, plan_hash) "
                    "SELECT ps.id, 'validation', ps.snapshot_hash, ps.split_manifest_hash, "
                    "       ps.registry_snapshot_hash, :g, :p, :e, :c, 10, 4, 40, :h "
                    "  FROM profiling.profile_snapshots ps LIMIT 1 RETURNING id"
                ),
                {
                    "g": H("gatk"),
                    "p": space,
                    "e": H("policy"),
                    "c": authority.phase_c_candidate_set_hash,
                    "h": plan_hash,
                },
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_plan_members ("
                    "  plan_id, profile_snapshot_id, feature_matrix_id, profile_snapshot_member_id, "
                    "  feature_matrix_member_id, bam_profile_id, dataset_registry_id, partition, "
                    "  feature_values_hash, member_index, source_matrix_member_index) "
                    "SELECT :p, psm.profile_snapshot_id, NULL, psm.id, NULL, psm.bam_profile_id, "
                    "       psm.dataset_registry_id, 'validation', psm.feature_values_hash, "
                    "       ord.position1 - 1, ord.position1 - 1 "
                    "  FROM profiling.profile_snapshot_members psm "
                    "  JOIN catalog.dataset_registry d ON d.id = psm.dataset_registry_id "
                    "  JOIN unnest(CAST(:order AS text[])) "
                    "       WITH ORDINALITY AS ord(dataset_id, position1) "
                    "    ON ord.dataset_id = d.dataset_id"
                ),
                {"p": plan_id, "order": [m.dataset_id for m in members]},
            )
            for index, cfg in enumerate(configs):
                conn.execute(
                    text(
                        "INSERT INTO experiments.l2f_experiment_plan_configs "
                        "  (plan_id, config_payload_id, config_hash, parameter_space_hash, "
                        "   config_index) VALUES (:pl, :pid, :h, :p, :i)"
                    ),
                    {"pl": plan_id, "pid": payloads[cfg], "h": cfg, "p": space, "i": index},
                )
            if decide_one_job:
                result_ids[label] = _seed_one_success(
                    conn, plan_id, plan_hash, label, art, tmp_path
                )

    return {
        "genuine_execution_result_id": result_ids.get("genuine"),
        "impostor_execution_result_id": result_ids.get("impostor"),
        "impostor_plan_hash": _IMPOSTOR_PLAN_HASH,
        "ordered_config_hashes": configs,
        "parameter_space_hash": space,
    }


def _seed_one_success(
    conn: Any, plan_id: str, plan_hash: str, label: str, art: str, root: Path
) -> str:
    """One SUCCEEDED job + execution result on the given plan, member 0 / config 0.

    The VCF is written to disk and the ledger records ITS sha256, so the orchestrator's
    digest re-check is a real check rather than an arranged one.
    """
    vcf_dir = root / "result_artifacts"
    vcf_dir.mkdir(parents=True, exist_ok=True)
    vcf_path = vcf_dir / f"{label}.vcf"
    # Two campaigns must not emit byte-identical calls: catalog.artifacts is content-addressed,
    # so identical bytes would collapse into one artifact and hide which campaign produced it.
    called = _CALLED_VCF_TEMPLATE.format(label=label).encode("utf-8")
    vcf_path.write_bytes(called)
    vcf_sha = hashlib.sha256(called).hexdigest()
    manifest_path = vcf_dir / f"{label}.json"
    manifest_bytes = json.dumps({"label": label}, sort_keys=True).encode()
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    member_id, config_id = conn.execute(
        text(
            "SELECT (SELECT id FROM experiments.l2f_experiment_plan_members "
            "         WHERE plan_id = :p AND member_index = 0), "
            "       (SELECT id FROM experiments.l2f_experiment_plan_configs "
            "         WHERE plan_id = :p AND config_index = 0)"
        ),
        {"p": plan_id},
    ).one()
    job_key = H(f"jobkey:{label}")
    job_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f_experiment_jobs "
            "  (plan_id, plan_member_id, plan_config_id, job_key, status) "
            "VALUES (:p, :m, :c, :k, 'SUCCEEDED') RETURNING id"
        ),
        {"p": plan_id, "m": member_id, "c": config_id, "k": job_key},
    ).scalar_one()
    vcf = conn.execute(
        text(
            "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
            "VALUES (:u, :s, :vm, :sz, 'l2f:gatk-vcf') RETURNING id"
        ),
        {"u": vcf_path.as_uri(), "s": vcf_sha, "vm": L2F_VCF_MEDIA_TYPE, "sz": len(called)},
    ).scalar_one()
    man = conn.execute(
        text(
            "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
            "VALUES (:u, :s, :mm, :sz, 'l2f:execution-result-json') RETURNING id"
        ),
        {
            "u": manifest_path.as_uri(),
            "s": manifest_sha,
            "mm": L2F_RESULT_MANIFEST_MEDIA_TYPE,
            "sz": len(manifest_bytes),
        },
    ).scalar_one()
    cfg_hash = conn.execute(
        text("SELECT config_hash FROM experiments.l2f_experiment_plan_configs WHERE id = :c"),
        {"c": config_id},
    ).scalar_one()
    space = conn.execute(
        text("SELECT parameter_space_hash FROM experiments.l2f_experiment_plans WHERE id = :p"),
        {"p": plan_id},
    ).scalar_one()
    return str(
        conn.execute(
            text(
                "INSERT INTO experiments.l2f_execution_results ("
                "  plan_id, job_id, job_key, plan_member_id, plan_config_id, config_hash, "
                "  parameter_space_hash, input_identity_hash, logical_argv_hash, "
                "  gatk_executable_sha256, gatk_version, vcf_artifact_id, vcf_sha256, vcf_media_type, "
                "  result_manifest_artifact_id, result_manifest_sha256, result_manifest_media_type, "
                "  result_hash, runtime_ms, execution_environment_hash) "
                "VALUES (:p, :j, :k, :m, :c, :ch, :ps, :iid, :argv, :gx, '4.5.0.0', :va, :vs, :vmt, "
                "        :ma, :ms, :mmt, :rh, 1000, :env) RETURNING id"
            ),
            {
                "p": plan_id,
                "j": job_id,
                "k": job_key,
                "m": member_id,
                "c": config_id,
                "ch": cfg_hash,
                "ps": space,
                "iid": H(f"input:{label}"),
                "argv": H(f"argv:{label}"),
                "gx": H("gatk-exe"),
                "va": vcf,
                "vs": vcf_sha,
                "ma": man,
                "ms": manifest_sha,
                "rh": H(f"result:{label}"),
                "vmt": L2F_VCF_MEDIA_TYPE,
                "mmt": L2F_RESULT_MANIFEST_MEDIA_TYPE,
                "env": "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3",
            },
        ).scalar_one()
    )
