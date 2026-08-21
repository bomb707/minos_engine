"""F3-C1 durable plan persistence — real-PostgreSQL behavioral tests (scratch only).

Covers the accepted-graph creation, idempotent replay, concurrency, typed conflicts, upstream
resolution, artifact publication contract, transaction/commit-ambiguity handling, exact-
connection identity + revision guards, and non-75 synthetic derivation. Uses scratch databases
(the accepted-path ones are named ``minos_engine_db`` on the ephemeral server so the operational
identity check passes; the real operational store is never touched).
"""

from __future__ import annotations

import contextlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.accepted_plan import (
    _build_plan_from_verified_inputs,
    build_accepted_experiment_plan,
)
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.layer2.features.extraction import FrozenSnapshot, SnapshotMember
from minos_engine.layer2.features.feature_view import (
    FeatureViewMember,
    build_feature_view_manifest,
)
from minos_engine.storage import l2f_plan_store as PS
from minos_engine.storage.database import (
    OperationalDatabaseIdentityError,
    normalize_database_url,
)
from minos_engine.storage.l2f_config_publisher import ConfigPayloadPublisher
from minos_engine.storage.l2f_plan_store import (
    AmbiguousPlanCommitError,
    ArtifactMetadataConflictError,
    ImmutableMetadataConflictError,
    PlanRevisionError,
    UpstreamIdentityError,
    _persist_experiment_plan_with_trust,
    persist_accepted_experiment_plan,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_OP_DB = "minos_engine_db"

_ACCEPTED_PLAN = build_accepted_experiment_plan()
_CS = generate_accepted_candidate_set()


def _provisioned_root(tmp_path: Path) -> Path:
    root = tmp_path / "cfgroot"
    root.mkdir()
    os.chmod(root, 0o2750)
    return root


def _publisher(root: Path) -> ConfigPayloadPublisher:
    return ConfigPayloadPublisher(root)


def _engine(url: str) -> Engine:
    return create_engine(normalize_database_url(url))


def _count(engine: Engine, sql: str, **p: Any) -> int:
    with engine.connect() as c:
        return int(c.execute(text(sql), p).scalar_one())


def _graph_counts(engine: Engine) -> dict[str, int]:
    return {
        t: _count(engine, f"SELECT count(*) FROM experiments.{t}")  # noqa: S608
        for t in (
            "l2f_experiment_plans",
            "l2f_experiment_plan_members",
            "l2f_config_payloads",
            "l2f_experiment_plan_configs",
            "l2f_experiment_jobs",
        )
    }


def _artifact_files(root: Path) -> list[Path]:
    return sorted(root.glob("*.json"))


# --------------------------------------------------------------------------- #
# synthetic (non-75) plans built from real FrozenSnapshots
# --------------------------------------------------------------------------- #
def _h(label: str) -> str:
    return sha256_hex(label.encode())


def _synthetic_plan(spec: list[tuple[str, str, str]]):
    members = tuple(
        SnapshotMember(
            dataset_id=ds,
            profile_id=f"profile-{ds}",
            partition=part,  # type: ignore[arg-type]
            content_hash=_h(f"content:{ds}"),
            feature_values_hash=_h(f"fvh:{ds}"),
            profile_sha256=_h(f"sha:{ds}"),
            chromosome=chrom,  # type: ignore[arg-type]
        )
        for ds, part, chrom in spec
    )
    snapshot = FrozenSnapshot(
        epoch=1, split_manifest_hash=_h("split"), registry_snapshot_hash=_h("reg"), members=members
    )
    train = snapshot.members_for("train")
    fv = build_feature_view_manifest(
        epoch=1,
        partition="train",
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        matrix_hash=_h("matrix"),
        artifact_sha256=_h("artifact"),
        row_count=len(train),
        members=tuple(
            FeatureViewMember(
                dataset_id=m.dataset_id,
                member_index=i,
                vector_hash=_h(f"vec:{m.dataset_id}"),
                feature_values_hash=m.feature_values_hash,
            )
            for i, m in enumerate(train)
        ),
        feature_set=None,
    )
    return _build_plan_from_verified_inputs(snapshot, fv, _CS)


_SNAPSHOT_A = [
    ("dsA1", "train", "chr18"),
    ("dsA2", "train", "chr18"),
    ("dsA3", "train", "chr19"),
    ("dsA4", "train", "chr22"),
    ("dsA5", "validation", "chr20"),
    ("dsA6", "validation", "chr21"),
    ("dsA7", "test", "chr18"),
    ("dsA8", "test", "chr19"),
    ("dsA9", "test", "chr20"),
]  # 9 total, 4 train
_SNAPSHOT_B = [
    ("dsB1", "train", "chr22"),
    ("dsB2", "train", "chr22"),
    ("dsB3", "validation", "chr18"),
    ("dsB4", "validation", "chr18"),
    ("dsB5", "validation", "chr19"),
    ("dsB6", "validation", "chr20"),
    ("dsB7", "validation", "chr21"),
    ("dsB8", "validation", "chr21"),
    ("dsB9", "test", "chr18"),
    ("dsB10", "test", "chr19"),
    ("dsB11", "test", "chr20"),
]  # 11 total, 2 train


# --------------------------------------------------------------------------- #
# accepted graph creation + idempotency + legacy/jobs invariants
# --------------------------------------------------------------------------- #
def test_accepted_persistence_creates_exact_graph(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            legacy_before = {
                t: _count(engine, f"SELECT count(*) FROM {t}")  # noqa: S608
                for t in (
                    "profiling.profiles",
                    "experiments.jobs",
                    "experiments.results",
                    "catalog.gatk_configs",
                )
            }
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))

            result = persist_accepted_experiment_plan()
            assert result.plan_created is True and result.replay is False
            assert result.plan_hash == _ACCEPTED_PLAN.plan_hash
            assert result.member_count == _ACCEPTED_PLAN.train_member_count
            assert result.config_count == _ACCEPTED_PLAN.candidate_count
            assert result.payload_count == _ACCEPTED_PLAN.candidate_count
            assert result.artifacts_created == _ACCEPTED_PLAN.candidate_count
            assert result.jobs_count == 0

            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plans"] == 1
            assert counts["l2f_experiment_plan_members"] == _ACCEPTED_PLAN.train_member_count
            assert counts["l2f_config_payloads"] == _ACCEPTED_PLAN.candidate_count
            assert counts["l2f_experiment_plan_configs"] == _ACCEPTED_PLAN.candidate_count
            assert counts["l2f_experiment_jobs"] == 0  # F3-C1 creates NO jobs

            files = _artifact_files(root)
            assert len(files) == _ACCEPTED_PLAN.candidate_count
            assert all(stat.S_IMODE(f.stat().st_mode) == 0o640 for f in files)

            # legacy tables untouched
            for t, n in legacy_before.items():
                assert _count(engine, f"SELECT count(*) FROM {t}") == n  # noqa: S608
        finally:
            engine.dispose()


def test_sequential_replay_is_idempotent(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))

            first = persist_accepted_experiment_plan()
            counts_after_first = _graph_counts(engine)
            files_after_first = {f.name: f.read_bytes() for f in _artifact_files(root)}

            second = persist_accepted_experiment_plan()
            assert first.plan_created is True
            assert second.plan_created is False and second.replay is True
            assert second.artifacts_created == 0
            assert _graph_counts(engine) == counts_after_first  # no duplicate rows
            assert {f.name: f.read_bytes() for f in _artifact_files(root)} == files_after_first
        finally:
            engine.dispose()


def test_two_engines_race_produce_one_graph(pg_base_url: str, tmp_path: Path) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)

            def _run() -> Any:
                eng = _engine(url)
                try:
                    return _persist_experiment_plan_with_trust(
                        eng, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                    )
                finally:
                    eng.dispose()

            with ThreadPoolExecutor(max_workers=2) as pool:
                r1 = pool.submit(_run)
                r2 = pool.submit(_run)
                res = [r1.result(), r2.result()]

            assert all(r.jobs_count == 0 for r in res)
            # exactly one creator; the other is an idempotent replay — one final graph.
            assert {r.plan_created for r in res} == {True, False}
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plans"] == 1
            assert counts["l2f_experiment_plan_members"] == _ACCEPTED_PLAN.train_member_count
            assert counts["l2f_config_payloads"] == _ACCEPTED_PLAN.candidate_count
            assert len(_artifact_files(root)) == _ACCEPTED_PLAN.candidate_count
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# helpers to pre-insert a poisoned/conflicting plan row
# --------------------------------------------------------------------------- #
def _resolve_upstream_ids(engine: Engine, plan: Any) -> dict[str, str]:
    with engine.connect() as c:
        fsid = c.execute(
            text(
                "SELECT id FROM profiling.feature_sets WHERE feature_set_hash=:h AND registry_hash=:r"
            ),
            {"h": plan.feature_set_hash, "r": plan.feature_registry_hash},
        ).scalar_one()
        sid = c.execute(
            text(
                "SELECT id FROM profiling.profile_snapshots WHERE snapshot_hash=:s "
                "AND split_manifest_hash=:sm AND registry_snapshot_hash=:rs"
            ),
            {
                "s": plan.snapshot_hash,
                "sm": plan.split_manifest_hash,
                "rs": plan.registry_snapshot_hash,
            },
        ).scalar_one()
        mid = c.execute(
            text(
                "SELECT id FROM profiling.feature_matrices WHERE profile_snapshot_id=:s "
                "AND partition='train' AND matrix_hash=:m AND feature_set_id=:f"
            ),
            {"s": sid, "m": plan.train_matrix_hash, "f": fsid},
        ).scalar_one()
    return {"feature_set_id": str(fsid), "profile_snapshot_id": str(sid), "matrix_id": str(mid)}


def _insert_poisoned_plan(engine: Engine, plan: Any, ids: dict[str, str], **override: Any) -> None:
    row = {
        "profile_snapshot_id": ids["profile_snapshot_id"],
        "train_feature_matrix_id": ids["matrix_id"],
        "feature_set_id": ids["feature_set_id"],
        "partition": "train",
        "snapshot_hash": plan.snapshot_hash,
        "split_manifest_hash": plan.split_manifest_hash,
        "registry_snapshot_hash": plan.registry_snapshot_hash,
        "train_matrix_hash": plan.train_matrix_hash,
        "train_feature_view_hash": plan.train_feature_view_hash,
        "feature_set_hash": plan.feature_set_hash,
        "feature_registry_hash": plan.feature_registry_hash,
        "gatk_registry_hash": plan.gatk_registry_hash,
        "parameter_space_hash": plan.parameter_space_hash,
        "experiment_parameter_policy_hash": plan.experiment_parameter_policy_hash,
        "candidate_set_hash": plan.candidate_set_hash,
        "train_member_count": plan.train_member_count,
        "candidate_count": plan.candidate_count,
        "logical_job_count": plan.logical_job_count,
        "plan_hash": plan.plan_hash,
    }
    row.update(override)
    cols = list(row)
    with engine.connect() as c, c.begin():
        c.execute(text("SET ROLE minos_admin"))
        c.execute(
            text(
                f"INSERT INTO experiments.l2f_experiment_plans ({', '.join(cols)}) "  # noqa: S608
                f"VALUES ({', '.join(f':{x}' for x in cols)})"
            ),
            row,
        )


def test_conflicting_immutable_metadata_is_typed_and_rolls_back_files(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            ids = _resolve_upstream_ids(engine, _ACCEPTED_PLAN)
            # a pre-existing plan row sharing plan_hash but with a DIFFERENT candidate_set_hash.
            _insert_poisoned_plan(engine, _ACCEPTED_PLAN, ids, candidate_set_hash="0" * 64)

            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))
            with pytest.raises(ImmutableMetadataConflictError):
                persist_accepted_experiment_plan()

            # the pre-existing poisoned plan is unchanged; no members/configs/payloads added;
            # and the pre-commit failure removed every newly created artifact file.
            assert _count(engine, "SELECT count(*) FROM experiments.l2f_experiment_plans") == 1
            assert (
                _count(
                    engine,
                    "SELECT count(*) FROM experiments.l2f_experiment_plans WHERE candidate_set_hash=:h",
                    h="0" * 64,
                )
                == 1
            )
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plan_members"] == 0
            assert counts["l2f_config_payloads"] == 0
            assert counts["l2f_experiment_plan_configs"] == 0
            assert _artifact_files(root) == []
        finally:
            engine.dispose()


def test_missing_upstream_identity_fails_before_publication(
    pg_base_url: str, tmp_path: Path
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)  # NO upstream seeded
        engine = _engine(url)
        try:
            root = _provisioned_root(tmp_path)
            with pytest.raises(UpstreamIdentityError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            assert _artifact_files(root) == []  # nothing published
            assert _graph_counts(engine)["l2f_config_payloads"] == 0
        finally:
            engine.dispose()


def test_existing_artifact_with_wrong_media_type_is_rejected(
    pg_base_url: str, tmp_path: Path
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
                # pre-register a catalog.artifacts row for the first config_hash with a WRONG
                # media_type — the get-or-verify must reject it.
                conn.execute(text("SET ROLE minos_admin"))
                conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance)"
                        " VALUES (:u, :h, :m, :s, :p)"
                    ),
                    {
                        "u": "mem://wrong",
                        "h": _CS.configs[0].config_hash,
                        "m": "application/octet-stream",
                        "s": 1,
                        "p": "wrong",
                    },
                )
            root = _provisioned_root(tmp_path)
            with pytest.raises(ArtifactMetadataConflictError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            assert _graph_counts(engine)["l2f_experiment_plans"] == 0
        finally:
            engine.dispose()


def test_existing_config_payload_with_wrong_param_space_is_rejected(
    pg_base_url: str, tmp_path: Path
) -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.storage.l2f_config_publisher import CONFIG_ARTIFACT_KIND

    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            cfg0 = _CS.configs[0]
            payload = canonical_json_bytes(cfg0.effective_config)
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
                conn.execute(text("SET ROLE minos_admin"))
                # an artifact that MATCHES what the publisher will produce (uri/size/media/
                # provenance), then a config_payload with a WRONG parameter_space_hash -> the
                # get-or-verify passes and the config_payload conflict is the typed failure.
                aid = conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance)"
                        " VALUES (:u,:h,:m,:s,:p) RETURNING id"
                    ),
                    {
                        "u": pub.content_uri(cfg0.config_hash),
                        "h": cfg0.config_hash,
                        "m": "application/vnd.minos.l2f-config+json",
                        "s": len(payload),
                        "p": CONFIG_ARTIFACT_KIND,
                    },
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO experiments.l2f_config_payloads "
                        "(config_hash, parameter_space_hash, schema_version, media_type, artifact_id) "
                        "VALUES (:h,:ps,:sv,:m,:a)"
                    ),
                    {
                        "h": cfg0.config_hash,
                        "ps": "0" * 64,
                        "sv": "l2f-config-payload-v1",
                        "m": "application/vnd.minos.l2f-config+json",
                        "a": aid,
                    },
                )
            with pytest.raises(ImmutableMetadataConflictError):
                _persist_experiment_plan_with_trust(engine, _ACCEPTED_PLAN, _CS, publisher=pub)
        finally:
            engine.dispose()


def test_ambiguous_commit_retains_artifacts(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)

            def _raise_ambiguous(_trans: Any) -> None:
                raise AmbiguousPlanCommitError("simulated commit raise")

            monkeypatch.setattr(PS, "_commit_or_ambiguous", _raise_ambiguous)
            with pytest.raises(AmbiguousPlanCommitError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            # ambiguous commit: immutable artifacts are RETAINED (not removed).
            assert len(_artifact_files(root)) == _ACCEPTED_PLAN.candidate_count
        finally:
            engine.dispose()


def test_post_commit_failure_keeps_rows_and_files(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)

            def _boom() -> None:
                raise RuntimeError("post-commit wrapper failure")

            monkeypatch.setattr(PS, "_post_commit_hook", _boom)
            with pytest.raises(RuntimeError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            # commit already succeeded: rows AND files remain intact.
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plans"] == 1
            assert counts["l2f_experiment_plan_members"] == _ACCEPTED_PLAN.train_member_count
            assert len(_artifact_files(root)) == _ACCEPTED_PLAN.candidate_count
        finally:
            engine.dispose()


def test_wrong_database_identity_rejected_before_any_write(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, "not_the_operational_store") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))
            with pytest.raises(OperationalDatabaseIdentityError):
                persist_accepted_experiment_plan()
            # rejected before any write or file access.
            assert _artifact_files(root) == []
            assert _graph_counts(engine)["l2f_experiment_plans"] == 0
        finally:
            engine.dispose()


def test_revision_0005_rejected_without_upgrade(
    pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _PREV)  # 0005, NOT 0006
        engine = _engine(url)
        try:
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))
            with pytest.raises(PlanRevisionError):
                persist_accepted_experiment_plan()
            # the boundary NEVER upgrades: still at 0005, nothing published.
            assert _count(engine, "SELECT version_num = :h FROM alembic_version", h=_PREV) == 1
            assert _artifact_files(root) == []
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    ("spec", "expected_train"),
    [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)],
)
def test_non75_synthetic_plan_persists_derived_counts(
    pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], expected_train: int
) -> None:
    plan = _synthetic_plan(spec)
    assert plan.train_member_count == expected_train  # non-75, derived from actual membership
    with scratch_database(pg_base_url, "minos_l2f_synth") as url:  # NOT the operational name
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            # the PRIVATE explicit-trust boundary (never the accepted production entry point).
            result = _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(root)
            )
            assert result.member_count == expected_train == plan.train_member_count
            assert result.config_count == len(_CS.configs) == plan.candidate_count
            assert result.jobs_count == 0
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plan_members"] == expected_train
            assert counts["l2f_config_payloads"] == len(_CS.configs)
            assert counts["l2f_experiment_jobs"] == 0
        finally:
            engine.dispose()


def test_private_trust_boundary_not_exported() -> None:
    assert "_persist_experiment_plan_with_trust" not in PS.__all__
    assert "persist_accepted_experiment_plan" in PS.__all__
    with contextlib.suppress(TypeError):
        # the accepted entry point takes no arguments.
        import inspect

        assert list(inspect.signature(persist_accepted_experiment_plan).parameters) == []
