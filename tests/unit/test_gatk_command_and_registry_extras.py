"""GATK CLI-flag metadata and upstream provenance / reference registry extras."""

from __future__ import annotations

import pytest

from minos_engine.callers.gatk.command import CLI_FLAGS, render_flag_args
from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.errors import UnavailableError
from minos_engine.common.versions import IdentityStatus
from minos_engine.intake.reference_registry import ReferenceEntry, ReferenceRegistry
from minos_engine.protocol.upstream_adapter import extract_provenance


def test_cli_flags_cover_all_registry_params():
    assert set(CLI_FLAGS) == set(REGISTRY.names())
    assert CLI_FLAGS["min_mapping_quality_score"] == "--minimum-mapping-quality"


def test_render_flag_args_effective_config():
    effective = canonicalize_config({"min_pruning": 3}).effective_config
    args = render_flag_args(effective)
    assert "--min-pruning" in args
    assert args[args.index("--min-pruning") + 1] == "3"


def test_render_flag_args_bool_rendering():
    args = render_flag_args({"dont_use_soft_clipped_bases": True})
    assert args == ["--dont-use-soft-clipped-bases", "true"]


def test_extract_provenance_available_and_unavailable():
    ids = extract_provenance({"scorer_hash": "abc", "minos_upstream_commit": "  "})
    assert ids["scorer_hash"].status is IdentityStatus.AVAILABLE
    assert ids["scorer_hash"].value == "abc"
    assert ids["minos_upstream_commit"].status is IdentityStatus.UNAVAILABLE
    assert ids["minos_upstream_commit"].value is None
    # A key not supplied at all is UNAVAILABLE, never fabricated.
    assert ids["reference_sha256"].status is IdentityStatus.UNAVAILABLE


def test_reference_registry_resolve_and_fail_closed():
    reg = ReferenceRegistry()
    reg.register(ReferenceEntry(contig="chr19", fasta_sha256="a" * 64))
    assert reg.resolve("chr19").build == "GRCh38"
    assert reg.contigs() == ("chr19",)
    with pytest.raises(UnavailableError):
        reg.resolve("chr21")
