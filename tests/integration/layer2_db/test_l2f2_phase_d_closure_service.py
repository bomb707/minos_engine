"""The production closure entry, driven end to end against a scratch 0026 store.

Synthetic outcomes only: no real validation score reaches this file, and the utilities are chosen
before the closure runs so the expected winner is known in advance rather than read off the result.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.phase_d_observations import compute_phase_d_closure_hash
from minos_engine.baseline.phase_d_selection import ORDERED_FINALISTS, SEED_CONFIG_HASH
from minos_engine.evaluation.phase_d_closure_service import (
    ENV_FINALIST_FREEZE_PATH,
    PhaseDClosureAuthorityError,
    _derive_with_trust,
)
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.test_l2f_plan_store import _engine
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

_DB = "minos_l2f2_validation"
_HEAD = "0026_l2f2_phase_d_closure"
_CLOSER_ROLE = "ci_phase_d_closer_svc"

#: chosen BEFORE any closure runs. Finalist 2 is deliberately strongest; the seed is weakest.
_UTILITIES: dict[int, list[float]] = {
    0: [0.60, 0.61, 0.62, 0.63, 0.64, 0.65, 0.66, 0.67, 0.68, 0.69],
    1: [0.70, 0.71, 0.72, 0.73, 0.74, 0.75, 0.76, 0.77, 0.78, 0.79],
    2: [0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99],
    3: [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19],
}
_EXPECTED_WINNER = ORDERED_FINALISTS[2]


@pytest.fixture(scope="module")
def authority() -> Any:
    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


@pytest.fixture(autouse=True)
def _freeze_env(monkeypatch: Any) -> None:
    monkeypatch.setenv(ENV_FINALIST_FREEZE_PATH, str(FIXTURE_FREEZE_PATH))


class _Store:
    def __init__(self, admin: Any, service: Any) -> None:
        self.admin, self.service = admin, service

    def close(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "engine": self.service,
            "expected_database": _DB,
            "expected_revision": _HEAD,
        }
        kwargs.update(overrides)
        return _derive_with_trust(**kwargs)


@pytest.fixture
def store(isolated_pg_base_url: str, tmp_path: Path, authority: Any) -> Any:
    from tests.integration.layer2_db.l2f2_phase_d_closure_seed import seed_complete_matrix

    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        admin = _engine(url)
        service = None
        try:
            seed_complete_matrix(admin, authority, _UTILITIES, tmp_path)
            parsed = make_url(url)
            with admin.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_CLOSER_ROLE}"))
                conn.execute(
                    text(
                        f"CREATE ROLE {_CLOSER_ROLE} LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOBYPASSRLS INHERIT"
                    )
                )
                conn.execute(
                    text(f'GRANT CONNECT ON DATABASE "{parsed.database}" TO {_CLOSER_ROLE}')
                )
                conn.execute(text(f"GRANT minos_evaluator TO {_CLOSER_ROLE}"))
            service = create_engine(parsed.set(username=_CLOSER_ROLE, password=""))
            yield _Store(admin, service)
        finally:
            if service is not None:
                service.dispose()
            with contextlib.suppress(Exception), admin.connect() as conn, conn.begin():
                conn.execute(text(f"DROP ROLE IF EXISTS {_CLOSER_ROLE}"))
            admin.dispose()


# --------------------------------------------------------------------------------------------
# §32 — the positive
# --------------------------------------------------------------------------------------------
def test_the_closure_selects_the_predetermined_winner(store: Any) -> None:
    closure = store.close()
    assert closure.observation_count == 40
    assert closure.candidate_count == 4
    assert closure.member_count == 10
    assert closure.selected_config_hash == _EXPECTED_WINNER
    assert closure.ordered_ranking[0] == _EXPECTED_WINNER
    assert closure.seed_config_hash == SEED_CONFIG_HASH
    assert closure.seed_rank == 3, "the seed was weakest and must be ranked last"
    assert [c.rank for c in closure.candidates] == [0, 1, 2, 3]
    assert all(c.observed_count == 10 for c in closure.candidates)
    assert all(c.infrastructure_incident_count == 0 for c in closure.candidates)


def test_the_closure_hash_is_stable_across_replays(store: Any) -> None:
    assert compute_phase_d_closure_hash(store.close()) == compute_phase_d_closure_hash(
        store.close()
    )


def test_physical_row_order_cannot_change_the_closure(store: Any) -> None:
    """§32 — reshuffle the heap and re-close; the scientific identity must not move."""
    before = compute_phase_d_closure_hash(store.close())
    with store.admin.connect() as conn, conn.begin():
        conn.execute(text("SET ROLE minos_admin"))
        conn.execute(
            text("CLUSTER evaluation.l2f_evaluation_results USING pk_l2f_evaluation_results")
        )
    assert compute_phase_d_closure_hash(store.close()) == before


def test_the_closure_reads_only_the_narrow_surface(store: Any) -> None:
    """The closer principal must succeed while still barred from the experiments tables."""
    with store.service.connect() as conn:
        for table in ("experiments.l2f_experiment_plans", "experiments.l2f_execution_results"):
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            conn.rollback()
        assert (
            conn.execute(
                text("SELECT count(*) FROM evaluation.l2f_phase_d_closure_inputs")
            ).scalar_one()
            == 40
        )


# --------------------------------------------------------------------------------------------
# §29 — the boundary
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "match"),
    [
        pytest.param({"expected_database": "minos_l2f2_baseline"}, "refuses database", id="store"),
        pytest.param({"expected_revision": "0025_l2f2_phase_d_eval_auth"}, "revision", id="rev"),
    ],
)
def test_the_wrong_store_or_revision_is_refused(store: Any, override: dict, match: str) -> None:
    with pytest.raises(PhaseDClosureAuthorityError, match=match):
        store.close(**override)


def test_an_over_privileged_principal_is_refused(store: Any) -> None:
    with store.admin.connect() as conn:
        conn.execute(text("SET ROLE minos_admin"))
        with pytest.raises(Exception) as excinfo:
            from minos_engine.evaluation.phase_d_closure_service import (
                authorize_validation_closure_connection,
            )

            authorize_validation_closure_connection(conn)
    message = str(excinfo.value).lower()
    assert "membership" in message or "permission denied" in message, message


def test_the_public_entry_accepts_no_argument() -> None:
    import inspect

    from minos_engine.evaluation import phase_d_closure_service as mod

    assert list(inspect.signature(mod.derive_l2f2_phase_d_closure).parameters) == []
    source = inspect.getsource(mod.derive_l2f2_phase_d_closure)
    for nominable in (
        "observations",
        "scores",
        "weights",
        "candidate_index",
        "ranking",
        "winner",
        "partition",
        "plan_hash",
    ):
        assert nominable not in source, nominable


# --------------------------------------------------------------------------------------------
# §34 — the external evidence file is provenance, never authority
# --------------------------------------------------------------------------------------------
def test_tampering_with_an_external_matrix_json_cannot_move_the_ranking(
    store: Any, tmp_path: Path
) -> None:
    """The ranking is derived from ledgers through 0026. A JSON on disk has no vote."""
    before = compute_phase_d_closure_hash(store.close())
    forged = tmp_path / "phase_d_real_evaluation_complete_TAMPERED.json"
    forged.write_text(
        json.dumps(
            {
                "matrix": [
                    {
                        "config_hash": SEED_CONFIG_HASH,
                        "minos_score": 1.0,
                        "member_index": m,
                        "config_index": 3,
                    }
                    for m in range(10)
                ],
                "selected_config_hash": SEED_CONFIG_HASH,
            }
        ),
        encoding="utf-8",
    )
    after = store.close()
    assert compute_phase_d_closure_hash(after) == before
    assert after.selected_config_hash == _EXPECTED_WINNER
    assert after.selected_config_hash != SEED_CONFIG_HASH


# --------------------------------------------------------------------------------------------
# CORRECTIVE: the committed manifests are VERIFIED before an outcome row is read
#
# Recomputing a hash from source proves the source is self-consistent; it proves nothing about
# what was committed. A tampered rulebook has to stop closure BEFORE it consults any score.
# --------------------------------------------------------------------------------------------
class _RowSpy:
    """Records whether the closure surface was queried at all."""

    def __init__(self) -> None:
        self.reads = 0

    def __enter__(self) -> _RowSpy:
        from minos_engine.evaluation import phase_d_closure_service as mod

        self._real = mod._read_closure_rows
        spy = self

        def counting(conn: Any) -> Any:
            spy.reads += 1
            return spy._real(conn)

        mod._read_closure_rows = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        from minos_engine.evaluation import phase_d_closure_service as mod

        mod._read_closure_rows = self._real  # type: ignore[assignment]


def test_a_missing_selection_interpretation_stops_closure_before_any_row_is_read(
    store: Any, monkeypatch: Any
) -> None:
    from minos_engine.baseline import phase_d_selection as sel
    from minos_engine.evaluation import phase_d_closure_service as mod

    def absent(root: Any = None) -> Any:
        raise sel.PhaseDSelectionInterpretationError("interpretation manifest is missing")

    monkeypatch.setattr(sel, "load_committed_selection_interpretation", absent)
    monkeypatch.setattr(mod, "_read_closure_rows", mod._read_closure_rows)
    with _RowSpy() as spy, pytest.raises(PhaseDClosureAuthorityError, match="unusable"):
        store.close()
    assert spy.reads == 0, "a score row was read despite an unusable interpretation"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        pytest.param("final_selection_rule", "SELECT_SEED", "final selection rule", id="rule"),
        pytest.param("interpretation_status", "PRE_REGISTERED", "status", id="status"),
        pytest.param("baseline_protocol_hash", "a" * 64, "different baseline protocol", id="proto"),
        pytest.param("phase_d_plan_hash", "b" * 64, "different Phase-D plan", id="plan"),
    ],
)
def test_a_tampered_selection_interpretation_stops_closure_before_any_row_is_read(
    store: Any, monkeypatch: Any, field: str, value: str, match: str
) -> None:
    from minos_engine.baseline import phase_d_selection as sel

    real = sel.load_committed_selection_interpretation

    def tampered(root: Any = None) -> Any:
        document = real(root)
        document = {**document, "content": {**document["content"], field: value}}
        return document

    monkeypatch.setattr(sel, "load_committed_selection_interpretation", tampered)
    with _RowSpy() as spy, pytest.raises(PhaseDClosureAuthorityError, match=match):
        store.close()
    assert spy.reads == 0


def test_a_tampered_interpretation_hash_stops_closure_before_any_row_is_read(
    store: Any, monkeypatch: Any
) -> None:
    from minos_engine.baseline import phase_d_selection as sel

    real = sel.load_committed_selection_interpretation
    monkeypatch.setattr(
        sel,
        "load_committed_selection_interpretation",
        lambda root=None: {**real(root), "selection_interpretation_hash": "c" * 64},
    )
    with _RowSpy() as spy, pytest.raises(PhaseDClosureAuthorityError, match="not the accepted"):
        store.close()
    assert spy.reads == 0


def test_a_missing_or_tampered_protocol_manifest_stops_closure_before_any_row_is_read(
    store: Any, monkeypatch: Any
) -> None:
    from minos_engine.baseline import protocol as proto

    monkeypatch.setattr(
        proto,
        "load_committed_protocol",
        lambda root=None: (_ for _ in ()).throw(
            proto.BaselineProtocolError("committed protocol manifest is missing")
        ),
    )
    with _RowSpy() as spy, pytest.raises(PhaseDClosureAuthorityError, match="unusable"):
        store.close()
    assert spy.reads == 0

    monkeypatch.setattr(
        proto, "load_committed_protocol", lambda root=None: {"protocol_hash": "d" * 64}
    )
    with _RowSpy() as spy, pytest.raises(PhaseDClosureAuthorityError, match="committed protocol"):
        store.close()
    assert spy.reads == 0


def test_the_closure_binds_the_verified_committed_protocol_hash(store: Any) -> None:
    closure = store.close()
    assert closure.baseline_protocol_hash == (
        "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
    )
    assert closure.selection_interpretation_hash == (
        "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd"
    )


def test_every_authority_is_established_before_the_rows_are_read() -> None:
    """§11 — the ordering, asserted on the source rather than inferred from behaviour."""
    import inspect

    from minos_engine.evaluation import phase_d_closure_service as mod

    source = inspect.getsource(mod._derive_with_trust)
    order = [
        source.index("_authorize_closure_connection("),
        source.index("_frozen_phase_d_authority("),
        source.index("_verify_committed_authorities("),
        source.index("_read_closure_rows("),
        source.index("return build_phase_d_closure("),
    ]
    assert order == sorted(order), order
