"""Group F — reference-context known-answer tests."""

from __future__ import annotations

from minos_engine.layer1.reference_profile import (
    profile_reference_sequence,
    unavailable_reference_context,
)


def test_gc_and_entropy_uniform():
    r = profile_reference_sequence("ACGT" * 25)  # equal composition
    assert abs(r.gc_fraction - 0.5) < 1e-12
    assert abs(r.entropy_bits - 2.0) < 1e-9  # 4 equal symbols -> 2 bits
    assert r.n_fraction == 0.0


def test_gc_all_gc():
    r = profile_reference_sequence("GCGCGCGC")
    assert r.gc_fraction == 1.0


def test_n_fraction():
    r = profile_reference_sequence("ACGTNNNN")
    assert r.n_fraction == 0.5
    # ACGT contributes to ACGT composition; N excluded
    assert abs(r.gc_fraction - 0.5) < 1e-12


def test_homopolymer_detection():
    r = profile_reference_sequence("AAAAAACGT", homopolymer_min_run=4)
    # 6 A's form one run >= 4
    assert r.homopolymer_length_histogram == {"6": 1}
    assert r.homopolymer_base_fraction == 6 / 9


def test_dinucleotide_repeat():
    r = profile_reference_sequence("ATATATATGGGG", dinucleotide_min_run=3)
    # ATATATAT = 4 AT units >= 3 -> 8 bases covered
    assert r.dinucleotide_repeat_fraction == 8 / 12


def test_empty_sequence_unavailable():
    r = profile_reference_sequence("")
    assert r.reference_available is True
    assert r.gc_fraction == 0.0


def test_unavailable_reference():
    r = unavailable_reference_context(reference_available=False)
    assert r.reference_available is False
    assert r.homopolymer_length_histogram == {}
