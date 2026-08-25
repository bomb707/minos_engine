"""A synthetic pinned MINOS_SUBNET checkout for tests that must not run real hap.py.

The production oracle refuses to score against anything it cannot prove is the pinned authority,
which is exactly the property we want — and it means a test cannot simply hand it a directory.
So this builds a real (tiny) git checkout laid out like upstream, and an authority whose digests
match it. Everything the orchestrator does around the scorer is then exercised for real: root
verification, the isolated subprocess, the sandbox copies, the bounded failure vocabulary.

The synthetic scorer performs no science. It reads a control file from the checkout and returns
whatever the test asked for — metrics, ``None``, an exception or a hang — so every branch of the
production path can be driven deterministically without Docker, hap.py or biological data.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCORING_PY = """\
import json
import pathlib
import time

BCFTOOLS_DOCKER_IMAGE = "fake/bcftools@sha256:" + "b" * 64
HAPPY_DOCKER_IMAGE = "fake/happy@sha256:" + "a" * 64


class HappyScorer:
    def __init__(self, docker_image=None):
        self.docker_image = docker_image or HAPPY_DOCKER_IMAGE

    def score_vcf(self, truth_vcf, query_vcf, reference_fasta=None, confident_bed=None,
                  region=None, reference_sdf=None, mutations_vcf=None):
        control = json.loads((pathlib.Path(__file__).parent.parent / "control.json").read_text())
        pathlib.Path(control["seen_path"]).write_text(json.dumps({
            "truth_vcf": truth_vcf, "query_vcf": query_vcf, "mutations_vcf": mutations_vcf,
            "reference_fasta": reference_fasta, "reference_sdf": reference_sdf,
            "confident_bed": confident_bed, "region": region,
        }, sort_keys=True))
        mode = control["mode"]
        if mode == "raise":
            raise RuntimeError("synthetic upstream failure")
        if mode == "hang":
            time.sleep(control.get("sleep", 30))
        if mode == "none":
            return None
        return dict(control["metrics"])


class AdvancedScorer:
    @staticmethod
    def compute_advanced_score(metrics):
        return float(metrics["_advanced_score_100"])
"""

_UTILS_INIT = "from .scoring import AdvancedScorer, HappyScorer, BCFTOOLS_DOCKER_IMAGE\n"

_VALIDATOR_PY = """\
import math


def _valid_round_score(value, *, label):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score <= 0.0 or score > 1.0:
        return None
    return score


def _is_zero_input_advanced_fingerprint(metrics, combined_final):
    return (
        (metrics.get("f1_snp") or 0.0) == 0.0
        and (metrics.get("f1_indel") or 0.0) == 0.0
        and 0.24999 <= combined_final <= 0.25001
    )
"""

_TOOL_PARAMS_PY = "def validate_region(region):\n    return {'valid': True, 'error': None}\n"

#: a plausible upstream metrics dictionary. Its exact values are irrelevant to the tests; what
#: matters is that whatever is here comes back unchanged.
DEFAULT_METRICS: dict[str, Any] = {
    "_advanced_score_100": 86.25,
    "f1_snp": 0.982,
    "f1_indel": 0.941,
    "recall_snp": 0.975,
    "recall_indel": 0.93,
    "truth_total_snp": 1000,
    "truth_total_indel": 100,
    "query_total_snp": 1004,
    "query_total_indel": 101,
    "fp_snp": 4,
    "fp_indel": 1,
    "overcall_penalty": 0.5,
}


@dataclass(frozen=True)
class FakeUpstream:
    """A verified-looking pinned checkout plus the authority that matches it."""

    root: Path
    commit: str
    control_path: Path
    seen_path: Path

    def set_mode(
        self, mode: str, *, metrics: dict[str, Any] | None = None, sleep: int = 30
    ) -> None:
        """Choose what the synthetic upstream scorer will do on the next call."""
        self.control_path.write_text(
            json.dumps(
                {
                    "mode": mode,
                    "metrics": metrics if metrics is not None else DEFAULT_METRICS,
                    "sleep": sleep,
                    "seen_path": str(self.seen_path),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def seen(self) -> dict[str, Any]:
        """The exact arguments the scorer was handed on the last call."""
        return dict(json.loads(self.seen_path.read_text(encoding="utf-8")))


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def build_fake_upstream(base: Path) -> FakeUpstream:
    """Create the synthetic checkout under ``base`` and return a handle to it."""
    root = base / "fake_minos_subnet"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    for package, module, body in (
        ("utils", "scoring.py", _SCORING_PY),
        ("neurons", "validator.py", _VALIDATOR_PY),
        ("templates", "tool_params.py", _TOOL_PARAMS_PY),
    ):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text(
            _UTILS_INIT if package == "utils" else "", encoding="utf-8"
        )
        (root / package / module).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "synthetic upstream")
    commit = _git(root, "rev-parse", "HEAD")

    handle = FakeUpstream(
        root=root,
        commit=commit,
        control_path=root / "control.json",
        seen_path=base / "upstream-seen.json",
    )
    handle.set_mode("metrics")
    return handle


def authority_for(upstream: FakeUpstream, base: Any) -> Any:
    """The scoring authority that this synthetic checkout satisfies.

    Only the upstream identity fields move: the semantics block, the contract version and the
    hashing rule are the committed ones, so ``scoring_contract_hash`` is still computed the same
    way it is in production.
    """

    def digest(relative: str) -> str:
        return hashlib.sha256((upstream.root / relative).read_bytes()).hexdigest()

    return base.model_copy(
        update={
            "upstream_commit": upstream.commit,
            "scoring_py_sha256": digest("utils/scoring.py"),
            "validator_py_sha256": digest("neurons/validator.py"),
            "tool_params_py_sha256": digest("templates/tool_params.py"),
            "happy_image": "fake/happy@sha256:" + "a" * 64,
            "bcftools_image": "fake/bcftools@sha256:" + "b" * 64,
        }
    )


def oracle_for(upstream: FakeUpstream, authority: Any, *, timeout_seconds: int = 300) -> Any:
    """A production :class:`MinosSubnetOracle` pointed at the synthetic checkout.

    The interpreter is this one only because the synthetic package has no dependencies; the bridge
    still runs as a separate process rooted at the checkout, exactly as in production.
    """
    import os

    from minos_engine.evaluation.minos_subnet_oracle import (
        ENV_MINOS_SUBNET_PYTHON,
        MinosSubnetOracle,
    )

    os.environ[ENV_MINOS_SUBNET_PYTHON] = sys.executable
    return MinosSubnetOracle(
        authority=authority, root=upstream.root, timeout_seconds=timeout_seconds
    )
