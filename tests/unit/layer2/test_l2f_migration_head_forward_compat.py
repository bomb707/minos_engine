"""L2-F2 stage-transition seam — HARNESS migration identity must be forward-compatible.

HARNESS-READY proves the migration state of the L2-F1 source it qualified. It must keep proving
that the accepted migrations exist, are byte-identical and end at ``0008_l2f_execution_results``,
while a LATER additive migration (``0009`` and beyond) advances the repository's current head
without retroactively invalidating frozen evidence.

Every control here is Tier 1: pure filesystem fixtures, no database, no GATK, no network.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from minos_engine.common.errors import MinosEngineError
from minos_engine.qualification.l2f_accepted_identities import (
    recompute_alembic_head,
    recompute_harness_alembic_head,
    recompute_migration_sha256,
)
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_ALEMBIC_HEAD,
    ACCEPTED_MIGRATION_SHAS,
)

_FUTURE = '''"""Temporary offline probe: metadata only, creates no schema objects."""

revision: str = "9999_future_probe"
down_revision: str | None = "0008_l2f_execution_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal copy of the real migration lineage that fixtures may safely mutate."""
    root = tmp_path / "repo"
    (root / "migrations" / "versions").mkdir(parents=True)
    src = _repo_root() / "migrations" / "versions"
    for path in src.glob("[0-9]*.py"):
        shutil.copy2(path, root / "migrations" / "versions" / path.name)
    return root


def _accepted_path(repo: Path, needle: str) -> Path:
    relative = next(r for r in ACCEPTED_MIGRATION_SHAS if needle in r)
    return repo / relative


# --------------------------------------------------------------------------- #
# CONTROL 1 — the accepted subgraph resolves to the historical head
# --------------------------------------------------------------------------- #
def test_accepted_subgraph_head_is_the_historical_harness_head(repo: Path) -> None:
    assert recompute_harness_alembic_head(repo) == ACCEPTED_ALEMBIC_HEAD
    assert recompute_harness_alembic_head(repo) == "0008_l2f_execution_results"


def test_accepted_subgraph_head_matches_the_real_repository_today() -> None:
    """Today, with no 0009 present, both resolvers agree. That is expected, not the invariant."""
    root = _repo_root()
    assert recompute_harness_alembic_head(root) == "0008_l2f_execution_results"
    assert recompute_alembic_head(root) == "0008_l2f_execution_results"


# --------------------------------------------------------------------------- #
# CONTROL 2 — THE point: a future additive migration moves only the global head
# --------------------------------------------------------------------------- #
def test_a_future_additive_migration_does_not_move_the_harness_head(repo: Path) -> None:
    (repo / "migrations" / "versions" / "9999_future_probe.py").write_text(_FUTURE, "utf-8")

    # the repository's CURRENT head legitimately advances ...
    assert recompute_alembic_head(repo) == "9999_future_probe"
    # ... while historical HARNESS evidence stays anchored to its own stage.
    assert recompute_harness_alembic_head(repo) == "0008_l2f_execution_results"


# --------------------------------------------------------------------------- #
# CONTROL 3 — a future migration is not accepted history
# --------------------------------------------------------------------------- #
def test_a_future_additive_migration_never_enters_the_accepted_hash_set(repo: Path) -> None:
    (repo / "migrations" / "versions" / "9999_future_probe.py").write_text(_FUTURE, "utf-8")

    recomputed = recompute_migration_sha256(repo)
    assert set(recomputed) == set(ACCEPTED_MIGRATION_SHAS)
    assert not any("9999" in path for path in recomputed)
    assert recomputed == ACCEPTED_MIGRATION_SHAS


# --------------------------------------------------------------------------- #
# CONTROL 4 — a missing accepted migration fails closed, both ways
# --------------------------------------------------------------------------- #
def test_a_missing_accepted_migration_fails_closed(repo: Path) -> None:
    _accepted_path(repo, "0008_l2f_execution_results").unlink()

    with pytest.raises(MinosEngineError):
        recompute_harness_alembic_head(repo)
    with pytest.raises(MinosEngineError):
        recompute_migration_sha256(repo)


# --------------------------------------------------------------------------- #
# CONTROL 5 — a broken accepted lineage fails closed
# --------------------------------------------------------------------------- #
def test_a_broken_accepted_lineage_is_refused(repo: Path) -> None:
    """Re-parenting 0008 away from 0007 leaves two accepted entry points, which is not a lineage."""
    path = _accepted_path(repo, "0008_l2f_execution_results")
    text = path.read_text("utf-8").replace('"0007_l2f_job_claiming"', '"0001_l2b_initial"', 1)
    path.write_text(text, "utf-8")

    with pytest.raises(MinosEngineError, match="ONE lineage"):
        recompute_harness_alembic_head(repo)


# --------------------------------------------------------------------------- #
# CONTROL 6 — multiple accepted heads are refused
# --------------------------------------------------------------------------- #
def test_multiple_accepted_heads_are_refused(repo: Path) -> None:
    """Detach 0008 so nothing descends 0007: 0007 and 0008 both become heads."""
    path = _accepted_path(repo, "0008_l2f_execution_results")
    text = path.read_text("utf-8").replace(
        'down_revision: str | None = "0007_l2f_job_claiming"',
        "down_revision: str | None = None",
        1,
    )
    assert text != path.read_text("utf-8"), "fixture did not detach 0008"
    path.write_text(text, "utf-8")

    with pytest.raises(MinosEngineError):
        recompute_harness_alembic_head(repo)


# --------------------------------------------------------------------------- #
# CONTROL 7 — byte binding is NOT weakened by stage-scoping the head
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "needle", ["0006_l2f_experiment_plan", "0007_l2f_job_claiming", "0008_l2f_execution_results"]
)
def test_tampering_with_accepted_migration_bytes_is_still_rejected(repo: Path, needle: str) -> None:
    from minos_engine.qualification.l2f_accepted_identities import (
        AcceptedIdentityError,
        recompute_accepted_identities,
        verify_accepted_identities,
    )

    path = _accepted_path(repo, needle)
    path.write_text(path.read_text("utf-8") + "\n# tampered\n", "utf-8")

    recomputed = recompute_migration_sha256(repo)
    relative = next(r for r in ACCEPTED_MIGRATION_SHAS if needle in r)
    assert recomputed[relative] != ACCEPTED_MIGRATION_SHAS[relative]

    # and the accepted-identity closure refuses the mismatched migration identity
    accepted = recompute_accepted_identities()
    forged = accepted.model_copy(update={"migration_sha256": recomputed})
    with pytest.raises(AcceptedIdentityError):
        verify_accepted_identities(forged)


# --------------------------------------------------------------------------- #
# CONTROL 8 — the committed evidence still verifies, and the check name is frozen
# --------------------------------------------------------------------------- #
def test_committed_harness_evidence_still_verifies() -> None:
    from minos_engine.qualification.l2f_harness_ready_runner import (
        verify_committed_harness_ready_gate,
    )

    root = _repo_root()
    if not (root / "gates" / "harness-ready.json").exists():  # pragma: no cover - evidence-gated
        pytest.skip("HARNESS evidence is not committed in this tree")
    result = verify_committed_harness_ready_gate(
        base_dir=root,
        gate_path=root / "gates" / "harness-ready.json",
        qualification_path=root / "reports" / "layer2" / "harness-ready-result.json",
    )
    assert result["ok"] is True, result["reasons"]


def test_the_committed_forty_check_inventory_is_not_renamed() -> None:
    """``alembic_head_is_0008`` is a HISTORICAL observation and stays in the frozen inventory."""
    from minos_engine.gates.required_checks import required_checks_for

    checks = required_checks_for("HARNESS-READY")
    assert len(checks) == 40
    assert "alembic_head_is_0008" in checks
