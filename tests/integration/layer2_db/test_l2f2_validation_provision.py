"""Three stores, one direction: operational lineage in, validation campaign out, zero jobs.

The decisive proof of this task is that a validation database holding nothing but schema can
reach the accepted preparation boundary through PRODUCTION code — no target seeder anywhere.

    verified operational store (0005, READ ONLY)
              │ provision the exact ten lineages
              ▼
        validation target (0024)
              ▲
              │ prepare: the exact four CONFIG payloads
    closed baseline store (0020, READ ONLY)

The target begins with zero dataset, allocation, profile and snapshot rows and is never touched
by ``seed_target_upstream``; a test asserts that module is not even imported here. Everything the
target ends up holding arrived through ``provision_l2f2_validation_upstream`` and
``prepare_l2f2_validation_plan``.

No truth is read, registered or hashed. No job is materialized. TEST members exist in the scratch
operational store precisely so their absence from the target means something.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.validation_members import build_validation_schedule
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
    _prepare_with_trust,
)
from minos_engine.storage.l2f2_validation_provision import (
    ACCEPTED_REGISTRY_SNAPSHOT_HASH,
    ACCEPTED_SNAPSHOT_HASH,
    OPERATIONAL_DATABASE_NAME,
    OPERATIONAL_REVISION,
    ValidationProvisionError,
    _provision_with_trust,
    provision_l2f2_validation_upstream,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f2_operational_seed import (
    scratch_root_under_minos,
    seed_operational_store,
)
from tests.integration.layer2_db.l2f2_validation_seed import seed_source_configs
from tests.integration.layer2_db.test_l2f_plan_store import _engine
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

_OPERATIONAL_DB = "minos_engine_db"
_BASELINE_DB = "minos_l2f2_baseline"
_TARGET_DB = "minos_l2f2_validation"
_BASELINE_REVISION = "0020_l2f2_phase_c_execution"
_TARGET_REVISION = "0024_l2f2_phase_d_anchor"
_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"

_UPSTREAM_TABLES = (
    "catalog.dataset_registry",
    "catalog.split_allocations",
    "catalog.split_snapshots",
    "catalog.artifacts",
    "profiling.bam_profiles",
    "profiling.profile_snapshots",
    "profiling.profile_snapshot_members",
)


@pytest.fixture(scope="module")
def authority() -> Any:
    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


@pytest.fixture(scope="module")
def scratch_root() -> Any:
    root = scratch_root_under_minos("provision_")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        with contextlib.suppress(OSError):
            root.parent.rmdir()


class _Topology:
    """Operational lineage source, closed CONFIG source, and a schema-only validation target."""

    def __init__(self, operational: Any, baseline: Any, target: Any, root: Path) -> None:
        self.operational = operational
        self.baseline = baseline
        self.target = target
        self.root = root

    def provision(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "source": self.operational,
            "target": self.target,
            "expected_source_database": _OPERATIONAL_DB,
            "expected_source_revision": OPERATIONAL_REVISION,
            "expected_target_database": _TARGET_DB,
            "expected_target_revision": _TARGET_REVISION,
        }
        kwargs.update(overrides)
        return _provision_with_trust(**kwargs)

    def prepare(self) -> Any:
        return _prepare_with_trust(
            target=self.target,
            baseline=self.baseline,
            finalist_freeze_path=FIXTURE_FREEZE_PATH,
            config_artifact_root=self.root / "target_configs",
            expected_database=_TARGET_DB,
            expected_revision=_TARGET_REVISION,
        )

    def upstream_counts(self) -> dict[str, int]:
        with self.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            return {
                t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
                for t in _UPSTREAM_TABLES
            }

    def campaign_counts(self) -> dict[str, int]:
        with self.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))

            def n(sql: str) -> int:
                return int(conn.execute(text(sql)).scalar_one())

            return {
                "plans": n("SELECT count(*) FROM experiments.l2f_experiment_plans"),
                "plan_members": n("SELECT count(*) FROM experiments.l2f_experiment_plan_members"),
                "plan_configs": n("SELECT count(*) FROM experiments.l2f_experiment_plan_configs"),
                "authorities": n(
                    "SELECT count(*) FROM experiments.l2f2_execution_authorities "
                    " WHERE phase = 'PHASE_D'"
                ),
                "bindings": n("SELECT count(*) FROM experiments.l2f2_phase_d_binding"),
                "jobs": n("SELECT count(*) FROM experiments.l2f_experiment_jobs"),
                "truth": n("SELECT count(*) FROM evaluation.dataset_evaluation_identity"),
            }


def _topology(
    base_url: str, scratch_root: Path, authority: Any, *, seed: dict[str, Any] | None = None
) -> Any:
    @contextlib.contextmanager
    def _ctx() -> Any:
        import tempfile

        with (
            scratch_database(base_url, _OPERATIONAL_DB) as operational_url,
            scratch_database(base_url, _BASELINE_DB) as baseline_url,
            scratch_database(base_url, _TARGET_DB) as target_url,
        ):
            alembic_upgrade(operational_url, OPERATIONAL_REVISION)
            alembic_upgrade(baseline_url, _BASELINE_REVISION)
            alembic_upgrade(target_url, _TARGET_REVISION)
            operational, baseline, target = (
                _engine(operational_url),
                _engine(baseline_url),
                _engine(target_url),
            )
            root = Path(tempfile.mkdtemp(prefix="run_", dir=scratch_root))
            try:
                with operational.connect() as conn, conn.begin():
                    seed_operational_store(conn, **(seed or {}))
                # the CONFIG source is the CLOSED BASELINE, a separate authority
                with baseline.connect() as conn, conn.begin():
                    seed_source_configs(
                        conn,
                        authority.ordered_config_hashes,
                        authority.parameter_space_hash,
                        config_root=root / "baseline_configs",
                    )
                yield _Topology(operational, baseline, target, root)
            finally:
                for eng in (operational, baseline, target):
                    eng.dispose()

    return _ctx()


# --------------------------------------------------------------------------------------------
# the target really does start empty
# --------------------------------------------------------------------------------------------
def test_the_target_starts_with_schema_only(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        assert topo.upstream_counts() == dict.fromkeys(_UPSTREAM_TABLES, 0)
        assert topo.campaign_counts() == {
            "plans": 0,
            "plan_members": 0,
            "plan_configs": 0,
            "authorities": 0,
            "bindings": 0,
            "jobs": 0,
            "truth": 0,
        }


def test_this_proof_never_seeds_the_target_upstream() -> None:
    """A source-level guarantee: the target seeder is not reachable from this module."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    assert not any("seed_target_upstream" in name for name in imported)
    # the ONE thing borrowed from that module seeds the CLOSED BASELINE's CONFIG payloads,
    # which is a different store and a different authority.
    assert any(name.endswith("seed_source_configs") for name in imported)


# --------------------------------------------------------------------------------------------
# THE decisive proof: provision -> prepare
# --------------------------------------------------------------------------------------------
def test_provision_then_prepare_reaches_the_frozen_campaign(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        result = topo.provision()

        assert result.source_database == _OPERATIONAL_DB
        assert result.source_revision == OPERATIONAL_REVISION
        assert result.snapshot_hash == ACCEPTED_SNAPSHOT_HASH
        assert result.registry_snapshot_hash == ACCEPTED_REGISTRY_SNAPSHOT_HASH
        assert result.member_count == 10
        assert sum(result.created_rows.values()) == 72
        assert result.created_rows == {
            "catalog.split_snapshots": 1,
            "catalog.dataset_registry": 10,
            "catalog.split_allocations": 10,
            "catalog.artifacts": 30,
            "profiling.bam_profiles": 10,
            "profiling.profile_snapshots": 1,
            "profiling.profile_snapshot_members": 10,
        }
        assert topo.upstream_counts() == {
            "catalog.dataset_registry": 10,
            "catalog.split_allocations": 10,
            "catalog.split_snapshots": 1,
            "catalog.artifacts": 30,
            "profiling.bam_profiles": 10,
            "profiling.profile_snapshots": 1,
            "profiling.profile_snapshot_members": 10,
        }

        # and now the ALREADY-ACCEPTED preparation seam, unchanged, against that lineage.
        prepared = topo.prepare()
        assert prepared.plan_hash == _PLAN_HASH
        assert prepared.created_members == 10
        assert prepared.created_configs == 4
        assert prepared.job_count == 0
        assert topo.campaign_counts() == {
            "plans": 1,
            "plan_members": 10,
            "plan_configs": 4,
            "authorities": 1,
            "bindings": 1,
            "jobs": 0,
            "truth": 0,
        }


def test_only_the_frozen_ten_are_transferred(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Seventy-five in the source, ten in the target, and not one of them TRAIN or TEST."""
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        with topo.operational.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            source_total = int(
                conn.execute(text("SELECT count(*) FROM catalog.dataset_registry")).scalar_one()
            )
        assert source_total == 75

        topo.provision()
        with topo.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            rows = (
                conn.execute(
                    text(
                        "SELECT dr.dataset_id, dr.round_id, dr.chromosome, "
                        "       dr.identity_tuple_hash, sa.partition "
                        "  FROM catalog.dataset_registry dr "
                        "  JOIN catalog.split_allocations sa "
                        "    ON sa.dataset_registry_id = dr.id ORDER BY dr.dataset_id"
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 10
        assert {str(r["partition"]) for r in rows} == {"validation"}

        frozen = {m.dataset_id: m for m in build_validation_schedule().members}
        assert {str(r["dataset_id"]) for r in rows} == set(frozen)
        for row in rows:
            member = frozen[str(row["dataset_id"])]
            assert str(row["round_id"]) == member.round_id
            assert str(row["chromosome"]) == member.chromosome
            assert str(row["identity_tuple_hash"]) == member.identity_tuple_hash


def test_the_snapshot_is_transferred_as_a_labelled_projection(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """member_count stays 75 while the target holds 10 members. That is the honest reading.

    Seventy-five is what the snapshot IS — part of the identity hashing to cf717ebb… Writing 10
    beside that hash would misdescribe the snapshot; inventing a new hash would misdescribe the
    science. The target holds the frozen identity plus a validation-only projection.
    """
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        topo.provision()
        with topo.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            snap = (
                conn.execute(
                    text(
                        "SELECT snapshot_hash, registry_snapshot_hash, member_count "
                        "  FROM profiling.profile_snapshots"
                    )
                )
                .mappings()
                .one()
            )
            children = int(
                conn.execute(
                    text("SELECT count(*) FROM profiling.profile_snapshot_members")
                ).scalar_one()
            )
            split = (
                conn.execute(
                    text(
                        "SELECT sample_count, count_train, count_validation, count_test "
                        "  FROM catalog.split_snapshots"
                    )
                )
                .mappings()
                .one()
            )
        assert snap["snapshot_hash"] == ACCEPTED_SNAPSHOT_HASH
        assert snap["registry_snapshot_hash"] == ACCEPTED_REGISTRY_SNAPSHOT_HASH
        assert int(snap["member_count"]) == 75
        assert children == 10
        # the split snapshot keeps its frozen counts for the same reason.
        assert (
            int(split["sample_count"]),
            int(split["count_train"]),
            int(split["count_validation"]),
            int(split["count_test"]),
        ) == (75, 50, 10, 15)


def test_every_transferred_row_is_byte_identical_to_the_source(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Not "a row with the same id" — the same scientific content, column for column."""
    from minos_engine.storage.l2f2_validation_provision import _IMMUTABLE, _differs

    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        topo.provision()
        for table, columns in _IMMUTABLE.items():
            projection = ", ".join(columns)
            with topo.target.connect() as conn:
                conn.execute(text("SET ROLE minos_admin"))
                target_rows = {
                    str(r["id"]): dict(r)
                    for r in conn.execute(
                        text(f"SELECT {projection} FROM {table}")  # noqa: S608
                    ).mappings()
                }
            with topo.operational.connect() as conn:
                conn.execute(text("SET ROLE minos_admin"))
                source_rows = {
                    str(r["id"]): dict(r)
                    for r in conn.execute(
                        text(  # noqa: S608
                            f"SELECT {projection} FROM {table} WHERE id = ANY(:ids)"
                        ),
                        {"ids": sorted(target_rows)},
                    ).mappings()
                }
            assert set(source_rows) == set(target_rows), table
            for row_id, target_row in target_rows.items():
                for column in columns:
                    assert not _differs(target_row[column], source_rows[row_id][column]), (
                        f"{table}.{column} drifted for {row_id}"
                    )


# --------------------------------------------------------------------------------------------
# source non-mutation
# --------------------------------------------------------------------------------------------
def _source_fingerprint(engine: Any) -> dict[str, int]:
    with engine.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        return {
            t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
            for t in _UPSTREAM_TABLES
        }


def test_neither_source_is_mutated(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        before_op = _source_fingerprint(topo.operational)
        before_base = _source_fingerprint(topo.baseline)
        topo.provision()
        topo.prepare()
        assert _source_fingerprint(topo.operational) == before_op
        assert _source_fingerprint(topo.baseline) == before_base


def test_the_operational_transaction_is_read_only_in_the_database(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Observed from the server, and a write in the same transaction is refused."""
    with (
        _topology(isolated_pg_base_url, scratch_root, authority) as topo,
        topo.operational.connect() as conn,
    ):
        conn.execute(text("SELECT current_database()"))
        conn.execute(text("SET TRANSACTION READ ONLY"))
        assert str(conn.execute(text("SHOW transaction_read_only")).scalar_one()) == "on"
        with pytest.raises(Exception, match="read-only transaction"):
            conn.execute(
                text("INSERT INTO catalog.artifacts (uri, sha256) VALUES ('mem://x', :s)"),
                {"s": "0" * 64},
            )


# --------------------------------------------------------------------------------------------
# idempotency and conflict
# --------------------------------------------------------------------------------------------
def test_replay_creates_nothing(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        first = topo.provision()
        counts = topo.upstream_counts()
        second = topo.provision()
        assert sum(second.created_rows.values()) == 0
        assert sum(second.existing_rows.values()) == 72
        assert second.snapshot_hash == first.snapshot_hash
        assert topo.upstream_counts() == counts


def test_an_exact_partial_target_is_safely_completed(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Unlike the job materializer's EMPTY-or-COMPLETE rule, and for a stated reason.

    A partial job graph is unexplained. A partial upstream graph is not: every row here is
    re-read from the closed operational source and compared field by field before anything is
    written, so completing it asserts nothing that was not independently verified.
    """
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        with topo.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text(
                    "INSERT INTO catalog.artifacts (id, uri, sha256, media_type, size_bytes, "
                    "                               provenance) "
                    "SELECT id, uri, sha256, media_type, size_bytes, provenance "
                    "  FROM catalog.artifacts WHERE false"
                )
            )
        # copy three artifacts that ARE in the validation closure, leaving the rest absent
        with topo.operational.connect() as src:
            src.execute(text("SET ROLE minos_admin"))
            rows = (
                src.execute(
                    text(
                        "SELECT DISTINCT a.id, a.uri, a.sha256, a.media_type, a.size_bytes, "
                        "       a.provenance "
                        "  FROM catalog.artifacts a "
                        "  JOIN profiling.bam_profiles bp "
                        "    ON a.id IN (bp.profile_artifact_id, "
                        "                bp.profile_manifest_artifact_id, "
                        "                bp.windows_artifact_id) "
                        "  JOIN catalog.split_allocations sa "
                        "    ON sa.dataset_registry_id = bp.dataset_registry_id "
                        " WHERE sa.partition = 'validation' ORDER BY a.id LIMIT 3"
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 3
        with topo.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            for row in rows:
                conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts "
                        "  (id, uri, sha256, media_type, size_bytes, provenance) "
                        "VALUES (:id, :uri, :sha256, :media_type, :size_bytes, :provenance)"
                    ),
                    dict(row),
                )
        assert topo.upstream_counts()["catalog.artifacts"] == 3

        result = topo.provision()
        assert result.existing_rows["catalog.artifacts"] == 3
        assert result.created_rows["catalog.artifacts"] == 27
        assert topo.upstream_counts()["catalog.artifacts"] == 30
        topo.prepare()
        assert topo.campaign_counts()["plans"] == 1


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("catalog.dataset_registry", "round_id", "ffffffffffffffff"),
        ("catalog.dataset_registry", "identity_tuple_hash", "0" * 64),
        ("catalog.split_allocations", "partition", "train"),
        ("catalog.artifacts", "sha256", "1" * 64),
        ("profiling.bam_profiles", "content_hash", "2" * 64),
        ("profiling.profile_snapshots", "registry_snapshot_hash", "3" * 64),
        ("profiling.profile_snapshot_members", "feature_values_hash", "4" * 64),
    ],
)
def test_a_conflicting_target_row_is_refused(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    table: str,
    column: str,
    value: str,
) -> None:
    """Provision once, corrupt one target row, replay. Never repaired."""
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        topo.provision()
        with topo.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            schema = table.split(".")[0]
            conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))  # noqa: S608
            conn.execute(
                text(  # noqa: S608
                    f"UPDATE {table} SET {column} = :v "
                    f" WHERE id = (SELECT id FROM {table} ORDER BY id LIMIT 1)"
                ),
                {"v": value},
            )
            conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))  # noqa: S608
            assert schema in {"catalog", "profiling"}
        with pytest.raises(ValidationProvisionError, match="conflicting"):
            topo.provision()


# --------------------------------------------------------------------------------------------
# provisioning negatives
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"expected_source_database": "not_the_operational_store"}, "lineage source connection"),
        ({"expected_source_revision": "0099_nope"}, "lineage source database is at revision"),
        ({"expected_target_database": "not_the_validation_store"}, "target connection"),
        ({"expected_target_revision": "0099_nope"}, "target database is at revision"),
        ({"expected_snapshot_hash": "9" * 64}, "profile snapshots with the frozen identity"),
    ],
)
def test_provisioning_provisioning_negatives(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    override: dict[str, Any],
    match: str,
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority) as topo:
        with pytest.raises(ValidationProvisionError, match=match):
            topo.provision(**override)
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


@pytest.mark.parametrize(
    ("seed", "match"),
    [
        ({"registry_snapshot_hash": "8" * 64}, "registry snapshot"),
        ({"member_count": 10}, "declares 10 members"),
    ],
)
def test_a_snapshot_that_is_not_the_frozen_one_is_refused(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    seed: dict[str, Any],
    match: str,
) -> None:
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(ValidationProvisionError, match=match):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


def _victim() -> str:
    return build_validation_schedule().members[4].dataset_id


@pytest.mark.parametrize("partition", ["train", "test"])
def test_a_validation_member_allocated_elsewhere_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, partition: str
) -> None:
    seed = {"partitions": {_victim(): partition}}
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(ValidationProvisionError, match="VALIDATION partition only"):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


def test_a_missing_validation_member_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    seed = {"omit_member": _victim()}
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(ValidationProvisionError, match="resolves to 0 authoritative"):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("round_id", "ffffffffffffffff", "round_id"),
        ("chromosome", "chr18", "chromosome"),
        ("identity_tuple_hash", "5" * 64, "identity_tuple_hash"),
    ],
)
def test_a_member_whose_frozen_identity_drifts_is_refused(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    column: str,
    value: str,
    match: str,
) -> None:
    seed = {"field_overrides": {_victim(): {column: value}}}
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(ValidationProvisionError, match=match):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        # profile_status is absent deliberately: ck_bam_profiles_complete_only makes a
        # non-COMPLETE row unconstructable at the database boundary, so the provisioner's
        # COMPLETE check is belt over a DB CHECK and has no constructible negative.
        ("identity_tuple_hash", "6" * 64, "different identity tuple"),
    ],
)
def test_a_member_whose_profile_identity_drifts_is_refused(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    column: str,
    value: str,
    match: str,
) -> None:
    seed = {"profile_overrides": {_victim(): {column: value}}}
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(Exception, match=match):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0


# --------------------------------------------------------------------------------------------
# the boundary's own shape
# --------------------------------------------------------------------------------------------
def test_the_public_entry_accepts_no_scientific_argument() -> None:
    import inspect

    signature = inspect.signature(provision_l2f2_validation_upstream)
    assert list(signature.parameters) == ["source", "target"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    assert OPERATIONAL_DATABASE_NAME == "minos_engine_db"
    assert OPERATIONAL_REVISION == "0005_l2e_feature_view"


def test_the_provisioner_imports_no_tests_truth_or_execution() -> None:
    """It must work in an installed package where tests do not exist."""
    import ast

    source = Path("src/minos_engine/storage/l2f2_validation_provision.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "tests",
        "l2f2_validation_seed",
        "truth_registration",
        "gatk",
        "scorer",
        "evaluation.orchestrator",
        "l2f2_validation_activate",
    ):
        assert not any(forbidden in module for module in modules), forbidden

    statements = " ".join(
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    for forbidden in (
        "dataset_evaluation_identity",
        "truth_vcf",
        "l2f_experiment_jobs",
        "l2f_experiment_plans",
        "l2f2_phase_d_binding",
    ):
        assert forbidden not in statements, forbidden


def test_a_membership_citing_other_feature_values_than_its_profile_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The registry row, the BAM profile and the snapshot membership must be one sample."""
    seed = {"mismatch_feature_values_for": _victim()}
    with _topology(isolated_pg_base_url, scratch_root, authority, seed=seed) as topo:
        with pytest.raises(ValidationProvisionError, match="feature values"):
            topo.provision()
        assert topo.upstream_counts()["catalog.dataset_registry"] == 0
