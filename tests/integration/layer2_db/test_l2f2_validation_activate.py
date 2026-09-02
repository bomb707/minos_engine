"""Phase-D activation on scratch databases: ten truth identities, forty jobs, and a full stop.

Preparation proved what the campaign IS. This proves the two things that have to be true before a
single GATK hour may be spent on it: which truth the evaluator is authorized to use, and which
forty jobs are authorized to exist. Then it stops — every job unclaimed, in the canonical initial
state, with nothing executed, evaluated, scored or ranked.

Two absences are deliberate and are asserted, not merely intended:

* **the real validation truth is never opened.** The truth bytes here are synthetic. Registering
  an identity requires bytes to hash, not the answer key, and Phase D has not been authorized to
  look at the answer key. TEST is not reachable at all — the accepted registrar reads a
  VALIDATION-only projection and re-derives the partition from the split itself;
* **no job leaves PENDING.** Nothing in this module claims, starts, executes or evaluates.

Everything runs on scratch databases and a scratch dataset root the fixture creates and drops.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.storage.l2f2_validation_activate import (
    INITIAL_JOB_STATUS,
    ValidationActivationError,
    _activate_truth_with_trust,
    _materialize_with_trust,
    activate_l2f2_validation_truth,
    materialize_l2f2_validation_jobs,
)
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
    _prepare_with_trust,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f2_validation_seed import (
    seed_source_configs,
    seed_target_upstream,
    seed_truth_bundles,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH, forgery_config_hashes

_SOURCE_DB = "minos_l2f2_baseline"
_TARGET_DB = "minos_l2f2_validation"
_SOURCE_REVISION = "0020_l2f2_phase_c_execution"
_TARGET_REVISION = "0024_l2f2_phase_d_anchor"

_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"

#: scratch state lives beneath the MINOS physical root when the machine has one, and beneath a
#: temporary directory when it does not — always outside the repository. The root is discovered
#: rather than assumed: hard-coding the operator's path made this suite unrunnable anywhere else.


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
def scratch_root(tmp_path_factory: Any) -> Any:
    """A scratch filesystem root, proven to lie beneath whichever MINOS root is in force."""
    import shutil

    from tests.minos_scratch import minos_scratch_root, prune_scratch_parent

    scratch, effective_root = minos_scratch_root(
        "phase_d_", fallback=tmp_path_factory.mktemp("minos_scratch")
    )
    print(f"scratch requested: {scratch}")
    print(f"scratch realpath : {scratch.resolve()}")
    print(f"effective root   : {effective_root}")
    assert scratch.resolve().is_relative_to(effective_root.resolve())
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        prune_scratch_parent(scratch)


class _Campaign:
    """A scratch source at 0020 and a scratch target at 0024, prepared and ready to activate."""

    def __init__(self, source: Any, target: Any, root: Path, authority: Any) -> None:
        self.source = source
        self.target = target
        self.root = root
        self.authority = authority
        self.dataset_root = root / "validation_truth"

    def prepare(self) -> Any:
        return _prepare_with_trust(
            target=self.target,
            baseline=self.source,
            finalist_freeze_path=FIXTURE_FREEZE_PATH,
            config_artifact_root=self.root / "target_configs",
            expected_database=_TARGET_DB,
            expected_revision=_TARGET_REVISION,
        )

    def register_truth(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "target": self.target,
            "finalist_freeze_path": FIXTURE_FREEZE_PATH,
            "dataset_root": self.dataset_root,
            "expected_database": _TARGET_DB,
            "expected_revision": _TARGET_REVISION,
        }
        kwargs.update(overrides)
        return _activate_truth_with_trust(**kwargs)

    def materialize(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "target": self.target,
            "finalist_freeze_path": FIXTURE_FREEZE_PATH,
            "expected_database": _TARGET_DB,
            "expected_revision": _TARGET_REVISION,
        }
        kwargs.update(overrides)
        return _materialize_with_trust(**kwargs)

    def counts(self) -> dict[str, int]:
        with self.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))

            def n(sql: str) -> int:
                return int(conn.execute(text(sql)).scalar_one())

            return {
                "jobs": n("SELECT count(*) FROM experiments.l2f_experiment_jobs"),
                "pending": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs "
                    f" WHERE status = '{INITIAL_JOB_STATUS}'"
                ),
                "claimed": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs "
                    " WHERE status = 'CLAIMED' OR claimed_by IS NOT NULL"
                ),
                "running": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs  WHERE status = 'RUNNING'"
                ),
                "succeeded": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs "
                    " WHERE status = 'SUCCEEDED'"
                ),
                "failed": n(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs "
                    " WHERE status = 'FAILED' OR status = 'CANCELLED'"
                ),
                "truth": n(
                    "SELECT count(*) FROM evaluation.dataset_evaluation_identity "
                    " WHERE truth_vcf_sha256 IS NOT NULL"
                ),
                "exec_results": n("SELECT count(*) FROM experiments.l2f_execution_results"),
                "exec_failures": n("SELECT count(*) FROM experiments.l2f_execution_failures"),
                "evaluations": n("SELECT count(*) FROM evaluation.l2f_evaluation_results"),
            }


def _campaign(
    base_url: str,
    scratch_root: Path,
    authority: Any,
    *,
    target_db: str = _TARGET_DB,
    target_revision: str = _TARGET_REVISION,
    truth: bool = True,
    prepare: bool = True,
    seed_kwargs: dict[str, Any] | None = None,
    truth_kwargs: dict[str, Any] | None = None,
) -> Any:
    @contextlib.contextmanager
    def _ctx() -> Any:
        import tempfile

        with (
            scratch_database(base_url, _SOURCE_DB) as source_url,
            scratch_database(base_url, target_db) as target_url,
        ):
            alembic_upgrade(source_url, _SOURCE_REVISION)
            alembic_upgrade(target_url, target_revision)
            source = _engine(source_url)
            target = _engine(target_url)
            root = Path(tempfile.mkdtemp(prefix="run_", dir=scratch_root))
            campaign = _Campaign(source, target, root, authority)
            try:
                with source.connect() as conn, conn.begin():
                    seed_source_configs(
                        conn,
                        authority.ordered_config_hashes,
                        authority.parameter_space_hash,
                        config_root=root / "source_configs",
                    )
                with target.connect() as conn, conn.begin():
                    seed_target_upstream(
                        conn,
                        authority.schedule.members,
                        split_manifest_hash=authority.split_manifest_sha256,
                        **(seed_kwargs or {}),
                    )
                seed_truth_bundles(
                    campaign.dataset_root,
                    authority.schedule.members,
                    **(truth_kwargs or {}),
                )
                if prepare:
                    campaign.prepare()
                if truth:
                    campaign.register_truth()
                yield campaign
            finally:
                source.dispose()
                target.dispose()

    return _ctx()


# --------------------------------------------------------------------------------------------
# the scratch filesystem invariant
# --------------------------------------------------------------------------------------------
def test_every_scratch_root_lies_under_the_minos_root_in_force(scratch_root: Path) -> None:
    """Under the canonical MINOS root where one exists, and never inside the repository."""
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert scratch_root.is_absolute()
    assert not scratch_root.is_symlink()
    assert not scratch_root.resolve().is_relative_to(Path(__file__).resolve().parents[3])
    if CANONICAL_MINOS_ROOT.is_dir():
        assert scratch_root.resolve().is_relative_to(CANONICAL_MINOS_ROOT.resolve())


# --------------------------------------------------------------------------------------------
# 1. truth activation
# --------------------------------------------------------------------------------------------
def test_activation_registers_exactly_ten_validation_truth_identities(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        assert campaign.counts()["truth"] == 0
        result = campaign.register_truth()

        assert result.plan_hash == _PLAN_HASH
        assert result.member_count == 10
        assert result.truth_identity_count == 10
        assert result.created_truth_count == 10
        assert campaign.counts()["truth"] == 10
        # identity summaries only: one plan hash, three counts. No digest, no path, no payload.
        import dataclasses

        fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
        assert set(fields) == {
            "plan_hash",
            "member_count",
            "truth_identity_count",
            "created_truth_count",
        }
        assert [v for v in fields.values() if isinstance(v, str)] == [_PLAN_HASH]


def test_truth_registration_replay_creates_nothing(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        before = campaign.counts()
        second = campaign.register_truth()
        assert second.created_truth_count == 0
        assert second.truth_identity_count == 10
        assert campaign.counts() == before


def test_only_the_ten_frozen_validation_members_carry_truth(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """No TRAIN row, no TEST row — and the ten are exactly the frozen schedule."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            rows = (
                conn.execute(
                    text(
                        "SELECT dr.dataset_id, sa.partition "
                        "  FROM evaluation.dataset_evaluation_identity d "
                        "  JOIN catalog.dataset_registry dr ON dr.id = d.dataset_registry_id "
                        "  JOIN catalog.split_allocations sa ON sa.dataset_registry_id = dr.id"
                    )
                )
                .mappings()
                .all()
            )
        assert {str(r["dataset_id"]) for r in rows} == {
            m.dataset_id for m in authority.schedule.members
        }
        assert {str(r["partition"]) for r in rows} == {"validation"}


@pytest.mark.parametrize("partition", ["train", "test"])
def test_a_non_validation_member_cannot_reach_truth_readiness(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, partition: str
) -> None:
    """A member reallocated away from validation stops the campaign before truth is touched.

    The allocation is flipped AFTER preparation, because preparation itself refuses a
    non-validation member — which is the point: there is no ordering in which TRAIN or TEST
    reaches Phase D.
    """
    victim = authority.schedule.members[0].dataset_id
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(text("ALTER TABLE catalog.split_allocations DISABLE TRIGGER USER"))
            conn.execute(
                text(
                    "UPDATE catalog.split_allocations SET partition = :part "
                    " WHERE dataset_registry_id = ("
                    "   SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d)"
                ),
                {"part": partition, "d": victim},
            )
            conn.execute(text("ALTER TABLE catalog.split_allocations ENABLE TRIGGER USER"))

        with pytest.raises(ValidationActivationError, match="allocated to"):
            campaign.register_truth()
        assert campaign.counts()["truth"] == 0
        with pytest.raises(ValidationActivationError, match="allocated to"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


@pytest.mark.parametrize("partition", ["train", "test"])
def test_the_registrar_projection_cannot_enumerate_train_or_test(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, partition: str
) -> None:
    """Structural, not polite: the accepted registration target view has no TRAIN or TEST row.

    TRAIN and TEST are excluded because the interface the registrar reads cannot name them, so
    there is no argument a caller could pass to reach one.
    """
    victim = authority.schedule.members[1].dataset_id
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(text("ALTER TABLE catalog.split_allocations DISABLE TRIGGER USER"))
            conn.execute(
                text(
                    "UPDATE catalog.split_allocations SET partition = :part "
                    " WHERE dataset_registry_id = ("
                    "   SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d)"
                ),
                {"part": partition, "d": victim},
            )
            conn.execute(text("ALTER TABLE catalog.split_allocations ENABLE TRIGGER USER"))
        # read as the connecting principal: 0022 grants SELECT on this projection to
        # minos_evaluator alone, so switching to the control plane would lose the privilege.
        with campaign.target.connect() as conn:
            visible = {
                str(r[0])
                for r in conn.execute(
                    text(
                        "SELECT dataset_id FROM "
                        "evaluation.l2f_validation_truth_registration_targets"
                    )
                )
            }
        assert victim not in visible
        assert len(visible) == 9


def test_a_missing_truth_bundle_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    victim = authority.schedule.members[3].round_id
    with _campaign(
        isolated_pg_base_url,
        scratch_root,
        authority,
        truth=False,
        truth_kwargs={"omit_round": victim},
    ) as campaign:
        with pytest.raises(Exception, match="round directory"):
            campaign.register_truth()
        assert campaign.counts()["truth"] == 0


def test_a_missing_truth_file_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    victim = authority.schedule.members[5].round_id
    with _campaign(
        isolated_pg_base_url,
        scratch_root,
        authority,
        truth=False,
        truth_kwargs={"omit_file": (victim, "truth.vcf.gz.tbi")},
    ) as campaign:
        with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal; none may pass
            campaign.register_truth()
        assert campaign.counts()["truth"] == 0


def test_tampered_truth_bytes_conflict_with_the_registered_identity(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Identity is content. Re-registering changed bytes is a contradiction, not an update."""
    victim = authority.schedule.members[7].round_id
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        before = campaign.counts()
        target = campaign.dataset_root / f"round_{victim}" / "truth.vcf.gz"
        target.write_bytes(target.read_bytes() + b"tampered\n")
        with pytest.raises(Exception, match="already registered with different bytes"):
            campaign.register_truth()
        assert campaign.counts() == before


def test_an_incomplete_truth_identity_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """A row with a NULL digest is registered but not usable; readiness must say so.

    ``dataset_evaluation_identity`` is append-only, so the incomplete row is INSERTed rather than
    edited into existence — which is also the only way it could arise for real.
    """
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            for index, member in enumerate(authority.schedule.members):
                conn.execute(
                    text(
                        "INSERT INTO evaluation.dataset_evaluation_identity "
                        "  (dataset_registry_id, truth_vcf_sha256, truth_tbi_sha256, "
                        "   mutations_vcf_sha256, mutations_tbi_sha256) "
                        "SELECT dr.id, :tv, :tt, :mv, :mt FROM catalog.dataset_registry dr "
                        " WHERE dr.dataset_id = :d"
                    ),
                    {
                        "d": member.dataset_id,
                        "tv": f"{index:064x}",
                        # exactly one member's index digest is absent
                        "tt": None if index == 4 else f"{index:064x}",
                        "mv": f"{index:064x}",
                        "mt": f"{index:064x}",
                    },
                )
        assert campaign.counts()["truth"] == 10
        with pytest.raises(ValidationActivationError, match="incomplete"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


# --------------------------------------------------------------------------------------------
# 2. the exact forty jobs
# --------------------------------------------------------------------------------------------
def _job_rows(campaign: Any) -> list[dict[str, Any]]:
    with campaign.target.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT j.job_key, j.status, j.claimed_by, j.claimed_at, "
                    "       pm.member_index, pc.config_index, pc.config_hash, pm.partition "
                    "  FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                    "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                    " ORDER BY pm.member_index, pc.config_index"
                )
            ).mappings()
        ]


def test_materialization_creates_exactly_the_forty_pair_product(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        assert campaign.counts()["jobs"] == 0
        result = campaign.materialize()

        assert result.plan_hash == _PLAN_HASH
        assert (result.member_count, result.config_count) == (10, 4)
        assert result.logical_job_count == 40
        assert result.created_jobs == 40
        assert result.existing_jobs == 0
        assert result.pending_jobs == 40

        rows = _job_rows(campaign)
        assert len(rows) == 40
        pairs = [(int(r["member_index"]), int(r["config_index"])) for r in rows]
        assert pairs == [(m, c) for m in range(10) for c in range(4)]
        assert len(set(pairs)) == 40
        assert {str(r["config_hash"]) for r in rows} == set(authority.ordered_config_hashes)
        assert {str(r["partition"]) for r in rows} == {"validation"}


def test_every_job_key_is_the_frozen_deterministic_identity(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Not a fresh UUID scheme: the same domain-separated formula every earlier phase used."""
    from minos_engine.experiments.plan import compute_job_key

    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        with campaign.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            members = {
                int(r["member_index"]): dict(r)
                for r in conn.execute(
                    text(
                        "SELECT pm.member_index, dr.dataset_id, bp.profile_id, bp.content_hash, "
                        "       pm.feature_values_hash "
                        "  FROM experiments.l2f_experiment_plan_members pm "
                        "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                        "  JOIN profiling.bam_profiles bp ON bp.id = pm.bam_profile_id"
                    )
                ).mappings()
            }
        expected = {
            compute_job_key(
                plan_hash=_PLAN_HASH,
                member_index=index,
                dataset_id=str(m["dataset_id"]),
                profile_id=str(m["profile_id"]),
                content_hash=str(m["content_hash"]),
                feature_values_hash=str(m["feature_values_hash"]),
                config_index=config_index,
                config_hash=config_hash,
            )
            for index, m in members.items()
            for config_index, config_hash in enumerate(authority.ordered_config_hashes)
        }
        assert {str(r["job_key"]) for r in _job_rows(campaign)} == expected
        assert len(expected) == 40


def test_activation_ends_with_every_job_untouched(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The whole point of stopping here: authorized, and not one of them begun."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        counts = campaign.counts()
        assert counts["jobs"] == 40
        assert counts["pending"] == 40
        assert counts["claimed"] == 0
        assert counts["running"] == 0
        assert counts["succeeded"] == 0
        assert counts["failed"] == 0
        assert counts["exec_results"] == 0
        assert counts["exec_failures"] == 0
        assert counts["evaluations"] == 0

        rows = _job_rows(campaign)
        assert {str(r["status"]) for r in rows} == {INITIAL_JOB_STATUS}
        assert all(r["claimed_by"] is None and r["claimed_at"] is None for r in rows)

        # no result artifact of any kind was published into the target.
        with campaign.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            provenance = [
                str(r[0])
                for r in conn.execute(text("SELECT DISTINCT provenance FROM catalog.artifacts"))
            ]
        assert all("result" not in (p or "").lower() for p in provenance)


def test_materialization_replay_creates_nothing(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        first = campaign.materialize()
        keys_before = {str(r["job_key"]) for r in _job_rows(campaign)}

        second = campaign.materialize()
        assert second.created_jobs == 0
        assert second.existing_jobs == 40
        assert second.pending_jobs == 40
        assert second.plan_hash == first.plan_hash
        assert second.plan_id == first.plan_id
        assert {str(r["job_key"]) for r in _job_rows(campaign)} == keys_before
        assert campaign.counts()["jobs"] == 40


# --------------------------------------------------------------------------------------------
# atomicity
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fail_after", [0, 1, 19, 39])
def test_a_mid_materialization_failure_leaves_no_partial_campaign(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, fail_after: int
) -> None:
    """No 1/40, no 19/40, no 39/40. The product is written whole or not at all."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with pytest.raises(ValidationActivationError, match="deliberate mid-materialization"):
            campaign.materialize(fail_after=fail_after)
        assert campaign.counts()["jobs"] == 0

        # and the campaign is still activatable afterwards: the rollback left nothing behind.
        assert campaign.materialize().created_jobs == 40
        assert campaign.counts()["jobs"] == 40


# --------------------------------------------------------------------------------------------
# provisioning negatives
# --------------------------------------------------------------------------------------------
def test_a_wrong_target_database_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with pytest.raises(ValidationActivationError, match="validation target connection"):
            campaign.materialize(expected_database="some_other_store")
        assert campaign.counts()["jobs"] == 0


def test_a_wrong_target_revision_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with pytest.raises(ValidationActivationError, match="expected '0025"):
            campaign.materialize(expected_revision="0025_not_a_revision")
        assert campaign.counts()["jobs"] == 0


def test_the_public_entries_pin_the_store_and_accept_no_science(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Operational arguments only — no plan, member, config, count or digest may cross."""
    import inspect

    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    assert VALIDATION_DATABASE_NAME == _TARGET_DB
    assert VALIDATION_REVISION == _TARGET_REVISION

    assert list(inspect.signature(materialize_l2f2_validation_jobs).parameters) == [
        "target",
        "finalist_freeze_path",
    ]
    assert list(inspect.signature(activate_l2f2_validation_truth).parameters) == [
        "target",
        "finalist_freeze_path",
        "dataset_root",
    ]
    for entry in (materialize_l2f2_validation_jobs, activate_l2f2_validation_truth):
        assert all(
            p.kind is inspect.Parameter.KEYWORD_ONLY
            for p in inspect.signature(entry).parameters.values()
        )

    with (
        _campaign(
            isolated_pg_base_url,
            scratch_root,
            authority,
            target_db="minos_l2f2_wrong_target",
            prepare=False,
            truth=False,
        ) as campaign,
        pytest.raises(ValidationActivationError, match="validation target connection"),
    ):
        materialize_l2f2_validation_jobs(
            target=campaign.target, finalist_freeze_path=FIXTURE_FREEZE_PATH
        )


# --------------------------------------------------------------------------------------------
# authority / graph negatives
# --------------------------------------------------------------------------------------------
def test_an_unprepared_store_cannot_materialize(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """No PHASE_D authority, no binding, no plan: the bootstrap refuses before any job exists."""
    with _campaign(
        isolated_pg_base_url, scratch_root, authority, prepare=False, truth=False
    ) as campaign:
        with pytest.raises(Exception, match="PHASE_D"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_a_campaign_without_truth_cannot_materialize(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """A prepared campaign is not an activated one. Ten truth identities gate the forty jobs."""
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        assert campaign.counts()["truth"] == 0
        with pytest.raises(ValidationActivationError, match="have no truth identity"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_one_missing_truth_identity_blocks_all_forty(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Nine of ten authorizes zero jobs, not thirty-six."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text("ALTER TABLE evaluation.dataset_evaluation_identity DISABLE TRIGGER USER")
            )
            conn.execute(
                text(
                    "DELETE FROM evaluation.dataset_evaluation_identity "
                    " WHERE dataset_registry_id = ("
                    "   SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d)"
                ),
                {"d": authority.schedule.members[6].dataset_id},
            )
            conn.execute(
                text("ALTER TABLE evaluation.dataset_evaluation_identity ENABLE TRIGGER USER")
            )
        assert campaign.counts()["truth"] == 9
        with pytest.raises(ValidationActivationError, match="1 of 10 validation members"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_a_conflicting_existing_job_graph_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """A job whose key is not in the frozen product is a different experiment, not a stray row."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text(
                    "INSERT INTO experiments.l2f_experiment_jobs "
                    "  (plan_id, plan_member_id, plan_config_id, job_key, status) "
                    "SELECT pm.plan_id, pm.id, pc.id, :k, 'PENDING' "
                    "  FROM experiments.l2f_experiment_plan_members pm "
                    "  JOIN experiments.l2f_experiment_plan_configs pc "
                    "    ON pc.plan_id = pm.plan_id AND pc.config_index = 0 "
                    " WHERE pm.member_index = 0"
                ),
                {"k": "c" * 64},
            )
        with pytest.raises(ValidationActivationError, match="not in the frozen logical product"):
            campaign.materialize()
        # exactly the intruder remains: not one job of ours was written alongside it.
        assert campaign.counts()["jobs"] == 1


def test_an_already_executing_campaign_is_not_extended(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Replay onto a campaign that has begun running refuses rather than topping it up."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text(
                    "UPDATE experiments.l2f_experiment_jobs "
                    "   SET status = 'CLAIMED', claimed_by = 'worker-x', claimed_at = now() "
                    " WHERE job_key = (SELECT job_key FROM experiments.l2f_experiment_jobs "
                    "                   ORDER BY job_key LIMIT 1)"
                )
            )
        with pytest.raises(ValidationActivationError, match="already begun executing"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 40


@pytest.mark.parametrize(
    ("victim", "expected"),
    [
        ("config_index", "config_index inventory"),
        ("config_hash", "frozen four in frozen order"),
    ],
)
def test_a_plan_whose_configs_drift_cannot_materialize(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, victim: str, expected: str
) -> None:
    """The source-side re-check, independent of the bootstrap's own refusal."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text("ALTER TABLE experiments.l2f_experiment_plan_configs DISABLE TRIGGER USER")
            )
            if victim == "config_index":
                conn.execute(
                    text(
                        "UPDATE experiments.l2f_experiment_plan_configs SET config_index = 7 "
                        " WHERE config_index = 3"
                    )
                )
            else:
                # a REAL non-finalist configuration, registered here so the drifted row is
                # otherwise entirely valid: the only thing wrong with it is that it is not one
                # of the frozen four.
                intruder = forgery_config_hashes()[0]
                artifact_id = conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts "
                        "  (uri, sha256, media_type, size_bytes, provenance) "
                        "VALUES (:u, :s, 'application/vnd.minos.l2f-config+json', 1, 'drift') "
                        "RETURNING id"
                    ),
                    {"u": f"file:///drift/{intruder}.json", "s": intruder},
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
                        "UPDATE experiments.l2f_experiment_plan_configs "
                        "   SET config_hash = :h, config_payload_id = :pid "
                        " WHERE config_index = 0"
                    ),
                    {"h": intruder, "pid": payload_id},
                )
            conn.execute(
                text("ALTER TABLE experiments.l2f_experiment_plan_configs ENABLE TRIGGER USER")
            )
        with pytest.raises(Exception, match=expected):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_a_non_validation_member_cannot_reach_materialization(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """TEST must remain structurally absent from the job graph, by every route."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(text("ALTER TABLE catalog.split_allocations DISABLE TRIGGER USER"))
            conn.execute(
                text(
                    "UPDATE catalog.split_allocations SET partition = 'test' "
                    " WHERE dataset_registry_id = ("
                    "   SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d)"
                ),
                {"d": authority.schedule.members[2].dataset_id},
            )
            conn.execute(text("ALTER TABLE catalog.split_allocations ENABLE TRIGGER USER"))
        with pytest.raises(ValidationActivationError, match="allocated to 'test'|VALIDATION"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_the_runner_gains_no_write_authority_over_jobs(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Jobs now exist. The runner still may not write them directly — only through 0007."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        with campaign.target.connect() as conn:
            for role in ("minos_runner", "minos_evaluator", "minos_trainer", "minos_live"):
                for privilege in ("INSERT", "UPDATE", "DELETE", "SELECT"):
                    granted = conn.execute(
                        text(
                            "SELECT has_table_privilege(:r, 'experiments.l2f_experiment_jobs', :p)"
                        ),
                        {"r": role, "p": privilege},
                    ).scalar_one()
                    assert granted is False, f"{role} has {privilege}"
            owner = conn.execute(
                text(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class "
                    " WHERE oid = 'experiments.l2f_experiment_jobs'::regclass"
                )
            ).scalar_one()
            assert str(owner) == "minos_admin"


def test_no_gatk_scoring_or_evaluation_module_is_reachable_from_activation() -> None:
    """A source-level guarantee: activation has no path to execution at all."""
    import ast

    source = Path("src/minos_engine/storage/l2f2_validation_activate.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in ("gatk", "happy", "scorer", "evaluation.orchestrator", "minos_subnet"):
        assert not any(forbidden in module for module in imported), forbidden

    statements = " ".join(
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    for forbidden in (
        "minos_l2f_claim",
        "update experiments.l2f_experiment_jobs",
        "l2f_execution_results",
        "l2f_evaluation_results",
        "'running'",
        "'claimed'",
        "'succeeded'",
    ):
        assert forbidden not in statements, forbidden


def test_the_pending_graph_matches_what_the_runner_would_claim(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Inspection only: the forty jobs are exactly the set the accepted claimer would select.

    The claim function is NOT called — no Phase-D job may be claimed in this task. What is proven
    is that the graph activation leaves behind is the graph the runner's own predicate resolves,
    so a future authorized execution finds forty claimable jobs and not thirty-nine.
    """
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        with campaign.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            definition = str(
                conn.execute(
                    text(
                        "SELECT pg_get_functiondef(to_regprocedure("
                        "  'experiments.minos_l2f_claim_next_job(text,text)'))"
                    )
                ).scalar_one()
            )
            collapsed = " ".join(definition.split())
            assert "'PENDING'" in collapsed
            assert "p_plan_hash" in collapsed

            # the same predicate the claimer uses, evaluated read-only.
            claimable = conn.execute(
                text(
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    " WHERE p.plan_hash = :h AND j.status = 'PENDING' "
                    "   AND j.claimed_by IS NULL"
                ),
                {"h": _PLAN_HASH},
            ).scalar_one()
            assert int(claimable) == 40
            # and nothing has actually been claimed by anyone.
            assert campaign.counts()["claimed"] == 0


# --------------------------------------------------------------------------------------------
# GAP A — a partial durable job graph is refused, never topped up
# --------------------------------------------------------------------------------------------
def _delete_jobs_down_to(campaign: Any, keep: int) -> None:
    """Privileged TEST corruption seam: leave exactly ``keep`` legitimate jobs behind.

    ``l2f_experiment_jobs`` forbids DELETE by trigger, which is why a partial graph cannot arise
    through any accepted interface — and why staging one for the negative needs an explicit seam
    rather than a production code path.
    """
    with campaign.target.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(text("ALTER TABLE experiments.l2f_experiment_jobs DISABLE TRIGGER USER"))
        conn.execute(
            text(
                "DELETE FROM experiments.l2f_experiment_jobs WHERE job_key IN ("
                "  SELECT job_key FROM experiments.l2f_experiment_jobs "
                "   ORDER BY job_key OFFSET :keep)"
            ),
            {"keep": keep},
        )
        conn.execute(text("ALTER TABLE experiments.l2f_experiment_jobs ENABLE TRIGGER USER"))


@pytest.mark.parametrize("survivors", [1, 19, 39])
def test_a_partial_job_graph_is_refused_and_never_completed(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, survivors: int
) -> None:
    """Every surviving job is legitimate. The count alone is the refusal.

    A partial durable graph is evidence that some previous activation was interrupted or ran
    outside this contract. Filling in the gap would convert that evidence into a campaign that
    looks like it was authorized in one piece, so activation refuses and leaves it for an
    operator to look at.
    """
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        _delete_jobs_down_to(campaign, survivors)
        assert campaign.counts()["jobs"] == survivors
        surviving_keys = {str(r["job_key"]) for r in _job_rows(campaign)}

        with pytest.raises(
            ValidationActivationError, match=f"already holds {survivors} of 40 jobs"
        ):
            campaign.materialize()

        # not one job was inserted to close the gap, and none was removed either.
        assert campaign.counts()["jobs"] == survivors
        assert {str(r["job_key"]) for r in _job_rows(campaign)} == surviving_keys


def test_the_replay_contract_is_empty_or_complete(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The whole contract in one place: 0 -> 40, 40 -> 40 created 0, anything between -> refuse."""
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        assert campaign.counts()["jobs"] == 0
        assert campaign.materialize().created_jobs == 40

        second = campaign.materialize()
        assert (second.created_jobs, second.existing_jobs, second.pending_jobs) == (0, 40, 40)

        _delete_jobs_down_to(campaign, 25)
        with pytest.raises(ValidationActivationError, match="already holds 25 of 40 jobs"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 25


def test_the_empty_or_complete_rule_is_stated_not_prefixed() -> None:
    """The old contract was documented as a PREFIX. It is not one, and the source says so."""
    source = Path("src/minos_engine/storage/l2f2_validation_activate.py").read_text(
        encoding="utf-8"
    )
    assert "_require_empty_or_complete" in source
    assert "_reject_conflicting_graph" not in source
    assert "PREFIX" not in source
    assert "EMPTY or COMPLETE" in source


# --------------------------------------------------------------------------------------------
# GAP B — the persisted ten must BE the frozen ten
# --------------------------------------------------------------------------------------------
def _corrupt_registry(campaign: Any, dataset_id: str, column: str, value: str) -> None:
    """Privileged TEST seam for a defect the accepted interfaces cannot construct."""
    with campaign.target.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(text("ALTER TABLE catalog.dataset_registry DISABLE TRIGGER USER"))
        conn.execute(
            text(
                f"UPDATE catalog.dataset_registry SET {column} = :v "  # noqa: S608
                " WHERE dataset_id = :d"
            ),
            {"v": value, "d": dataset_id},
        )
        conn.execute(text("ALTER TABLE catalog.dataset_registry ENABLE TRIGGER USER"))


@pytest.mark.parametrize(
    ("column", "value", "field"),
    [
        ("round_id", "ffffffffffffffff", "round_id"),
        ("chromosome", "chr18", "chromosome"),
        ("identity_tuple_hash", "0" * 64, "identity_tuple_hash"),
    ],
)
def test_a_member_whose_frozen_identity_drifts_is_refused(
    isolated_pg_base_url: str,
    scratch_root: Path,
    authority: Any,
    column: str,
    value: str,
    field: str,
) -> None:
    """Ten validation members at the right indices is not the same as the right ten."""
    victim = authority.schedule.members[4]
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        campaign.materialize()
        _delete_jobs_down_to(campaign, 0)
        _corrupt_registry(campaign, victim.dataset_id, column, value)

        with pytest.raises(
            ValidationActivationError, match=f"is not the frozen validation.*|{field}"
        ):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_a_wrong_dataset_at_a_member_index_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """A validation round that is simply not in the frozen ten."""
    victim = authority.schedule.members[8]
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        _corrupt_registry(campaign, victim.dataset_id, "dataset_id", "minos-chr22-000000000000dead")
        with pytest.raises(ValidationActivationError, match="is not the frozen validation member"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_the_right_ten_in_the_wrong_order_are_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Every frozen identity is present; two are at each other's index. That is a different plan.

    This is the case a count-and-set check cannot see, and the reason the comparison is
    positional rather than a set membership test.
    """
    a, b = authority.schedule.members[0], authority.schedule.members[1]
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        with campaign.target.connect() as conn, conn.begin():
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text("ALTER TABLE experiments.l2f_experiment_plan_members DISABLE TRIGGER USER")
            )
            for index, dataset_id in ((99, a.dataset_id), (0, b.dataset_id), (1, a.dataset_id)):
                conn.execute(
                    text(
                        "UPDATE experiments.l2f_experiment_plan_members pm "
                        "   SET member_index = :i FROM catalog.dataset_registry dr "
                        " WHERE dr.id = pm.dataset_registry_id AND dr.dataset_id = :d"
                    ),
                    {"i": index, "d": dataset_id},
                )
            conn.execute(
                text("ALTER TABLE experiments.l2f_experiment_plan_members ENABLE TRIGGER USER")
            )
        with pytest.raises(ValidationActivationError, match="is not the frozen validation member"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_the_persisted_plan_hash_is_not_accepted_as_proof_of_its_rows(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The plan hash still matches; the rows do not. The rows win.

    A hash stored in a column is a value, and a value cannot vouch for the rows that were
    supposed to have produced it.
    """
    victim = authority.schedule.members[2]
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        _corrupt_registry(campaign, victim.dataset_id, "round_id", "aaaaaaaaaaaaaaaa")
        with campaign.target.connect() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            stored = str(
                conn.execute(
                    text("SELECT plan_hash FROM experiments.l2f_experiment_plans")
                ).scalar_one()
            )
        assert stored == _PLAN_HASH == authority.plan_hash
        with pytest.raises(ValidationActivationError, match="is not the frozen validation member"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


# --------------------------------------------------------------------------------------------
# GAP C — truth is the answer key, and it stays shut until the campaign is proven
# --------------------------------------------------------------------------------------------
class _TruthFileSpy:
    """Records every truth file the process opens, by any route the registrar could take."""

    def __init__(self, dataset_root: Path) -> None:
        self.root = dataset_root.resolve()
        self.opened: list[str] = []

    def __enter__(self) -> _TruthFileSpy:
        import builtins
        import os

        self._open, self._os_open = builtins.open, os.open
        self._read_bytes, self._read_text = Path.read_bytes, Path.read_text
        self._path_open = Path.open
        spy = self

        def record(target: Any) -> None:
            with contextlib.suppress(Exception):
                resolved = Path(target).resolve()
                if resolved.is_relative_to(spy.root):
                    spy.opened.append(str(resolved))

        def builtins_open(file: Any, *a: Any, **k: Any) -> Any:
            record(file)
            return spy._open(file, *a, **k)

        def os_open(path: Any, *a: Any, **k: Any) -> Any:
            record(path)
            return spy._os_open(path, *a, **k)

        def path_read_bytes(self_: Path) -> bytes:
            record(self_)
            return spy._read_bytes(self_)

        def path_read_text(self_: Path, *a: Any, **k: Any) -> str:
            record(self_)
            return spy._read_text(self_, *a, **k)

        def path_open(self_: Path, *a: Any, **k: Any) -> Any:
            record(self_)
            return spy._path_open(self_, *a, **k)

        builtins.open, os.open = builtins_open, os_open
        Path.read_bytes, Path.read_text, Path.open = (  # type: ignore[method-assign]
            path_read_bytes,
            path_read_text,
            path_open,
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        import builtins
        import os

        builtins.open, os.open = self._open, self._os_open
        Path.read_bytes, Path.read_text, Path.open = (  # type: ignore[method-assign]
            self._read_bytes,
            self._read_text,
            self._path_open,
        )


def test_the_spy_sees_truth_files_when_they_are_legitimately_read(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The instrument must be shown to work before its silence can mean anything."""
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with _TruthFileSpy(campaign.dataset_root) as spy:
            campaign.register_truth()
        assert len(spy.opened) >= 40  # four files per round, ten rounds
        assert campaign.counts()["truth"] == 10


def test_an_unprepared_target_is_refused_before_any_truth_byte_is_opened(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """The decisive one. Right database, right revision, real upstream, truth files sitting there.

    No PHASE_D plan, authority or binding, so the campaign is not this campaign — and the answer
    key must still be unread when the refusal happens. Provisioning pointing at a directory is
    not a reason to open it.
    """
    with _campaign(
        isolated_pg_base_url, scratch_root, authority, prepare=False, truth=False
    ) as campaign:
        assert (campaign.dataset_root / f"round_{authority.schedule.members[0].round_id}").is_dir()

        with (
            _TruthFileSpy(campaign.dataset_root) as spy,
            pytest.raises(Exception, match="PHASE_D"),
        ):
            activate_l2f2_validation_truth(
                target=campaign.target,
                finalist_freeze_path=FIXTURE_FREEZE_PATH,
                dataset_root=campaign.dataset_root,
            )
        assert spy.opened == []
        assert campaign.counts()["truth"] == 0


def test_a_malformed_prepared_campaign_is_refused_before_any_truth_byte_is_opened(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    """Prepared, but not this campaign: one member's frozen identity has drifted."""
    victim = authority.schedule.members[5]
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        _corrupt_registry(campaign, victim.dataset_id, "round_id", "bbbbbbbbbbbbbbbb")

        with (
            _TruthFileSpy(campaign.dataset_root) as spy,
            pytest.raises(ValidationActivationError, match="is not the frozen validation"),
        ):
            campaign.register_truth()
        assert spy.opened == []
        assert campaign.counts()["truth"] == 0


@pytest.mark.parametrize("expected_database", ["minos_l2f2_baseline", "some_other_store"])
def test_a_wrong_store_is_refused_before_any_truth_byte_is_opened(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, expected_database: str
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with (
            _TruthFileSpy(campaign.dataset_root) as spy,
            pytest.raises(ValidationActivationError, match="validation target connection"),
        ):
            campaign.register_truth(expected_database=expected_database)
        assert spy.opened == []
        assert campaign.counts()["truth"] == 0


def test_a_wrong_revision_is_refused_before_any_truth_byte_is_opened(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any
) -> None:
    with _campaign(isolated_pg_base_url, scratch_root, authority, truth=False) as campaign:
        with (
            _TruthFileSpy(campaign.dataset_root) as spy,
            pytest.raises(ValidationActivationError, match="expected '0025"),
        ):
            campaign.register_truth(expected_revision="0025_not_a_revision")
        assert spy.opened == []
        assert campaign.counts()["truth"] == 0


# --------------------------------------------------------------------------------------------
# truth readiness means THIS exact frozen round
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("round_id", "cccccccccccccccc"),
        ("chromosome", "chr22"),
        ("identity_tuple_hash", "1" * 64),
    ],
)
def test_truth_registered_against_a_different_round_is_refused(
    isolated_pg_base_url: str, scratch_root: Path, authority: Any, column: str, value: str
) -> None:
    """A familiar dataset_id is not enough: readiness is checked on the full frozen identity.

    Truth is registered legitimately, and only afterwards does the registry row stop being the
    frozen round — which is exactly the shape of a store whose truth was attached to the right
    name and the wrong data.
    """
    victim = authority.schedule.members[3]
    with _campaign(isolated_pg_base_url, scratch_root, authority) as campaign:
        assert campaign.counts()["truth"] == 10
        _corrupt_registry(campaign, victim.dataset_id, column, value)

        with pytest.raises(ValidationActivationError, match="is not the frozen validation|round"):
            campaign.materialize()
        assert campaign.counts()["jobs"] == 0


def test_truth_readiness_reads_the_full_frozen_identity_not_the_dataset_id_alone() -> None:
    """A source-level statement of the rule the negatives above exercise."""
    source = Path("src/minos_engine/storage/l2f2_validation_activate.py").read_text(
        encoding="utf-8"
    )
    readiness = source[source.index("def _truth_readiness") :]
    readiness = readiness[: readiness.index("\ndef ")]
    for field in ("round_id", "chromosome", "identity_tuple_hash"):
        assert field in readiness, field
