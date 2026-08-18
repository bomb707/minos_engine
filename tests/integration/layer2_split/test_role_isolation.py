"""L2-C SQL-level partition isolation, proven by connecting as each real role."""

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


_TRAIN = "SELECT count(*) FROM catalog.training_dataset_allocations"
_LOCKED = "SELECT count(*) FROM evaluation.locked_allocations"
_BASE_ALLOC = "SELECT count(*) FROM catalog.split_allocations"
_BASE_REG = "SELECT count(*) FROM catalog.dataset_registry"
_EVAL_ID = "SELECT count(*) FROM evaluation.dataset_evaluation_identity"


def test_trainer_reads_only_training_allocations(l2c_engine: Engine):
    allowed, n = _role_query(l2c_engine, "minos_trainer", _TRAIN)
    assert allowed and n == 50  # only the 50 training rows, via the owner view


def test_trainer_denied_locked_base_and_eval(l2c_engine: Engine):
    assert _role_query(l2c_engine, "minos_trainer", _LOCKED)[0] is False
    assert _role_query(l2c_engine, "minos_trainer", _BASE_ALLOC)[0] is False
    assert _role_query(l2c_engine, "minos_trainer", _BASE_REG)[0] is False
    assert _role_query(l2c_engine, "minos_trainer", _EVAL_ID)[0] is False


def test_live_has_no_split_allocation_access(l2c_engine: Engine):
    assert _role_query(l2c_engine, "minos_live", _TRAIN)[0] is False
    assert _role_query(l2c_engine, "minos_live", _LOCKED)[0] is False
    assert _role_query(l2c_engine, "minos_live", _BASE_ALLOC)[0] is False
    assert _role_query(l2c_engine, "minos_live", _BASE_REG)[0] is False


def test_runner_has_no_split_allocation_access(l2c_engine: Engine):
    assert _role_query(l2c_engine, "minos_runner", _TRAIN)[0] is False
    assert _role_query(l2c_engine, "minos_runner", _LOCKED)[0] is False
    assert _role_query(l2c_engine, "minos_runner", _BASE_ALLOC)[0] is False


def test_evaluator_reads_locked_and_eval_identity_only(l2c_engine: Engine):
    allowed, n = _role_query(l2c_engine, "minos_evaluator", _LOCKED)
    assert allowed and n == 25  # 10 validation + 15 test
    assert _role_query(l2c_engine, "minos_evaluator", _EVAL_ID)[0] is True
    # evaluator cannot reach the trainer view or the base registry table.
    assert _role_query(l2c_engine, "minos_evaluator", _TRAIN)[0] is False
    assert _role_query(l2c_engine, "minos_evaluator", _BASE_REG)[0] is False


def test_direct_base_table_access_does_not_bypass_views(l2c_engine: Engine):
    # No application role can reach the base allocation tables directly.
    for role in ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner"):
        assert _role_query(l2c_engine, role, _BASE_ALLOC)[0] is False
        assert _role_query(l2c_engine, role, _BASE_REG)[0] is False
