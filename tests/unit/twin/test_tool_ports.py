"""Tool runner ports — disabled fails closed, fakes are deterministic, no exec."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import UnavailableError
from minos_engine.intake.contracts import Region
from minos_engine.tools.gatk import (
    DisabledGatkRunner,
    FakeGatkRunner,
    build_gatk_argv,
)
from minos_engine.tools.happy import (
    DisabledHappyRunner,
    FakeHappyRunner,
)

REGION = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")
_H = "a" * 64


def test_build_gatk_argv_is_list_and_region_1based():
    argv = build_gatk_argv(
        effective_config={"min_pruning": 3},
        region=REGION,
        reference_path="/data/ref.fa",
        bam_path="/data/in put.bam",  # space handled safely (separate token)
        output_path="/out/o.vcf.gz",
    )
    assert argv[0] == "gatk"
    assert "/data/in put.bam" in argv  # a single argv token, not shell-split
    li = argv.index("-L")
    assert argv[li + 1] == "chr19:13000000-23000000"


def test_disabled_runners_fail_closed():
    with pytest.raises(UnavailableError):
        DisabledGatkRunner().run(("gatk",))
    with pytest.raises(UnavailableError):
        DisabledHappyRunner().run("req")


def test_fake_runners_do_not_execute():
    g = FakeGatkRunner(vcf_sha256=_H).run(("gatk",))
    assert g.executed is False and g.vcf_sha256 == _H
    hp = FakeHappyRunner({"snp": {"tp": 1, "fp": 0, "fn": 0}}).run("req")
    assert hp.executed is False and "snp" in hp.raw


def test_redacted_command_hides_signed_urls():
    from minos_engine.twin.contracts import ToolInvocation
    from minos_engine.twin.identities import ToolIdentity

    inv = ToolInvocation(
        tool=ToolIdentity(name="gatk", version="4.5.0.0"),
        argv=("gatk", "-O", "https://s3/out?X-Amz-Signature=secret"),
    )
    assert "secret" not in inv.redacted_command()
