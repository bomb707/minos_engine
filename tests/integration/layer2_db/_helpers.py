"""Raw-SQL insert helpers for L2-B integration tests (return generated UUIDs)."""

from __future__ import annotations

from sqlalchemy import Connection, text

H = "a" * 64  # a canonical lowercase sha256


def _scalar(conn: Connection, sql: str, **params: object) -> str:
    return str(conn.execute(text(sql), params).scalar())


def insert_artifact(conn: Connection, *, uri: str = "s3://b/x", sha256: str = H) -> str:
    return _scalar(
        conn,
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES (:uri, :sha) RETURNING id",
        uri=uri,
        sha=sha256,
    )


def insert_config(conn: Connection, *, config_hash: str = "b" * 64, ps_hash: str = "c" * 64) -> str:
    return _scalar(
        conn,
        "INSERT INTO catalog.gatk_configs (config_hash, parameter_space_hash) "
        "VALUES (:ch, :ps) RETURNING id",
        ch=config_hash,
        ps=ps_hash,
    )


def insert_dataset(conn: Connection, *, dataset_id: str = "round_0001") -> str:
    return _scalar(
        conn,
        "INSERT INTO catalog.datasets (dataset_id) VALUES (:d) RETURNING id",
        d=dataset_id,
    )


def insert_profile(conn: Connection, *, profile_id: str = "p1", tuplechar: str = "d") -> str:
    t = tuplechar * 64
    return _scalar(
        conn,
        "INSERT INTO profiling.profiles "
        "(profile_id, bam_sha256, bai_sha256, reference_sha256, fai_sha256, region_hash, "
        " profile_manifest_hash, fingerprint_hash, identity_tuple_hash) "
        "VALUES (:pid, :t, :t2, :t3, :t4, :t5, :m, :f, :it) RETURNING id",
        pid=profile_id,
        t=t,
        t2="e" * 64,
        t3="f" * 64,
        t4="0" * 64,
        t5="1" * 64,
        m="2" * 64,
        f="3" * 64,
        it="4" * 64,
    )


def insert_job(
    conn: Connection, profile_uuid: str, config_uuid: str, *, job_key: str = "job1"
) -> str:
    return _scalar(
        conn,
        "INSERT INTO experiments.jobs (job_key, profile_id, config_id) "
        "VALUES (:k, :p, :c) RETURNING id",
        k=job_key,
        p=profile_uuid,
        c=config_uuid,
    )


def insert_result(conn: Connection, job_uuid: str, *, result_hash: str = "5" * 64) -> str:
    return _scalar(
        conn,
        "INSERT INTO experiments.results (job_id, result_hash) VALUES (:j, :h) RETURNING id",
        j=job_uuid,
        h=result_hash,
    )


def insert_evaluation(conn: Connection, result_uuid: str, *, ev_hash: str = "6" * 64) -> str:
    return _scalar(
        conn,
        "INSERT INTO evaluation.evaluations (experiment_result_id, evaluation_hash) "
        "VALUES (:r, :h) RETURNING id",
        r=result_uuid,
        h=ev_hash,
    )


def insert_model_bundle(conn: Connection, artifact_uuid: str, *, bundle_key: str = "m1") -> str:
    return _scalar(
        conn,
        "INSERT INTO models.model_bundles (bundle_key, artifact_id) VALUES (:k, :a) RETURNING id",
        k=bundle_key,
        a=artifact_uuid,
    )


def insert_decision(
    conn: Connection, *, round_id: str = "round_0001", decision_hash: str = "7" * 64
) -> str:
    return _scalar(
        conn,
        "INSERT INTO runtime.decisions (round_id, decision_hash, decision_manifest_hash) "
        "VALUES (:r, :h, :m) RETURNING id",
        r=round_id,
        h=decision_hash,
        m="8" * 64,
    )


def insert_audit(conn: Connection, *, actor: str = "minos_live", action: str = "decide") -> str:
    return _scalar(
        conn,
        "INSERT INTO audit.events (actor_role, action, payload_hash) "
        "VALUES (:a, :act, :h) RETURNING id",
        a=actor,
        act=action,
        h="9" * 64,
    )
