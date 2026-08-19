"""L2-C v2 epoch partition isolation + sealed-test denial, as each real role."""

from __future__ import annotations

from sqlalchemy import Engine, text


def _role_query(engine: Engine, role: str, sql: str) -> tuple[bool, int | None]:
    """(allowed, scalar) — run ``sql`` as ``role`` on a fresh connection."""
    with engine.connect() as c:
        try:
            c.execute(text(f"SET ROLE {role}"))
            val = c.execute(text(sql)).scalar()
            return True, (int(val) if val is not None else None)
        except Exception:
            return False, None


_TRAIN = "SELECT count(*) FROM catalog.training_epoch_allocations"
_VALIDATION = "SELECT count(*) FROM evaluation.validation_epoch_allocations"
_SEALED = "SELECT count(*) FROM evaluation.sealed_test_epoch_allocations"
_BASE_ALLOC = "SELECT count(*) FROM catalog.split_epoch_allocations"
_BASE_SNAP = "SELECT count(*) FROM catalog.split_snapshots"
_BASE_REG = "SELECT count(*) FROM catalog.dataset_registry"

_APP_ROLES = ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner")


def test_trainer_reads_only_training_epoch_allocations(l2c_v2_engine: Engine) -> None:
    allowed, n = _role_query(l2c_v2_engine, "minos_trainer", _TRAIN)
    assert allowed and n == 50  # only the 50 training rows, via the owner view


def test_trainer_denied_validation_sealed_and_base(l2c_v2_engine: Engine) -> None:
    assert _role_query(l2c_v2_engine, "minos_trainer", _VALIDATION)[0] is False
    assert _role_query(l2c_v2_engine, "minos_trainer", _SEALED)[0] is False
    assert _role_query(l2c_v2_engine, "minos_trainer", _BASE_ALLOC)[0] is False
    assert _role_query(l2c_v2_engine, "minos_trainer", _BASE_SNAP)[0] is False
    assert _role_query(l2c_v2_engine, "minos_trainer", _BASE_REG)[0] is False


def test_evaluator_reads_validation_only(l2c_v2_engine: Engine) -> None:
    """Evaluator sees validation (10 rows) but neither train nor the sealed test cohort."""
    allowed, n = _role_query(l2c_v2_engine, "minos_evaluator", _VALIDATION)
    assert allowed and n == 10
    assert _role_query(l2c_v2_engine, "minos_evaluator", _TRAIN)[0] is False
    assert _role_query(l2c_v2_engine, "minos_evaluator", _SEALED)[0] is False
    assert _role_query(l2c_v2_engine, "minos_evaluator", _BASE_ALLOC)[0] is False
    assert _role_query(l2c_v2_engine, "minos_evaluator", _BASE_REG)[0] is False


def test_sealed_test_denied_to_all_roles(l2c_v2_engine: Engine) -> None:
    """The sealed-test view carries NO grant: every application role is refused."""
    for role in _APP_ROLES:
        assert _role_query(l2c_v2_engine, role, _SEALED)[0] is False, role


def test_no_role_reads_test_partition_via_any_path(l2c_v2_engine: Engine) -> None:
    """No application role can count test-partition rows through views or base tables."""
    probes = (
        _SEALED,
        "SELECT count(*) FROM catalog.split_epoch_allocations WHERE partition = 'test'",
    )
    for role in _APP_ROLES:
        for sql in probes:
            assert _role_query(l2c_v2_engine, role, sql)[0] is False, (role, sql)


def test_live_and_runner_have_no_epoch_access(l2c_v2_engine: Engine) -> None:
    for role in ("minos_live", "minos_runner"):
        assert _role_query(l2c_v2_engine, role, _TRAIN)[0] is False
        assert _role_query(l2c_v2_engine, role, _VALIDATION)[0] is False
        assert _role_query(l2c_v2_engine, role, _SEALED)[0] is False
        assert _role_query(l2c_v2_engine, role, _BASE_ALLOC)[0] is False


def test_no_app_role_reaches_base_tables(l2c_v2_engine: Engine) -> None:
    for role in _APP_ROLES:
        assert _role_query(l2c_v2_engine, role, _BASE_ALLOC)[0] is False
        assert _role_query(l2c_v2_engine, role, _BASE_SNAP)[0] is False
        assert _role_query(l2c_v2_engine, role, _BASE_REG)[0] is False
