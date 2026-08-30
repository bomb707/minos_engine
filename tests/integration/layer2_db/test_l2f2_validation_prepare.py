"""The Phase-D preparation boundary, proven on two scratch databases, ending at ZERO jobs.

Preparation is the only place in L2-F2-F where the closed TRAIN baseline and the empty validation
store are open at the same time, so it is the only place a scientific identity could cross from one
campaign into another. That is what these tests are about. The happy path is short; almost
everything here is a forgery that must not survive.

The four configurations are the campaign's REAL frozen payloads, copied byte for byte into each
scratch source. They have to be: ``0024`` anchors the four hashes as SQL literals, so a synthetic
campaign cannot reach the bootstrap at all, and a proof that only ever ran on synthetic identities
would prove nothing about this one.

Nothing here materializes a job, registers a truth identity, runs GATK, scores anything or touches
TEST. Preparation ends at zero jobs by contract, and that count is asserted on every path that
reaches the end.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
    ValidationPrepareError,
    _prepare_with_trust,
    prepare_l2f2_validation_plan,
)
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f2_validation_seed import (
    seed_source_configs,
    seed_target_upstream,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_SOURCE_DB = "minos_l2f2_baseline"
_TARGET_DB = "minos_l2f2_validation"
_SOURCE_REVISION = "0020_l2f2_phase_c_execution"
_TARGET_REVISION = "0024_l2f2_phase_d_anchor"

_FREEZE_PATH = Path(
    "/home/hr/bittensor/minos_l2f2_baseline/phase_c_validation_finalists_20260830.json"
)
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"


# --------------------------------------------------------------------------------------------
# fixtures: the derived authority, and the two stores
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def authority() -> Any:
    freeze = load_finalist_freeze(
        _FREEZE_PATH,
        expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
        expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )
    return build_l2f2_phase_d_authority(freeze)


class _Stores:
    """A scratch source at 0020 and a scratch target at the validation revision."""

    def __init__(self, source: Any, target: Any, config_root: Path) -> None:
        self.source = source
        self.target = target
        self.config_root = config_root

    def prepare(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "target": self.target,
            "baseline": self.source,
            "finalist_freeze_path": _FREEZE_PATH,
            "config_artifact_root": self.config_root / "target",
            "expected_database": _TARGET_DB,
            "expected_revision": _TARGET_REVISION,
        }
        kwargs.update(overrides)
        return _prepare_with_trust(**kwargs)

    def counts(self) -> dict[str, int]:
        with self.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            return {
                "plans": _scalar(conn, "SELECT count(*) FROM experiments.l2f_experiment_plans"),
                "members": _scalar(
                    conn, "SELECT count(*) FROM experiments.l2f_experiment_plan_members"
                ),
                "configs": _scalar(
                    conn, "SELECT count(*) FROM experiments.l2f_experiment_plan_configs"
                ),
                "payloads": _scalar(conn, "SELECT count(*) FROM experiments.l2f_config_payloads"),
                "authorities": _scalar(
                    conn,
                    "SELECT count(*) FROM experiments.l2f2_execution_authorities "
                    " WHERE phase = 'PHASE_D'",
                ),
                "bindings": _scalar(conn, "SELECT count(*) FROM experiments.l2f2_phase_d_binding"),
                "jobs": _scalar(conn, "SELECT count(*) FROM experiments.l2f_experiment_jobs"),
                "truth": _scalar(
                    conn,
                    "SELECT count(*) FROM evaluation.dataset_evaluation_identity "
                    " WHERE truth_vcf_sha256 IS NOT NULL",
                ),
            }


def _a_non_finalist_config_hash(authority: Any) -> str:
    """A REAL Phase-C candidate payload that is not one of the four. Never a fabricated hash."""
    frozen = set(authority.ordered_config_hashes)
    for path in sorted(
        Path("/home/hr/bittensor/minos_l2f2_baseline/config_artifacts").glob("*.json")
    ):
        if path.stem not in frozen and len(path.stem) == 64:
            return path.stem
    raise AssertionError("the campaign artifact root holds no non-finalist configuration")


def _scalar(conn: Any, sql: str) -> int:
    return int(conn.execute(text(sql)).scalar_one())


def _stores(
    base_url: str,
    tmp_path: Path,
    authority: Any,
    *,
    source_db: str = _SOURCE_DB,
    target_db: str = _TARGET_DB,
    source_revision: str = _SOURCE_REVISION,
    target_revision: str = _TARGET_REVISION,
    tamper: str | None = None,
    **seed_kwargs: Any,
) -> Any:
    """Context manager yielding both scratch stores, seeded and ready."""
    import contextlib

    @contextlib.contextmanager
    def _ctx() -> Any:
        with (
            scratch_database(base_url, source_db) as source_url,
            scratch_database(base_url, target_db) as target_url,
        ):
            alembic_upgrade(source_url, source_revision)
            alembic_upgrade(target_url, target_revision)
            source = _engine(source_url)
            target = _engine(target_url)
            try:
                root = tmp_path / source_db
                with source.connect() as conn, conn.begin():
                    seed_source_configs(
                        conn,
                        authority.ordered_config_hashes,
                        authority.parameter_space_hash,
                        config_root=root / "source",
                        tamper=tamper,
                    )
                with target.connect() as conn, conn.begin():
                    seed_target_upstream(
                        conn,
                        authority.schedule.members,
                        split_manifest_hash=authority.split_manifest_sha256,
                        **seed_kwargs,
                    )
                yield _Stores(source, target, root)
            finally:
                source.dispose()
                target.dispose()

    return _ctx()


# --------------------------------------------------------------------------------------------
# the happy path — and the zero it ends at
# --------------------------------------------------------------------------------------------
def test_preparation_writes_one_campaign_and_no_jobs(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        assert stores.counts() == {
            "plans": 0,
            "members": 0,
            "configs": 0,
            "payloads": 0,
            "authorities": 0,
            "bindings": 0,
            "jobs": 0,
            "truth": 0,
        }
        result = stores.prepare()

        assert result.plan_hash == _PLAN_HASH == authority.plan_hash
        assert result.created_plan is True
        assert result.created_members == 10
        assert result.created_configs == 4
        assert result.created_authority is True
        assert result.created_binding is True
        assert result.job_count == 0
        # the database's own bootstrap, not this module's opinion of it.
        assert result.bootstrap_plan_hash == _PLAN_HASH
        assert result.bootstrap_environment_hash == _ENVIRONMENT

        assert stores.counts() == {
            "plans": 1,
            "members": 10,
            "configs": 4,
            "payloads": 4,
            "authorities": 1,
            "bindings": 1,
            "jobs": 0,
            "truth": 0,
        }


def test_the_persisted_campaign_is_the_frozen_one(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Order, partition, indices and lineage nullity — read back from the target."""
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        stores.prepare()
        with stores.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            configs = [
                (str(r["config_hash"]), int(r["config_index"]))
                for r in conn.execute(
                    text(
                        "SELECT config_hash, config_index "
                        "  FROM experiments.l2f_experiment_plan_configs ORDER BY config_index"
                    )
                ).mappings()
            ]
            assert configs == [(h, i) for i, h in enumerate(authority.ordered_config_hashes)]

            plan = (
                conn.execute(
                    text(
                        "SELECT partition, train_feature_matrix_id, train_matrix_hash, "
                        "       train_feature_view_hash, feature_set_id, feature_set_hash, "
                        "       feature_registry_hash, candidate_count, train_member_count, "
                        "       logical_job_count "
                        "  FROM experiments.l2f_experiment_plans"
                    )
                )
                .mappings()
                .one()
            )
            assert plan["partition"] == "validation"
            # 0022: a validation plan carries NO matrix lineage. NULL, never a placeholder.
            for column in (
                "train_feature_matrix_id",
                "train_matrix_hash",
                "train_feature_view_hash",
                "feature_set_id",
                "feature_set_hash",
                "feature_registry_hash",
            ):
                assert plan[column] is None, column
            assert (plan["candidate_count"], plan["train_member_count"]) == (4, 10)
            assert plan["logical_job_count"] == 40

            members = [
                (str(r["partition"]), r["feature_matrix_id"], r["feature_matrix_member_id"])
                for r in conn.execute(
                    text(
                        "SELECT partition, feature_matrix_id, feature_matrix_member_id "
                        "  FROM experiments.l2f_experiment_plan_members ORDER BY member_index"
                    )
                ).mappings()
            ]
            assert len(members) == 10
            assert all(m == ("validation", None, None) for m in members)

            binding = (
                conn.execute(
                    text(
                        "SELECT ordered_config_hashes, inherited_candidate_indices, "
                        "       seed_config_hash, finalist_freeze_sha256, "
                        "       phase_c_closure_sha256, split_manifest_sha256 "
                        "  FROM experiments.l2f2_phase_d_binding"
                    )
                )
                .mappings()
                .one()
            )
            assert list(binding["ordered_config_hashes"]) == list(authority.ordered_config_hashes)
            assert list(binding["inherited_candidate_indices"]) == [42, 25, 36, 0]
            assert binding["seed_config_hash"] == authority.seed_config_hash
            assert binding["finalist_freeze_sha256"] == ACCEPTED_FINALIST_FREEZE_SHA256
            assert binding["phase_c_closure_sha256"] == ACCEPTED_PHASE_C_CLOSURE_SHA256
            assert binding["split_manifest_sha256"] == authority.split_manifest_sha256


def test_the_carried_payloads_are_the_campaign_bytes(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Every payload published into the target is byte-identical to the campaign's own."""
    campaign = Path("/home/hr/bittensor/minos_l2f2_baseline/config_artifacts")
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        stores.prepare()
        for config_hash in authority.ordered_config_hashes:
            published = (stores.config_root / "target" / f"{config_hash}.json").read_bytes()
            assert hashlib.sha256(published).hexdigest() == config_hash
            assert published == (campaign / f"{config_hash}.json").read_bytes()


def test_the_source_baseline_is_not_written(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The closed TRAIN store is evidence. Preparation reads it and cannot write to it."""
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with stores.source.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            before = _scalar(conn, "SELECT count(*) FROM experiments.l2f_experiment_plans")
            payloads = _scalar(conn, "SELECT count(*) FROM experiments.l2f_config_payloads")
        stores.prepare()
        with stores.source.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            assert _scalar(conn, "SELECT count(*) FROM experiments.l2f_experiment_plans") == before
            assert _scalar(conn, "SELECT count(*) FROM experiments.l2f_config_payloads") == payloads
            assert _scalar(conn, "SELECT count(*) FROM experiments.l2f2_execution_authorities") == 0


# --------------------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------------------
def test_replay_is_idempotent_and_creates_nothing(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        first = stores.prepare()
        after_first = stores.counts()
        second = stores.prepare()

        assert second.plan_id == first.plan_id
        assert second.authority_id == first.authority_id
        assert second.binding_id == first.binding_id
        assert second.plan_hash == first.plan_hash
        assert (second.created_plan, second.created_authority, second.created_binding) == (
            False,
            False,
            False,
        )
        assert (second.created_members, second.created_configs) == (0, 0)
        assert second.job_count == 0
        assert stores.counts() == after_first


def test_a_replay_against_a_conflicting_persisted_four_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """A plan already persisted under this hash with other configurations is a conflict."""
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        stores.prepare()
        # a genuine fifth configuration — a REAL Phase-C candidate that is not a finalist —
        # linked into the persisted plan. The plan graph and its binding now disagree, and a
        # replay must refuse rather than reconcile: these tables are append-only, so "repair"
        # would mean asserting a scientific identity nobody can revoke.
        intruder = _a_non_finalist_config_hash(authority)
        with stores.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            artifact_id = conn.execute(
                text(
                    "INSERT INTO catalog.artifacts "
                    "  (uri, sha256, media_type, size_bytes, provenance) "
                    "VALUES (:u, :s, :m, 1, 'intruder') RETURNING id"
                ),
                {
                    "u": f"file:///nowhere/{intruder}.json",
                    "s": intruder,
                    "m": "application/vnd.minos.l2f-config+json",
                },
            ).scalar_one()
            payload_id = conn.execute(
                text(
                    "INSERT INTO experiments.l2f_config_payloads "
                    "  (config_hash, parameter_space_hash, schema_version, media_type, "
                    "   artifact_id) "
                    "VALUES (:h, :p, 'l2f-config-payload-v1', "
                    "        'application/vnd.minos.l2f-config+json', :a) RETURNING id"
                ),
                {"h": intruder, "p": authority.parameter_space_hash, "a": artifact_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_plan_configs "
                    "  (plan_id, config_payload_id, config_hash, parameter_space_hash, "
                    "   config_index) "
                    "SELECT p.id, :pid, :h, p.parameter_space_hash, 4 "
                    "  FROM experiments.l2f_experiment_plans p WHERE p.plan_hash = :ph"
                ),
                {"pid": payload_id, "h": intruder, "ph": authority.plan_hash},
            )
        with pytest.raises(ValidationPrepareError, match="different configurations"):
            stores.prepare()


# --------------------------------------------------------------------------------------------
# provisioning negatives: the wrong database, the wrong revision
# --------------------------------------------------------------------------------------------
def test_a_target_that_is_not_the_validation_store_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(ValidationPrepareError, match="validation target connection"):
            stores.prepare(expected_database="some_other_store")
        assert stores.counts()["plans"] == 0


def test_a_target_at_the_wrong_revision_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(ValidationPrepareError, match="expected '0025"):
            stores.prepare(expected_revision="0025_not_a_revision")
        assert stores.counts()["plans"] == 0


def test_a_source_that_is_not_the_closed_baseline_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The four payloads may be carried only out of the closed TRAIN store at 0020."""
    with _stores(
        isolated_pg_base_url,
        tmp_path,
        authority,
        source_db="minos_l2f2_not_the_baseline",
    ) as stores:
        with pytest.raises(ValidationPrepareError, match="baseline source connection"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_a_source_at_the_wrong_revision_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(
        isolated_pg_base_url,
        tmp_path,
        authority,
        source_revision="0019_l2f2_phase_b_bootstrap",
    ) as stores:
        with pytest.raises(ValidationPrepareError, match="baseline source database is at revision"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_the_production_entry_point_pins_both_stores_by_name(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The public boundary takes NO identity parameters — the names are compiled in."""
    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    assert VALIDATION_DATABASE_NAME == _TARGET_DB
    assert VALIDATION_REVISION == _TARGET_REVISION
    with (
        _stores(
            isolated_pg_base_url, tmp_path, authority, target_db="minos_l2f2_wrong_target"
        ) as stores,
        pytest.raises(ValidationPrepareError, match="validation target connection"),
    ):
        prepare_l2f2_validation_plan(
            target=stores.target,
            baseline=stores.source,
            finalist_freeze_path=_FREEZE_PATH,
            config_artifact_root=stores.config_root / "target",
        )


# --------------------------------------------------------------------------------------------
# artifact negatives: the freeze, the closure, the payload bytes
# --------------------------------------------------------------------------------------------
def test_a_freeze_artifact_with_another_digest_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(Exception, match="hashes to"):
            stores.prepare(expected_freeze_sha256="0" * 64)
        assert stores.counts()["plans"] == 0


def test_a_freeze_citing_another_phase_c_closure_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(Exception, match="closure|sha256"):
            stores.prepare(expected_closure_sha256="1" * 64)
        assert stores.counts()["plans"] == 0


def test_a_forged_freeze_document_never_reaches_the_database(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Four well-formed hashes that are not the frozen four, in a re-digested artifact."""
    document = json.loads(_FREEZE_PATH.read_text(encoding="utf-8"))
    forged = tuple(f"{d}" * 64 for d in "1234")
    for index, entry in enumerate(document["validation_finalists_ordered"]):
        if isinstance(entry, dict):
            entry["config_hash"] = forged[index]
        else:
            document["validation_finalists_ordered"][index] = forged[index]
    path = tmp_path / "forged_freeze.json"
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal, none may pass
            stores.prepare(finalist_freeze_path=path, expected_freeze_sha256=digest)
        assert stores.counts()["plans"] == 0


def test_a_tampered_config_payload_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The registered artifact digest for a CONFIG payload IS its config hash."""
    victim = authority.ordered_config_hashes[2]
    with _stores(isolated_pg_base_url, tmp_path, authority, tamper=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="tampered with or substituted"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_a_missing_config_payload_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        victim = authority.ordered_config_hashes[1]
        (stores.config_root / "source" / f"{victim}.json").unlink()
        with pytest.raises(ValidationPrepareError, match="does not exist"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_a_config_registered_under_another_parameter_space_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        # the source is scratch, so its payload row can be re-registered under a foreign space by
        # rebuilding it; l2f_config_payloads is append-only, so the row is replaced by dropping
        # and recreating the whole store's payload for one hash is impossible — instead a SECOND
        # payload row for the same hash is impossible too (unique). The honest probe is therefore
        # a source whose payload was registered with a different space from the start.
        victim = authority.ordered_config_hashes[0]
        with stores.source.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(text("ALTER TABLE experiments.l2f_config_payloads DISABLE TRIGGER USER"))
            conn.execute(
                text(
                    "UPDATE experiments.l2f_config_payloads SET parameter_space_hash = :s "
                    " WHERE config_hash = :h"
                ),
                {"s": "9" * 64, "h": victim},
            )
            conn.execute(text("ALTER TABLE experiments.l2f_config_payloads ENABLE TRIGGER USER"))
        with pytest.raises(ValidationPrepareError, match="binds parameter space"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


# --------------------------------------------------------------------------------------------
# member negatives: TRAIN, TEST, ambiguity, snapshot spread
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("partition", ["train", "test"])
def test_a_member_outside_validation_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any, partition: str
) -> None:
    victim = authority.schedule.members[0].dataset_id
    with _stores(
        isolated_pg_base_url, tmp_path, authority, partitions={victim: partition}
    ) as stores:
        with pytest.raises(ValidationPrepareError, match="VALIDATION only"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_an_ambiguous_member_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    victim = authority.schedule.members[4].dataset_id
    with _stores(isolated_pg_base_url, tmp_path, authority, duplicate_dataset=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="upstream rows"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_members_spanning_two_snapshots_are_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    victim = authority.schedule.members[7].dataset_id
    with _stores(isolated_pg_base_url, tmp_path, authority, second_snapshot_for=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="profile snapshots"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_a_member_absent_from_the_target_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Nine of ten is not the campaign. Preparation never shrinks the schedule to fit the store."""
    victim = authority.schedule.members[9].dataset_id
    with _stores(isolated_pg_base_url, tmp_path, authority, omit_member=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="resolves to 0 upstream rows"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


# --------------------------------------------------------------------------------------------
# the boundary refuses to leave a half-campaign behind
# --------------------------------------------------------------------------------------------
def test_a_refused_bootstrap_rolls_the_whole_preparation_back(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """A PHASE_D authority already present makes the bootstrap refuse to choose. Nothing sticks."""
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with stores.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            # a decoy plan and a second PHASE_D authority over it: the bootstrap must then refuse,
            # and preparation must not leave its own rows behind when it does.
            plan_id = conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_plans ("
                    "  profile_snapshot_id, partition, snapshot_hash, split_manifest_hash, "
                    "  registry_snapshot_hash, gatk_registry_hash, parameter_space_hash, "
                    "  experiment_parameter_policy_hash, candidate_set_hash, train_member_count, "
                    "  candidate_count, logical_job_count, plan_hash) "
                    "SELECT ps.id, 'validation', ps.snapshot_hash, ps.split_manifest_hash, "
                    "       ps.registry_snapshot_hash, :g, :p, :e, :c, 1, 1, 1, :h "
                    "  FROM profiling.profile_snapshots ps LIMIT 1 RETURNING id"
                ),
                {
                    "g": "a" * 64,
                    "p": "b" * 64,
                    "e": "c" * 64,
                    "c": "d" * 64,
                    "h": "e" * 64,
                },
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f2_execution_authorities ("
                    "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                    "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                    "  logical_job_count) "
                    "VALUES (:proto, 'PHASE_D', :pl, :h, :s, :c, :p, 1, 1, 1)"
                ),
                {
                    "proto": ("c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"),
                    "pl": plan_id,
                    "h": "e" * 64,
                    "s": "f" * 64,
                    "c": "d" * 64,
                    "p": "b" * 64,
                },
            )
        before = stores.counts()
        with pytest.raises(Exception, match="more than one PHASE_D|refusing to choose"):
            stores.prepare()
        # exactly the decoy remains: no plan, member, config, authority or binding of ours.
        assert stores.counts() == before


def test_a_member_on_another_round_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    victim = authority.schedule.members[2].dataset_id
    with _stores(isolated_pg_base_url, tmp_path, authority, wrong_round_for=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="the frozen schedule says"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


def test_a_member_on_another_chromosome_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    victim = authority.schedule.members[6].dataset_id
    with _stores(isolated_pg_base_url, tmp_path, authority, wrong_chromosome_for=victim) as stores:
        with pytest.raises(ValidationPrepareError, match="the frozen schedule says"):
            stores.prepare()
        assert stores.counts()["plans"] == 0


# --------------------------------------------------------------------------------------------
# the boundary's own shape
# --------------------------------------------------------------------------------------------
def test_preparation_cannot_materialize_a_job() -> None:
    """A source-level guarantee, not a count: the module has no path to a job at all.

    Asserted on executable statements rather than on the whole file, so prose that *names* the
    materializer in order to say it is elsewhere does not trip the check.
    """
    import ast

    path = Path("src/minos_engine/storage/l2f2_validation_prepare.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    statements = " ".join(literals).lower()
    for forbidden in (
        "insert into experiments.l2f_experiment_jobs",
        "update experiments.l2f_experiment_jobs",
        "job_key",
        "minos_l2f_claim",
        "register_validation_truth",
        "dataset_evaluation_identity",
    ):
        assert forbidden not in statements, forbidden
    # the ONE thing it says about the job table is that the plan carries none.
    assert statements.count("l2f_experiment_jobs") == 1
    assert "select count(*) from experiments.l2f_experiment_jobs where plan_id = :p" in statements

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not {name for name in called if "materialize" in name or "enqueue" in name}


def test_the_public_boundary_accepts_no_scientific_parameter() -> None:
    """Connections, a path and an artifact root. Nothing that could name a different campaign."""
    import inspect

    signature = inspect.signature(prepare_l2f2_validation_plan)
    assert list(signature.parameters) == [
        "target",
        "baseline",
        "finalist_freeze_path",
        "config_artifact_root",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_a_bootstrap_before_0024_could_never_have_returned(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The regression this proof exists to prevent.

    ``0021`` read the execution environment from
    ``experiments.l2f2_execution_authorities.execution_environment_hash``. ``0015`` added that
    identity to the two OUTCOME ledgers and to nothing else, and no migration ever added it to the
    authority table, so PL/pgSQL raised on the field the moment control reached it. Every test
    written against ``0021`` and ``0023`` exercised a refusal, and every refusal raises earlier —
    so the PHASE_D bootstrap had never once returned, and no test could tell.

    Here the same complete, honest campaign is prepared once, and the function body is then rolled
    back to ``0023``'s and rolled forward again. Under ``0024`` the bootstrap returns; under
    ``0023`` it raises on the missing field. This is the first test in the repository that reaches
    a PHASE_D bootstrap happy path at all.
    """
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        result = stores.prepare()
        assert result.bootstrap_environment_hash == _ENVIRONMENT

        with stores.target.connect() as conn:
            columns = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        " WHERE table_schema = 'experiments' "
                        "   AND table_name = 'l2f2_execution_authorities'"
                    )
                )
            ]
            assert "execution_environment_hash" not in columns

        url = str(stores.target.url.render_as_string(hide_password=False))
        alembic_downgrade(url, "0023_l2f2_phase_d_binding")
        with stores.target.connect() as conn, pytest.raises(Exception, match="has no field"):
            conn.execute(text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"))

        alembic_upgrade(url, _TARGET_REVISION)
        with stores.target.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()")
            ).one()
            assert (str(row[0]), str(row[1])) == (_PLAN_HASH, _ENVIRONMENT)


def test_the_source_transaction_is_read_only_in_the_database(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Not a promise in a docstring: the setting is observed, and a write is refused."""
    with (
        _stores(isolated_pg_base_url, tmp_path, authority) as stores,
        stores.source.connect() as conn,
    ):
        conn.execute(text("SELECT current_database()"))
        conn.execute(text("SELECT version_num FROM alembic_version"))
        conn.execute(text("SET TRANSACTION READ ONLY"))
        assert str(conn.execute(text("SHOW transaction_read_only")).scalar_one()) == "on"
        with pytest.raises(Exception, match="read-only transaction"):
            conn.execute(
                text("INSERT INTO catalog.artifacts (uri, sha256) VALUES ('mem://x', :s)"),
                {"s": "0" * 64},
            )


# --------------------------------------------------------------------------------------------
# THE live forgery — the thing 0024 exists for
# --------------------------------------------------------------------------------------------
def _forge_complete_campaign(
    conn: Any,
    authority: Any,
    *,
    ordered: tuple[str, ...],
    freeze_sha: str,
    closure_sha: str,
) -> None:
    """Build a COMPLETE, internally consistent PHASE_D graph that is not this campaign.

    Plan, ten validation members, four configurations, a PHASE_D authority and a binding that
    agrees with every one of them. Under ``0023`` this story passes: every identity was read out
    of the binding and compared only to the plan or to itself. It is the whole reason ``0024``
    puts the campaign's constants in the function body.
    """
    conn.execute(text("SET LOCAL ROLE minos_admin"))
    plan_hash = hashlib.sha256(b"forged-plan|" + "|".join(ordered).encode()).hexdigest()
    plan_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f_experiment_plans ("
            "  profile_snapshot_id, partition, snapshot_hash, split_manifest_hash, "
            "  registry_snapshot_hash, gatk_registry_hash, parameter_space_hash, "
            "  experiment_parameter_policy_hash, candidate_set_hash, train_member_count, "
            "  candidate_count, logical_job_count, plan_hash) "
            "SELECT ps.id, 'validation', ps.snapshot_hash, ps.split_manifest_hash, "
            "       ps.registry_snapshot_hash, :g, :p, :e, :c, 10, 4, 40, :h "
            "  FROM profiling.profile_snapshots ps ORDER BY ps.epoch LIMIT 1 RETURNING id"
        ),
        {
            "g": hashlib.sha256(b"gatk").hexdigest(),
            "p": authority.parameter_space_hash,
            "e": hashlib.sha256(b"policy").hexdigest(),
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
            "SELECT :plan, psm.profile_snapshot_id, NULL, psm.id, NULL, psm.bam_profile_id, "
            "       psm.dataset_registry_id, 'validation', psm.feature_values_hash, "
            "       row_number() OVER (ORDER BY psm.id) - 1, "
            "       row_number() OVER (ORDER BY psm.id) - 1 "
            "  FROM profiling.profile_snapshot_members psm"
        ),
        {"plan": plan_id},
    )
    for index, config_hash in enumerate(ordered):
        artifact_id = conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
                "VALUES (:u, :s, 'application/vnd.minos.l2f-config+json', 1, 'forged') "
                "RETURNING id"
            ),
            {"u": f"file:///forged/{config_hash}.json", "s": config_hash},
        ).scalar_one()
        payload_id = conn.execute(
            text(
                "INSERT INTO experiments.l2f_config_payloads "
                "  (config_hash, parameter_space_hash, schema_version, media_type, artifact_id) "
                "VALUES (:h, :p, 'l2f-config-payload-v1', "
                "        'application/vnd.minos.l2f-config+json', :a) RETURNING id"
            ),
            {"h": config_hash, "p": authority.parameter_space_hash, "a": artifact_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_plan_configs "
                "  (plan_id, config_payload_id, config_hash, parameter_space_hash, config_index) "
                "VALUES (:pl, :pid, :h, :p, :i)"
            ),
            {
                "pl": plan_id,
                "pid": payload_id,
                "h": config_hash,
                "p": authority.parameter_space_hash,
                "i": index,
            },
        )
    authority_id = conn.execute(
        text(
            "INSERT INTO experiments.l2f2_execution_authorities ("
            "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
            "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
            "  logical_job_count) "
            "VALUES (:proto, 'PHASE_D', :pl, :h, :s, :c, :p, 10, 4, 40) RETURNING id"
        ),
        {
            "proto": authority.baseline_protocol_hash,
            "pl": plan_id,
            "h": plan_hash,
            "s": authority.split_manifest_sha256,
            "c": authority.phase_c_candidate_set_hash,
            "p": authority.parameter_space_hash,
        },
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO experiments.l2f2_phase_d_binding ("
            "  baseline_protocol_hash, authority_id, plan_id, plan_hash, "
            "  finalist_freeze_sha256, phase_c_closure_sha256, parameter_space_hash, "
            "  execution_environment_hash, scoring_contract_hash, minos_subnet_sha, "
            "  split_manifest_sha256, seed_config_hash, ordered_config_hashes, "
            "  inherited_candidate_indices, member_count, candidate_count, logical_job_count) "
            "VALUES (:proto, :a, :pl, :h, :fz, :cl, :p, :env, :sc, :ms, :sm, :seed, "
            "        :cfgs, :idx, 10, 4, 40)"
        ),
        {
            "proto": authority.baseline_protocol_hash,
            "a": authority_id,
            "pl": plan_id,
            "h": plan_hash,
            "fz": freeze_sha,
            "cl": closure_sha,
            "p": authority.parameter_space_hash,
            "env": authority.execution_environment_hash,
            "sc": authority.scoring_contract_hash,
            "ms": authority.minos_subnet_sha,
            "sm": authority.split_manifest_sha256,
            "seed": ordered[3],
            "cfgs": list(ordered),
            "idx": [42, 25, 36, 0],
        },
    )


def _four_non_finalist_config_hashes(authority: Any) -> tuple[str, ...]:
    """Four REAL Phase-C candidate payloads that are not the frozen four."""
    frozen = set(authority.ordered_config_hashes)
    root = Path("/home/hr/bittensor/minos_l2f2_baseline/config_artifacts")
    others = [p.stem for p in sorted(root.glob("*.json")) if len(p.stem) == 64]
    picked = tuple(h for h in others if h not in frozen)[:4]
    assert len(picked) == 4, "the campaign root holds fewer than four non-finalist configurations"
    return picked


def test_a_self_consistent_forged_campaign_is_refused_by_the_anchor(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """The decisive one. Four real, wrong configurations; a binding and a plan that agree."""
    forged = _four_non_finalist_config_hashes(authority)
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with stores.target.connect() as conn, conn.begin():
            _forge_complete_campaign(
                conn,
                authority,
                ordered=forged,
                freeze_sha=hashlib.sha256(b"a-different-freeze").hexdigest(),
                closure_sha=hashlib.sha256(b"a-different-closure").hexdigest(),
            )
        with (
            stores.target.connect() as conn,
            pytest.raises(Exception, match="finalist freeze"),
        ):
            conn.execute(text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"))


def test_a_forgery_wearing_this_campaign_s_artifact_digests_is_still_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Citing the right artifacts while running four other configurations is the sharper lie."""
    forged = _four_non_finalist_config_hashes(authority)
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with stores.target.connect() as conn, conn.begin():
            _forge_complete_campaign(
                conn,
                authority,
                ordered=forged,
                freeze_sha=ACCEPTED_FINALIST_FREEZE_SHA256,
                closure_sha=ACCEPTED_PHASE_C_CLOSURE_SHA256,
            )
        with (
            stores.target.connect() as conn,
            pytest.raises(Exception, match="does not name the frozen four in frozen order"),
        ):
            conn.execute(text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"))


def test_the_forgery_that_0023_would_have_accepted(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any
) -> None:
    """Same graph, ``0023``'s function body: accepted there, refused here. That is the gap.

    Under ``0023`` the forged campaign gets past every comparison — and then dies on the missing
    ``execution_environment_hash`` field, which is ``0021``'s separate defect rather than a check.
    The distinction matters: ``0023`` refuses this graph for the wrong reason, and would have
    accepted it the moment that defect was fixed on its own.
    """
    forged = _four_non_finalist_config_hashes(authority)
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with stores.target.connect() as conn, conn.begin():
            _forge_complete_campaign(
                conn,
                authority,
                ordered=forged,
                freeze_sha=ACCEPTED_FINALIST_FREEZE_SHA256,
                closure_sha=ACCEPTED_PHASE_C_CLOSURE_SHA256,
            )
        url = str(stores.target.url.render_as_string(hide_password=False))
        alembic_downgrade(url, "0023_l2f2_phase_d_binding")
        with stores.target.connect() as conn, pytest.raises(Exception) as under_0023:
            conn.execute(text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"))
        # 0023 got all the way to the end: it never noticed the four were wrong.
        assert "has no field" in str(under_0023.value)
        assert "frozen four" not in str(under_0023.value)

        alembic_upgrade(url, _TARGET_REVISION)
        with stores.target.connect() as conn, pytest.raises(Exception) as under_0024:
            conn.execute(text("SELECT * FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"))
        assert "does not name the frozen four in frozen order" in str(under_0024.value)


def test_a_split_manifest_that_has_drifted_is_refused(
    isolated_pg_base_url: str, tmp_path: Path, authority: Any, monkeypatch: Any
) -> None:
    """The manifest is re-read and re-hashed at preparation time, not trusted from the authority.

    ``0024`` requires the binding to CARRY a split-manifest digest and cannot verify it: a database
    has no access to a file on a filesystem. This is the check that closes that loop, so it is
    proven against bytes that actually changed.
    """
    monkeypatch.setattr(
        "minos_engine.baseline.schedule.split_manifest_sha256",
        lambda root=None: "3" * 64,
    )
    with _stores(isolated_pg_base_url, tmp_path, authority) as stores:
        with pytest.raises(ValidationPrepareError, match="not the ten that were frozen"):
            stores.prepare()
        assert stores.counts()["plans"] == 0
