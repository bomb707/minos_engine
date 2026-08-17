"""Layer 1 CLI end-to-end (validate / profile / qualify-real / gate)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.cli.main import main


def _ds(tmp_path: Path):
    return build_dataset(tmp_path, simple_reads(3000, n_pairs=40), contig="chr1", contig_len=3000)


def test_cli_validate_ok(tmp_path, capsys):
    ds = _ds(tmp_path)
    code = main(
        [
            "layer1",
            "validate",
            "--bam",
            str(ds.bam),
            "--reference",
            str(ds.reference),
            "--region",
            "chr1:1-3000",
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["region"]["contig"] == "chr1"


def test_cli_validate_missing_bam(tmp_path, capsys):
    ds = _ds(tmp_path)
    ds.bam.unlink()
    code = main(
        [
            "layer1",
            "validate",
            "--bam",
            str(ds.bam),
            "--reference",
            str(ds.reference),
            "--region",
            "chr1:1-3000",
            "--json",
        ]
    )
    assert code == 2  # hard input failure


def test_cli_profile_writes_artifacts(tmp_path, capsys):
    ds = _ds(tmp_path)
    out = tmp_path / "out"
    code = main(
        [
            "layer1",
            "profile",
            "--bam",
            str(ds.bam),
            "--reference",
            str(ds.reference),
            "--region",
            "chr1:1-3000",
            "--output-dir",
            str(out),
            "--skip-prerequisite",
            "--json",
        ]
    )
    assert code == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "COMPLETE"
    assert (out / "bam-profile-v1.json").exists()
    assert (out / "window-profile-v1.parquet").exists()
    assert (out / "profile-manifest-v1.json").exists()


def test_cli_qualify_real_report(tmp_path, capsys):
    ds = _ds(tmp_path)
    report = tmp_path / "report.json"
    code = main(
        [
            "layer1",
            "qualify-real",
            "--bam",
            str(ds.bam),
            "--reference",
            str(ds.reference),
            "--region",
            "chr1:1-3000",
            "--dataset-id",
            "synthetic-ci",
            "--skip-prerequisite",
            "--output",
            str(report),
            "--json",
        ]
    )
    assert code == 0
    assert report.exists()
    payload = json.loads(report.read_text())
    assert payload["real_bam_qualified"] is True
    assert payload["repeat_run_fingerprint_equal"] is True
    # sanitized: no absolute paths in the committed report
    assert "/tmp" not in json.dumps(payload)


def test_cli_top_level_profile_alias(tmp_path):
    ds = _ds(tmp_path)
    out = tmp_path / "out2"
    code = main(
        [
            "profile",
            "--bam",
            str(ds.bam),
            "--reference",
            str(ds.reference),
            "--region",
            "chr1:1-3000",
            "--output-dir",
            str(out),
            "--skip-prerequisite",
        ]
    )
    assert code == 0
    assert (out / "bam-profile-v1.json").exists()
