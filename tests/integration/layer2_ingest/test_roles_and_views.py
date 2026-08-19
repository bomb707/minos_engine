"""L2-D role isolation: sealed test denied, legacy revoked, minimized member views."""

from __future__ import annotations

from sqlalchemy import Engine, text


def _role_query(engine: Engine, role: str, sql: str) -> bool:
    with engine.connect() as c:
        try:
            c.execute(text(f"SET ROLE {role}"))
            c.execute(text(sql)).scalar()
            return True
        except Exception:  # noqa: BLE001
            return False


_TRAIN = "SELECT count(*) FROM profiling.training_profile_members"
_VAL = "SELECT count(*) FROM evaluation.validation_profile_members"
_SEALED = "SELECT count(*) FROM evaluation.sealed_test_profile_members"
_BASE = "SELECT count(*) FROM profiling.bam_profiles"
_LEGACY = "SELECT count(*) FROM profiling.profiles"

_APP_ROLES = ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner")


def test_trainer_reads_training_members_only(l2d_engine: Engine) -> None:
    assert _role_query(l2d_engine, "minos_trainer", _TRAIN) is True
    assert _role_query(l2d_engine, "minos_trainer", _VAL) is False
    assert _role_query(l2d_engine, "minos_trainer", _BASE) is False


def test_evaluator_reads_validation_members_only(l2d_engine: Engine) -> None:
    assert _role_query(l2d_engine, "minos_evaluator", _VAL) is True
    assert _role_query(l2d_engine, "minos_evaluator", _TRAIN) is False
    assert _role_query(l2d_engine, "minos_evaluator", _BASE) is False


def test_sealed_test_members_denied_to_all_roles(l2d_engine: Engine) -> None:
    for role in _APP_ROLES:
        assert _role_query(l2d_engine, role, _SEALED) is False, role


def test_legacy_profiles_reads_revoked(l2d_engine: Engine) -> None:
    # Owner ruling: legacy profiling.profiles is compatibility-only downstream.
    assert _role_query(l2d_engine, "minos_trainer", _LEGACY) is False
    assert _role_query(l2d_engine, "minos_live", _LEGACY) is False
    # The legacy runner write path keeps its read (roles.py untouched).
    assert _role_query(l2d_engine, "minos_runner", _LEGACY) is True


def test_no_app_role_reaches_l2d_base_tables(l2d_engine: Engine) -> None:
    for role in _APP_ROLES:
        for sql in (
            _BASE,
            "SELECT count(*) FROM profiling.profile_snapshots",
            "SELECT count(*) FROM profiling.profile_snapshot_members",
            "SELECT count(*) FROM profiling.profile_ingest_attempts",
        ):
            assert _role_query(l2d_engine, role, sql) is False, (role, sql)


def test_member_views_hide_sensitive_columns(l2d_engine: Engine) -> None:
    """No JSONB, artifact ids/URIs, file identity hashes, or region coordinates."""
    with l2d_engine.connect() as c:
        cols = set(
            c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'training_profile_members'"
                )
            ).scalars()
        )
    forbidden = {
        "profile_document",
        "profile_artifact_id",
        "windows_artifact_id",
        "uri",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "chromosome",
        "region_start0",
        "region_end0_exclusive",
        "identity_tuple_hash",
        "attestation_hash",
    }
    assert forbidden.isdisjoint(cols), forbidden & cols
    assert {"dataset_id", "epoch", "partition", "feature_values_hash"} <= cols
    assert "chromosome" not in cols  # L2-E owns the feature/join allowlist
