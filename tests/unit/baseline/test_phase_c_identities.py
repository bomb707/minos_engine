"""The three Phase-C identity functions, at the level where every input can be moved one at a time.

A campaign hash is only worth having if it actually changes when the thing it describes changes.
The integration suite proves these are stable and re-derivable from a real ledger; here each
bound field is perturbed on its own, which is the direction a real defect takes — one input
quietly stops being covered and nobody notices, because the hash still looks like a hash.

The Phase-C candidate set carries TWO orderings, and the reason both are bound is the whole point
of :mod:`tests.unit.baseline.test_phase_c_candidate_index`: the promotion order is bookkeeping and
the inherited Phase-B index is the tie-break. Moving either must move the identity.
"""

from __future__ import annotations

from typing import Any

import pytest

from minos_engine.baseline.objective import BaselineObservation
from minos_engine.baseline.phase_b_completion import compute_phase_b_completion_hash
from minos_engine.baseline.phase_c import compute_phase_c_candidate_set_hash
from minos_engine.baseline.validation_finalists import compute_validation_finalist_set_hash


def _hash(seed: int) -> str:
    return f"{seed:064x}"


def _observation(index: int, **overrides: Any) -> BaselineObservation:
    base: dict[str, Any] = {
        "config_hash": _hash(index),
        "dataset_id": f"minos-chr18-{index:016x}",
        "chromosome": "chr18",
        "minos_score": 0.5 + index / 1000,
        "admitted": True,
        "failure_code": None,
        "gatk_runtime_ms": 60_000 + index,
    }
    return BaselineObservation.model_validate(base | overrides)


_COMPLETION_KWARGS = {
    "protocol_hash": _hash(0xA),
    "plan_hash": _hash(0xB),
    "candidate_set_hash": _hash(0xC),
    "parameter_space_hash": _hash(0xD),
    "execution_environment_hash": _hash(0xE),
}

_ORDERED = tuple(_hash(i) for i in range(10))
_INHERITED = dict(zip(_ORDERED, (42, 6, 31, 3, 0, 36, 25, 11, 5, 43), strict=True))
_SET_KWARGS = {
    "protocol_hash": _hash(0xA),
    "source_phase_b_plan_hash": _hash(0xB),
    "phase_b_completion_hash": _hash(0xC),
    "parameter_space_hash": _hash(0xD),
    "experiment_parameter_policy_hash": _hash(0xE),
    "seed_config_hash": _ORDERED[4],
}
_FINALISTS = _ORDERED[:4]
_FINALIST_KWARGS = {
    "protocol_hash": _hash(0xA),
    "phase_c_plan_hash": _hash(0xB),
    "phase_c_candidate_set_hash": _hash(0xC),
    "phase_c_result_hash": _hash(0xD),
    "ordered_config_hashes": _FINALISTS,
    "inherited_candidate_index": {h: _INHERITED[h] for h in _FINALISTS},
    "seed_config_hash": _FINALISTS[0],
}


# --------------------------------------------------------------------------- #
# the completed Phase-B screen
# --------------------------------------------------------------------------- #
def test_the_completion_hash_describes_the_screen_not_the_order_it_finished_in() -> None:
    """Two workers finishing the same 480 pairs in different orders describe the same screen."""
    observations = tuple(_observation(i) for i in range(6))
    shuffled = (
        observations[3],
        observations[0],
        observations[5],
        *observations[1:3],
        observations[4],
    )

    assert compute_phase_b_completion_hash(
        observations, **_COMPLETION_KWARGS
    ) == compute_phase_b_completion_hash(shuffled, **_COMPLETION_KWARGS)


@pytest.mark.parametrize(
    "override",
    [
        {"minos_score": 0.9},
        {"gatk_runtime_ms": 61_234},
        {"admitted": False, "minos_score": None, "failure_code": "GATK_NONZERO_EXIT"},
        {"chromosome": "chr22"},
        {"dataset_id": "minos-chr22-0000000000000099"},
    ],
    ids=["score", "runtime", "admission", "chromosome", "dataset"],
)
def test_every_bound_observation_field_moves_the_completion_hash(override: dict[str, Any]) -> None:
    """Runtime is bound because promotion ties break on it — an unbound tie-break is not frozen."""
    observations = tuple(_observation(i) for i in range(4))
    base = compute_phase_b_completion_hash(observations, **_COMPLETION_KWARGS)
    moved = (_observation(0, **override), *observations[1:])

    assert compute_phase_b_completion_hash(moved, **_COMPLETION_KWARGS) != base


@pytest.mark.parametrize("field", sorted(_COMPLETION_KWARGS))
def test_every_completion_context_field_moves_the_hash(field: str) -> None:
    observations = tuple(_observation(i) for i in range(3))
    base = compute_phase_b_completion_hash(observations, **_COMPLETION_KWARGS)

    assert (
        compute_phase_b_completion_hash(observations, **{**_COMPLETION_KWARGS, field: _hash(0xFF)})
        != base
    )


def test_a_dropped_observation_moves_the_completion_hash() -> None:
    """Absence is not zero: a screen missing a pair is a different screen, never a poorer one."""
    observations = tuple(_observation(i) for i in range(5))
    base = compute_phase_b_completion_hash(observations, **_COMPLETION_KWARGS)

    assert compute_phase_b_completion_hash(observations[:-1], **_COMPLETION_KWARGS) != base


# --------------------------------------------------------------------------- #
# the ten promoted candidates — both orderings
# --------------------------------------------------------------------------- #
def test_the_candidate_set_hash_is_stable_for_the_same_promotion() -> None:
    assert compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED, inherited_candidate_index=_INHERITED, **_SET_KWARGS
    ) == compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED,
        inherited_candidate_index=dict(reversed(list(_INHERITED.items()))),
        **_SET_KWARGS,
    ), "the inherited map is keyed, so its own insertion order must not matter"


def test_reordering_the_promotion_moves_the_candidate_set_hash() -> None:
    """Promotion order is bookkeeping, but it is bookkeeping this campaign committed to."""
    swapped = (_ORDERED[1], _ORDERED[0], *_ORDERED[2:])
    base = compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED, inherited_candidate_index=_INHERITED, **_SET_KWARGS
    )

    assert (
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=swapped, inherited_candidate_index=_INHERITED, **_SET_KWARGS
        )
        != base
    )


def test_moving_one_inherited_index_moves_the_candidate_set_hash() -> None:
    """THE tie-break number. If it were unbound, two different tie-breaks would share an identity."""
    moved = {**_INHERITED, _ORDERED[0]: _INHERITED[_ORDERED[0]] + 1}
    base = compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED, inherited_candidate_index=_INHERITED, **_SET_KWARGS
    )

    assert (
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=_ORDERED, inherited_candidate_index=moved, **_SET_KWARGS
        )
        != base
    )


def test_the_two_orderings_are_bound_independently() -> None:
    """Renumbering the inherited index to the promotion position must NOT be a silent no-op."""
    promotion_numbering = {h: i for i, h in enumerate(_ORDERED)}
    assert promotion_numbering != _INHERITED

    assert compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED, inherited_candidate_index=_INHERITED, **_SET_KWARGS
    ) != compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED,
        inherited_candidate_index=promotion_numbering,
        **_SET_KWARGS,
    )


@pytest.mark.parametrize("field", sorted(_SET_KWARGS))
def test_every_candidate_set_context_field_moves_the_hash(field: str) -> None:
    base = compute_phase_c_candidate_set_hash(
        ordered_config_hashes=_ORDERED, inherited_candidate_index=_INHERITED, **_SET_KWARGS
    )
    replacement = _ORDERED[9] if field == "seed_config_hash" else _hash(0xFF)

    assert (
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=_ORDERED,
            inherited_candidate_index=_INHERITED,
            **{**_SET_KWARGS, field: replacement},
        )
        != base
    )


def test_a_promoted_candidate_with_no_inherited_index_cannot_be_hashed() -> None:
    """Failing closed matters more here than a tidy error: a fabricated index is a wrong tie-break."""
    with pytest.raises(KeyError):
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=_ORDERED,
            inherited_candidate_index={h: _INHERITED[h] for h in _ORDERED[:9]},
            **_SET_KWARGS,
        )


# --------------------------------------------------------------------------- #
# the four finalists
# --------------------------------------------------------------------------- #
def test_the_finalist_hash_carries_the_inherited_index_forward() -> None:
    base = compute_validation_finalist_set_hash(**_FINALIST_KWARGS)
    moved = {
        **_FINALIST_KWARGS,
        "inherited_candidate_index": {
            **_FINALIST_KWARGS["inherited_candidate_index"],
            _FINALISTS[0]: 99,
        },
    }

    assert compute_validation_finalist_set_hash(**moved) != base


@pytest.mark.parametrize(
    "field",
    ["protocol_hash", "phase_c_plan_hash", "phase_c_candidate_set_hash", "phase_c_result_hash"],
)
def test_every_finalist_context_field_moves_the_hash(field: str) -> None:
    base = compute_validation_finalist_set_hash(**_FINALIST_KWARGS)

    assert compute_validation_finalist_set_hash(**{**_FINALIST_KWARGS, field: _hash(0xFF)}) != base


def test_reordering_the_finalists_moves_the_hash() -> None:
    reordered = (_FINALISTS[1], _FINALISTS[0], *_FINALISTS[2:])
    base = compute_validation_finalist_set_hash(**_FINALIST_KWARGS)

    assert (
        compute_validation_finalist_set_hash(
            **{**_FINALIST_KWARGS, "ordered_config_hashes": reordered}
        )
        != base
    )
