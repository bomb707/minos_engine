"""F3-C1 durable plan persistence — real-PostgreSQL behavioral tests.

Covers accepted-graph creation, idempotent replay, concurrency, complete upstream identity
binding (with an independent negative per identity field), exact immutable get-or-verify across
every column and every unique constraint, exact-connection identity + revision ordering
(sentinel-proven), artifact/payload metadata conflicts, transaction / commit-ambiguity handling,
and non-75 synthetic derivation.

CI database isolation
---------------------
The accepted production entry point requires ``current_database() == minos_engine_db``. In
GitHub Actions the service container's database is ALSO named ``minos_engine_db``, which cannot
be dropped from a connection attached to it (``cannot drop the currently open database``) and,
by contract, must never be dropped / recreated / migrated / written. Every database-backed test
here therefore uses the dedicated ``isolated_pg_base_url`` cluster (a separate bundled
``pgserver`` whose maintenance database is ``postgres``), so a throwaway ``minos_engine_db`` can
be created and dropped without ever touching the CI service database. The real operational store
is never touched (``MINOS_DATABASE_URL`` is monkeypatched only to the isolated scratch URL).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from minos_engine.common.canonical_json import canonical_json_bytes
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
from minos_engine.storage.l2f_config_publisher import (
    CONFIG_ARTIFACT_KIND,
    CONFIG_ARTIFACT_MEDIA_TYPE,
    ConfigPayloadPublisher,
)
from minos_engine.storage.l2f_migration_contract import L2F_CONFIG_PAYLOAD_SCHEMA
from minos_engine.storage.l2f_plan_store import (
    AmbiguousPlanCommitError,
    ArtifactMetadataConflictError,
    ImmutableMetadataConflictError,
    PlanRevisionError,
    PlanVerificationError,
    UpstreamIdentityError,
    _persist_experiment_plan_with_trust,
    persist_accepted_experiment_plan,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import (
    CORRUPTIONS,
    SET_DEFECTS,
    seed_upstream_for_plan,
)
from tests.integration.layer2_db.l2f_seed import H, U, _bam_row, _dataset_row, _insert

_HEAD = "0006_l2f_experiment_plan"
#: repository head — the first revision that stores the two index namespaces separately.
_SOURCE_INDEX_HEAD = "0012_l2f_plan_member_source_idx"
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


@contextlib.contextmanager
def _admin_conn(engine: Engine) -> Iterator[Connection]:
    conn = engine.connect()
    trans = conn.begin()
    try:
        conn.execute(text("SET ROLE minos_admin"))
        yield conn
        trans.commit()
    except BaseException:
        trans.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# synthetic (non-75) plans built from real FrozenSnapshots
# --------------------------------------------------------------------------- #
def _h(label: str) -> str:
    return sha256_hex(label.encode())


def _synthetic_plan(spec: list[tuple[str, str, str]]) -> Any:
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
# one-train-member snapshot used by the per-identity-field negative matrix.
_SNAPSHOT_ONE = [
    ("dsN1", "train", "chr18"),
    ("dsN2", "validation", "chr19"),
    ("dsN3", "test", "chr20"),
]


# --------------------------------------------------------------------------- #
# accepted graph creation + idempotency + legacy/jobs invariants
# --------------------------------------------------------------------------- #
def test_accepted_persistence_creates_exact_graph(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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


def test_two_engines_race_produce_one_graph(isolated_pg_base_url: str, tmp_path: Path) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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


def _poisoned_plan_row(plan: Any, ids: dict[str, str], **override: Any) -> dict[str, Any]:
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
    return row


def _insert_poisoned_plan(engine: Engine, plan: Any, ids: dict[str, str], **override: Any) -> None:
    row = _poisoned_plan_row(plan, ids, **override)
    cols = list(row)
    with _admin_conn(engine) as c:
        c.execute(
            text(
                f"INSERT INTO experiments.l2f_experiment_plans ({', '.join(cols)}) "  # noqa: S608
                f"VALUES ({', '.join(f':{x}' for x in cols)})"
            ),
            row,
        )


def test_conflicting_immutable_metadata_is_typed_and_rolls_back_files(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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


def test_plan_logical_identity_collision_is_typed(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """An alternate unique path: a pre-existing plan with the SAME complete logical identity but
    a DIFFERENT plan_hash collides on ``uq_l2f_plans_logical_identity`` (not ``plan_hash``); the
    store must classify that constraint, re-read, and raise the typed conflict."""
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            ids = _resolve_upstream_ids(engine, _ACCEPTED_PLAN)
            # same 11 logical-identity hashes, different plan_hash (FK-free, hex64).
            _insert_poisoned_plan(engine, _ACCEPTED_PLAN, ids, plan_hash="f" * 64)
            root = _provisioned_root(tmp_path)
            with pytest.raises(ImmutableMetadataConflictError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            assert _count(engine, "SELECT count(*) FROM experiments.l2f_experiment_plans") == 1
            assert _artifact_files(root) == []
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "column",
    ["parameter_space_hash", "experiment_parameter_policy_hash", "gatk_registry_hash"],
)
def test_plan_previously_omitted_immutable_column_conflict(
    isolated_pg_base_url: str, tmp_path: Path, column: str
) -> None:
    """A pre-existing plan sharing ``plan_hash`` but differing on an immutable column that the
    original comparison omitted (parameter-space / policy / gatk-registry hash) must now be a
    typed conflict, not a false idempotent success."""
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            ids = _resolve_upstream_ids(engine, _ACCEPTED_PLAN)
            _insert_poisoned_plan(engine, _ACCEPTED_PLAN, ids, **{column: "1" * 64})
            root = _provisioned_root(tmp_path)
            with pytest.raises(ImmutableMetadataConflictError):
                _persist_experiment_plan_with_trust(
                    engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
                )
            assert _count(engine, "SELECT count(*) FROM experiments.l2f_experiment_plans") == 1
        finally:
            engine.dispose()


def test_plan_config_alternate_unique_paths(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """Both plan-config unique constraints are classified: an identical row is idempotent; a row
    colliding on ``(plan_id, config_index)`` or ``(plan_id, config_payload_id)`` with differing
    immutable metadata is a typed conflict — no raw uniqueness error escapes."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))
            with engine.connect() as c:
                plan_id = c.execute(
                    text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash=:h"),
                    {"h": plan.plan_hash},
                ).scalar_one()
                rows = (
                    c.execute(
                        text(
                            "SELECT config_payload_id, config_hash, parameter_space_hash, config_index "
                            "FROM experiments.l2f_experiment_plan_configs WHERE plan_id=:p "
                            "ORDER BY config_index LIMIT 2"
                        ),
                        {"p": plan_id},
                    )
                    .mappings()
                    .all()
                )
            c0, c1 = dict(rows[0]), dict(rows[1])

            def _row(**over: Any) -> dict[str, Any]:
                base = {"plan_id": str(plan_id), **c0}
                base.update(over)
                return base

            # idempotent: identical row on both keys -> replay, no new row.
            with _admin_conn(engine) as conn:
                _id, created = PS._insert_or_verify(
                    conn,
                    table="l2f_experiment_plan_configs",
                    row=_row(),
                    unique_keys=PS._PLAN_CONFIG_UNIQUE_KEYS,
                )
                assert created is False
            # conflict on (plan_id, config_index): reuse index 0 but bind config 1's payload.
            with _admin_conn(engine) as conn, pytest.raises(ImmutableMetadataConflictError):
                PS._insert_or_verify(
                    conn,
                    table="l2f_experiment_plan_configs",
                    row=_row(
                        config_payload_id=c1["config_payload_id"],
                        config_hash=c1["config_hash"],
                        parameter_space_hash=c1["parameter_space_hash"],
                    ),
                    unique_keys=PS._PLAN_CONFIG_UNIQUE_KEYS,
                )
            # conflict on (plan_id, config_payload_id): reuse index 0's payload at a new index.
            with _admin_conn(engine) as conn, pytest.raises(ImmutableMetadataConflictError):
                PS._insert_or_verify(
                    conn,
                    table="l2f_experiment_plan_configs",
                    row=_row(config_index=99999),
                    unique_keys=PS._PLAN_CONFIG_UNIQUE_KEYS,
                )
            assert (
                _count(
                    engine,
                    "SELECT count(*) FROM experiments.l2f_experiment_plan_configs WHERE plan_id=:p",
                    p=str(plan_id),
                )
                == plan.candidate_count
            )
        finally:
            engine.dispose()


def test_plan_member_duplicate_is_idempotent(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """A fully FK-valid duplicate plan-member collides on all three member unique constraints at
    once (the composite FKs pin dataset / feature-values / matrix-index / bam_profile), so the
    only reachable get-or-verify outcome is idempotent success. The store must classify whichever
    member unique constraint PostgreSQL reports, re-read, match every immutable column, and add
    no row — never surface a raw uniqueness error."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))
            with engine.connect() as c:
                member = (
                    c.execute(
                        text(
                            "SELECT plan_id, profile_snapshot_id, feature_matrix_id, "
                            "profile_snapshot_member_id, feature_matrix_member_id, bam_profile_id, "
                            "dataset_registry_id, partition, feature_values_hash, member_index "
                            "FROM experiments.l2f_experiment_plan_members LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
            before = _count(engine, "SELECT count(*) FROM experiments.l2f_experiment_plan_members")
            with _admin_conn(engine) as conn:
                _id, created = PS._insert_or_verify(
                    conn,
                    table="l2f_experiment_plan_members",
                    row={k: str(v) for k, v in member.items()},
                    unique_keys=PS._MEMBER_UNIQUE_KEYS,
                )
                assert created is False
            after = _count(engine, "SELECT count(*) FROM experiments.l2f_experiment_plan_members")
            assert after == before
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# complete upstream identity binding — one independent negative per field
# --------------------------------------------------------------------------- #
def test_missing_upstream_identity_fails_before_publication(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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


@pytest.mark.parametrize("field", CORRUPTIONS)
def test_upstream_identity_field_negative(
    isolated_pg_base_url: str, tmp_path: Path, field: str
) -> None:
    """Corrupting exactly one upstream identity field of a plan member makes the store fail with a
    typed ``UpstreamIdentityError`` BEFORE any payload publication, leaving zero rows / files."""
    plan = _synthetic_plan(_SNAPSHOT_ONE)
    assert plan.train_member_count == 1
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, corrupt=field)
            root = _provisioned_root(tmp_path)
            with pytest.raises(UpstreamIdentityError):
                _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))
            assert _artifact_files(root) == []
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plans"] == 0
            assert counts["l2f_experiment_plan_members"] == 0
            assert counts["l2f_config_payloads"] == 0
        finally:
            engine.dispose()


def test_valid_upstream_after_seed_fix_persists(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """The (fixed) seed's claimed valid graph contains the exact accepted identities: a
    one-train-member plan with a matching bam_profile / snapshot-member / matrix-member persists
    cleanly (proving the negatives above fail for the right reason, not a broken seed)."""
    plan = _synthetic_plan(_SNAPSHOT_ONE)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            result = _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(root)
            )
            assert result.member_count == 1
            assert result.jobs_count == 0
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# artifact / payload metadata conflicts (typed; NULL/malformed safe)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "case", ["wrong_uri", "wrong_size", "null_size", "wrong_media", "wrong_provenance"]
)
def test_artifact_metadata_conflict_is_typed(
    isolated_pg_base_url: str, tmp_path: Path, case: str
) -> None:
    """Every catalog.artifacts mismatch — including a NULL stored size — raises the typed
    ``ArtifactMetadataConflictError`` (never TypeError/ValueError/raw DB error) and leaves the
    accepted graph unchanged."""
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            cfg0 = _CS.configs[0]
            payload = canonical_json_bytes(cfg0.effective_config)
            # a matching artifact row with exactly one field wrong.
            row = {
                "u": pub.content_uri(cfg0.config_hash),
                "h": cfg0.config_hash,
                "m": CONFIG_ARTIFACT_MEDIA_TYPE,
                "s": len(payload),
                "p": CONFIG_ARTIFACT_KIND,
            }
            if case == "wrong_uri":
                row["u"] = "mem://wrong"
            elif case == "wrong_size":
                row["s"] = len(payload) + 1
            elif case == "null_size":
                row["s"] = None
            elif case == "wrong_media":
                row["m"] = "application/octet-stream"
            elif case == "wrong_provenance":
                row["p"] = "not-l2f"
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
                conn.execute(text("SET ROLE minos_admin"))
                conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance)"
                        " VALUES (:u,:h,:m,:s,:p)"
                    ),
                    row,
                )
            with pytest.raises(ArtifactMetadataConflictError):
                _persist_experiment_plan_with_trust(engine, _ACCEPTED_PLAN, _CS, publisher=pub)
            assert _graph_counts(engine)["l2f_experiment_plans"] == 0
            assert _graph_counts(engine)["l2f_config_payloads"] == 0
        finally:
            engine.dispose()


def test_existing_config_payload_with_wrong_param_space_is_rejected(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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
                # an artifact that MATCHES what the publisher will produce, then a config_payload
                # with a WRONG parameter_space_hash -> the get-or-verify passes and the
                # config_payload conflict is the typed failure.
                aid = conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance)"
                        " VALUES (:u,:h,:m,:s,:p) RETURNING id"
                    ),
                    {
                        "u": pub.content_uri(cfg0.config_hash),
                        "h": cfg0.config_hash,
                        "m": CONFIG_ARTIFACT_MEDIA_TYPE,
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
                        "m": CONFIG_ARTIFACT_MEDIA_TYPE,
                        "a": aid,
                    },
                )
            with pytest.raises(ImmutableMetadataConflictError):
                _persist_experiment_plan_with_trust(engine, _ACCEPTED_PLAN, _CS, publisher=pub)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# transaction / commit-ambiguity handling
# --------------------------------------------------------------------------- #
def test_ambiguous_commit_retains_artifacts(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
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


# --------------------------------------------------------------------------- #
# exact-connection identity + revision ordering (sentinel-proven)
# --------------------------------------------------------------------------- #
def _install_build_sentinels(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"plan": 0, "candidates": 0, "publisher": 0, "resolve": 0, "publish": 0}

    def _wrap(name: str, orig: Any) -> Any:
        def _w(*a: Any, **k: Any) -> Any:
            calls[name] += 1
            return orig(*a, **k)

        return _w

    monkeypatch.setattr(PS, "_build_accepted_plan", _wrap("plan", PS._build_accepted_plan))
    monkeypatch.setattr(
        PS, "_build_accepted_candidate_set", _wrap("candidates", PS._build_accepted_candidate_set)
    )
    monkeypatch.setattr(PS, "_build_publisher", _wrap("publisher", PS._build_publisher))
    monkeypatch.setattr(PS, "_resolve_plan_upstream", _wrap("resolve", PS._resolve_plan_upstream))
    monkeypatch.setattr(
        PS, "_publish_config_payloads", _wrap("publish", PS._publish_config_payloads)
    )
    return calls


def test_wrong_identity_builds_and_touches_nothing(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, "not_the_operational_store") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))
            calls = _install_build_sentinels(monkeypatch)
            with pytest.raises(OperationalDatabaseIdentityError):
                persist_accepted_experiment_plan()
            # zero calls to plan builder, candidate builder, publisher/root access, upstream
            # resolver and publication; nothing written or published.
            assert calls == {"plan": 0, "candidates": 0, "publisher": 0, "resolve": 0, "publish": 0}
            assert _artifact_files(root) == []
            assert _graph_counts(engine)["l2f_experiment_plans"] == 0
        finally:
            engine.dispose()


def test_revision_0005_builds_and_touches_nothing(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _PREV)  # 0005, NOT 0006
        engine = _engine(url)
        try:
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv(PS.ENV_CONFIG_ARTIFACT_ROOT, str(root))
            calls = _install_build_sentinels(monkeypatch)
            with pytest.raises(PlanRevisionError):
                persist_accepted_experiment_plan()
            assert calls == {"plan": 0, "candidates": 0, "publisher": 0, "resolve": 0, "publish": 0}
            # the boundary NEVER upgrades: still at 0005, nothing published.
            assert _count(engine, "SELECT version_num = :h FROM alembic_version", h=_PREV) == 1
            assert _artifact_files(root) == []
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# non-75 synthetic derivation + boundary export invariants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("spec", "expected_train"),
    [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)],
)
def test_non75_synthetic_plan_persists_derived_counts(
    isolated_pg_base_url: str,
    tmp_path: Path,
    spec: list[tuple[str, str, str]],
    expected_train: int,
) -> None:
    plan = _synthetic_plan(spec)
    assert plan.train_member_count == expected_train  # non-75, derived from actual membership
    with scratch_database(
        isolated_pg_base_url, "minos_l2f_synth"
    ) as url:  # NOT the operational name
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


@pytest.mark.parametrize("revision", [_HEAD, _SOURCE_INDEX_HEAD])
def test_full_plan_persists_with_both_index_namespaces_equal(
    isolated_pg_base_url: str, tmp_path: Path, revision: str
) -> None:
    """The historical full-inventory contract, at 0006 and at repository head alike.

    For a plan that covers the COMPLETE live TRAIN inventory the plan-local ordinal and the source
    ``feature_matrix_members.member_index`` are the same ``0..N-1`` sequence — which is why the
    original resolver could bind them to one another, and exactly what stops being true for an
    authorized subset projection. From ``0012`` the stored ``source_matrix_member_index`` is
    checked as a third, independent value; before it, the single column carries both.
    """
    stores_source = revision == _SOURCE_INDEX_HEAD
    with scratch_database(isolated_pg_base_url, "minos_l2f_full") as url:
        alembic_upgrade(url, revision)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            result = _persist_experiment_plan_with_trust(
                engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
            )
            assert result.member_count == _ACCEPTED_PLAN.train_member_count == 50
            stored = (
                "pm.source_matrix_member_index AS stored_index"
                if stores_source
                else "pm.member_index AS stored_index"
            )
            with engine.connect() as conn:
                rows = [
                    (int(r.local_index), int(r.stored_index), int(r.live_index))
                    for r in conn.execute(
                        text(
                            "SELECT pm.member_index AS local_index, "
                            f"       {stored}, "
                            "       fmm.member_index AS live_index "
                            "  FROM experiments.l2f_experiment_plan_members pm "
                            "  JOIN profiling.feature_matrix_members fmm "
                            "    ON fmm.id = pm.feature_matrix_member_id "
                            " ORDER BY pm.member_index"
                        )
                    )
                ]
            assert rows == [(i, i, i) for i in range(50)]
            # replay is idempotent and changes nothing.
            replay = _persist_experiment_plan_with_trust(
                engine, _ACCEPTED_PLAN, _CS, publisher=_publisher(root)
            )
            assert replay.replay is True
            assert _graph_counts(engine)["l2f_experiment_plan_members"] == 50
        finally:
            engine.dispose()


def test_a_subset_plan_is_refused_before_0012(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """A pre-0012 schema stores ONE ordinal for both namespaces, so it says so explicitly.

    The frozen Phase-A plan is a genuine subset of the accepted TRAIN closure; at 0006 the
    persistence layer refuses it with a typed revision error rather than letting it surface as an
    opaque foreign-key violation deep inside the insert.
    """
    from minos_engine.baseline.phase_a import build_phase_a_plan

    plan = build_phase_a_plan()
    with scratch_database(isolated_pg_base_url, "minos_l2f_full") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            with pytest.raises(PlanRevisionError, match="predates 0012"):
                PS._execute_persistence_txn(
                    engine,
                    verify_identity=False,
                    build_inputs=lambda _conn: (plan, _CS, _publisher(root)),
                    upstream_resolver=PS._resolve_phase_a_upstream,
                )
            assert _artifact_files(root) == []
            assert _graph_counts(engine)["l2f_experiment_plans"] == 0
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# B. exact upstream train-set equality (no extra/missing live train members)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("set_defect", SET_DEFECTS)
def test_upstream_exact_set_negative(
    isolated_pg_base_url: str, tmp_path: Path, set_defect: str
) -> None:
    """A live upstream train inventory that differs from the plan's member inventory (extra /
    missing / mismatched dataset or index sets, or an inconsistent matrix row_count) is rejected
    with a typed UpstreamIdentityError before any publication — zero files / zero L2-F rows."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, set_defect=set_defect)
            root = _provisioned_root(tmp_path)
            with pytest.raises(UpstreamIdentityError):
                _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))
            assert _artifact_files(root) == []
            counts = _graph_counts(engine)
            assert counts["l2f_experiment_plans"] == 0
            assert counts["l2f_config_payloads"] == 0
        finally:
            engine.dispose()


def test_upstream_exact_set_valid_persists(isolated_pg_base_url: str, tmp_path: Path) -> None:
    """Exact live train inventory (no extras) passes the equality proof and persists."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            result = _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(root)
            )
            assert result.member_count == plan.train_member_count == 4
            assert result.jobs_count == 0
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# C/D. independent transaction-local graph verifier
# --------------------------------------------------------------------------- #
def _member_row(
    plan_id: str, upstream: dict[str, Any], m: Any, rm: dict[str, Any]
) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "profile_snapshot_id": upstream["profile_snapshot_id"],
        "feature_matrix_id": upstream["train_feature_matrix_id"],
        "profile_snapshot_member_id": rm["profile_snapshot_member_id"],
        "feature_matrix_member_id": rm["feature_matrix_member_id"],
        "bam_profile_id": rm["bam_profile_id"],
        "dataset_registry_id": rm["dataset_registry_id"],
        "partition": "train",
        "feature_values_hash": m.feature_values_hash,
        "member_index": m.member_index,
    }


def _hand_build_graph(
    conn: Connection, plan: Any, publisher: ConfigPayloadPublisher, *, defect: str | None
) -> tuple[str, dict[str, Any]]:
    """Build a complete L2-F graph for ``plan`` directly (reusing the store's insert helpers),
    optionally injecting exactly one build-time defect. Returns (plan_id, upstream)."""
    conn.execute(text("SET ROLE minos_admin"))
    upstream = PS._resolve_plan_upstream(conn, plan)
    tag = plan.plan_hash
    agen = U(f"art:gen:{tag}")

    payload_ids: list[str] = []
    for j, cand in enumerate(_CS.configs):
        payload = canonical_json_bytes(cand.effective_config)
        art = publisher.publish(payload, config_hash=cand.config_hash)
        if defect == "wrong_artifact_metadata" and j == 0:
            aid = conn.execute(
                text(
                    "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance)"
                    " VALUES (:u,:h,:m,:s,:p) RETURNING id"
                ),
                {
                    "u": art.uri,
                    "h": cand.config_hash,
                    "m": CONFIG_ARTIFACT_MEDIA_TYPE,
                    "s": len(payload) + 7,  # deliberately wrong stored byte size
                    "p": CONFIG_ARTIFACT_KIND,
                },
            ).scalar_one()
        else:
            aid = PS._register_config_artifact(
                conn,
                uri=art.uri,
                sha256=cand.config_hash,
                size_bytes=art.size_bytes,
                media_type=art.media_type,
            )
        pid, _ = PS._insert_or_verify(
            conn,
            table="l2f_config_payloads",
            row={
                "config_hash": cand.config_hash,
                "parameter_space_hash": plan.parameter_space_hash,
                "schema_version": L2F_CONFIG_PAYLOAD_SCHEMA,
                "media_type": CONFIG_ARTIFACT_MEDIA_TYPE,
                "artifact_id": aid,
            },
            unique_keys=PS._CONFIG_PAYLOAD_UNIQUE_KEYS,
        )
        payload_ids.append(pid)

    tmc, cc = plan.train_member_count, plan.candidate_count
    ljc = plan.logical_job_count
    if defect == "wrong_plan_count":
        tmc = tmc + 1
        ljc = tmc * cc  # keep the DB CHECK satisfied while disagreeing with actual membership
    plan_id, _ = PS._insert_or_verify(
        conn,
        table="l2f_experiment_plans",
        row={
            "profile_snapshot_id": upstream["profile_snapshot_id"],
            "train_feature_matrix_id": upstream["train_feature_matrix_id"],
            "feature_set_id": upstream["feature_set_id"],
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
            "train_member_count": tmc,
            "candidate_count": cc,
            "logical_job_count": ljc,
            "plan_hash": plan.plan_hash,
        },
        unique_keys=PS._PLAN_UNIQUE_KEYS,
    )

    members = list(zip(plan.members, upstream["members"], strict=True))
    if defect == "missing_member":
        members = members[:-1]
    for m, rm in members:
        PS._insert_or_verify(
            conn,
            table="l2f_experiment_plan_members",
            row=_member_row(plan_id, upstream, m, rm),
            unique_keys=PS._MEMBER_UNIQUE_KEYS,
        )
    if defect == "extra_member":
        _insert_extra_member(conn, plan, upstream, agen, plan_id)

    configs = list(zip(plan.configs, payload_ids, strict=True))
    if defect == "missing_config":
        configs = configs[:-1]
    for cfg, pid in configs:
        PS._insert_or_verify(
            conn,
            table="l2f_experiment_plan_configs",
            row={
                "plan_id": plan_id,
                "config_payload_id": pid,
                "config_hash": cfg.config_hash,
                "parameter_space_hash": cfg.parameter_space_hash,
                "config_index": cfg.config_index,
            },
            unique_keys=PS._PLAN_CONFIG_UNIQUE_KEYS,
        )
    if defect == "extra_config":
        _insert_extra_config(conn, plan, publisher, plan_id)
    if defect == "nonzero_jobs":
        _insert_job(conn, plan_id)
    return plan_id, upstream


def _insert_extra_member(
    conn: Connection, plan: Any, upstream: dict[str, Any], agen: str, plan_id: str
) -> None:
    tag = plan.plan_hash
    n = plan.train_member_count
    dsr = U(f"hb:extra:dsr:{tag}")
    drow = _dataset_row(f"hb-extra-{tag}", 4)
    drow["id"] = dsr
    drow["dataset_id"] = f"hb-extra-ds-{tag}"
    _insert(conn, "catalog", "dataset_registry", drow)
    bam = _bam_row(f"hb-extra-{tag}", dsr, agen)
    _insert(conn, "profiling", "bam_profiles", bam, jsonb_cols=("profile_document",))
    psm = U(f"hb:extra:psm:{tag}")
    _insert(
        conn,
        "profiling",
        "profile_snapshot_members",
        {
            "id": psm,
            "profile_snapshot_id": upstream["profile_snapshot_id"],
            "bam_profile_id": bam["id"],
            "dataset_registry_id": dsr,
            "partition": "train",
            "feature_values_hash": bam["feature_values_hash"],
        },
    )
    fmm = U(f"hb:extra:fmm:{tag}")
    _insert(
        conn,
        "profiling",
        "feature_matrix_members",
        {
            "id": fmm,
            "feature_matrix_id": upstream["train_feature_matrix_id"],
            "dataset_registry_id": dsr,
            "member_index": n,
            "vector_hash": H(f"hb:extra:vec:{tag}"),
            "feature_values_hash": bam["feature_values_hash"],
        },
    )
    PS._insert_or_verify(
        conn,
        table="l2f_experiment_plan_members",
        row={
            "plan_id": plan_id,
            "profile_snapshot_id": upstream["profile_snapshot_id"],
            "feature_matrix_id": upstream["train_feature_matrix_id"],
            "profile_snapshot_member_id": psm,
            "feature_matrix_member_id": fmm,
            "bam_profile_id": bam["id"],
            "dataset_registry_id": dsr,
            "partition": "train",
            "feature_values_hash": bam["feature_values_hash"],
            "member_index": n,
        },
        unique_keys=PS._MEMBER_UNIQUE_KEYS,
    )


def _insert_extra_config(
    conn: Connection, plan: Any, publisher: ConfigPayloadPublisher, plan_id: str
) -> None:
    payload = b'{"extra_config":true}'
    config_hash = hashlib.sha256(payload).hexdigest()
    art = publisher.publish(payload, config_hash=config_hash)
    aid = PS._register_config_artifact(
        conn, uri=art.uri, sha256=config_hash, size_bytes=art.size_bytes, media_type=art.media_type
    )
    pid, _ = PS._insert_or_verify(
        conn,
        table="l2f_config_payloads",
        row={
            "config_hash": config_hash,
            "parameter_space_hash": plan.parameter_space_hash,
            "schema_version": L2F_CONFIG_PAYLOAD_SCHEMA,
            "media_type": CONFIG_ARTIFACT_MEDIA_TYPE,
            "artifact_id": aid,
        },
        unique_keys=PS._CONFIG_PAYLOAD_UNIQUE_KEYS,
    )
    PS._insert_or_verify(
        conn,
        table="l2f_experiment_plan_configs",
        row={
            "plan_id": plan_id,
            "config_payload_id": pid,
            "config_hash": config_hash,
            "parameter_space_hash": plan.parameter_space_hash,
            "config_index": plan.candidate_count,  # one past the last valid index
        },
        unique_keys=PS._PLAN_CONFIG_UNIQUE_KEYS,
    )


def _insert_job(conn: Connection, plan_id: str) -> None:
    member_id = conn.execute(
        text("SELECT id FROM experiments.l2f_experiment_plan_members WHERE plan_id=:p LIMIT 1"),
        {"p": plan_id},
    ).scalar_one()
    config_id = conn.execute(
        text("SELECT id FROM experiments.l2f_experiment_plan_configs WHERE plan_id=:p LIMIT 1"),
        {"p": plan_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO experiments.l2f_experiment_jobs "
            "(plan_id, plan_member_id, plan_config_id, job_key, status) "
            "VALUES (:p,:m,:c,:k,'PENDING')"
        ),
        {"p": plan_id, "m": member_id, "c": config_id, "k": H(f"job:{plan_id}")},
    )


class _ReorderedCS:
    """A candidate-set view with two configs swapped (for the wrong-binding verifier test)."""

    def __init__(self, cs: Any) -> None:
        cfgs = list(cs.configs)
        cfgs[0], cfgs[1] = cfgs[1], cfgs[0]
        self.configs = tuple(cfgs)


_HANDBUILD_REJECTS = [
    "missing_member",
    "missing_config",
    "wrong_plan_count",
    "wrong_artifact_metadata",
    "extra_member",
    "extra_config",
]


@pytest.mark.parametrize("defect", _HANDBUILD_REJECTS)
def test_verifier_rejects_build_time_defect(
    isolated_pg_base_url: str, tmp_path: Path, defect: str
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect=defect)
                with pytest.raises(PlanVerificationError):
                    PS._verify_persisted_graph(conn, plan, _CS, plan_id, upstream)
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_verifier_accepts_valid_hand_built_graph(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect=None)
                counts = PS._verify_persisted_graph(conn, plan, _CS, plan_id, upstream)
                assert counts["member_count"] == plan.train_member_count
                assert counts["config_count"] == plan.candidate_count
                assert counts["payload_count"] == plan.candidate_count
                assert counts["jobs_count"] == 0
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_verifier_rejects_nonzero_jobs(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect="nonzero_jobs")
                with pytest.raises(PlanVerificationError):
                    PS._verify_persisted_graph(conn, plan, _CS, plan_id, upstream)
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_verifier_rejects_wrong_artifact_bytes(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect=None)
                # corrupt one published artifact file's bytes on disk (hash will no longer match).
                target = _artifact_files(root)[0]
                os.chmod(target, 0o640)
                target.write_bytes(b'{"tampered":true}')
                with pytest.raises(PlanVerificationError):
                    PS._verify_persisted_graph(conn, plan, _CS, plan_id, upstream)
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_verifier_rejects_wrong_member_uuid_binding(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """A stored member whose bam_profile UUID does not match the expected ExperimentPlanMember
    binding is rejected (exercised via the verifier's expected-vs-stored comparison; the FK graph
    makes such a state otherwise unconstructable without disabling production constraints)."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect=None)
                corrupted = dict(upstream)
                members = [dict(rm) for rm in upstream["members"]]
                members[0]["bam_profile_id"] = U("some:other:bam")
                corrupted["members"] = members
                with pytest.raises(PlanVerificationError):
                    PS._verify_persisted_graph(conn, plan, _CS, plan_id, corrupted)
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_verifier_rejects_wrong_config_payload_binding(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """A config whose payload does not correspond to its expected candidate is rejected
    (exercised via a reordered candidate set; the payload FK/UNIQUE graph makes a wrong stored
    binding otherwise unconstructable without disabling production constraints)."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            pub = _publisher(root)
            conn = engine.connect()
            trans = conn.begin()
            try:
                plan_id, upstream = _hand_build_graph(conn, plan, pub, defect=None)
                with pytest.raises(PlanVerificationError):
                    PS._verify_persisted_graph(conn, plan, _ReorderedCS(_CS), plan_id, upstream)
            finally:
                trans.rollback()
                conn.close()
        finally:
            engine.dispose()


def test_private_trust_boundary_not_exported() -> None:
    assert "_persist_experiment_plan_with_trust" not in PS.__all__
    assert "persist_accepted_experiment_plan" in PS.__all__
    with contextlib.suppress(TypeError):
        # the accepted entry point takes no arguments.
        import inspect

        assert list(inspect.signature(persist_accepted_experiment_plan).parameters) == []
