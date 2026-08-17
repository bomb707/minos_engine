"""In-process Layer 1 behavior checks for the L1-READY gate.

These are pure/self-contained predicates over the Layer 1 implementation. Where a
check needs real BAM/FASTA I/O it builds a tiny deterministic pysam fixture in a
temporary directory (never a committed binary, never a real-round directory). The
real-BAM-dependent checks (hard-limit, real-BAM-qualified) are derived by the
runner from the committed integration report, not here.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from minos_engine.layer1.contracts import ProfileRequest

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "profile_schema_hash",
    "profiler_config_hash",
    "profiler_version",
    "contracts_schema_valid",
    "input_validation_complete",
    "filter_policy_verified",
    "feature_known_answers_pass",
    "reference_profiler_verified",
    "determinism_verified",
    "deadline_behavior_verified",
    "memory_policy_verified",
    "truth_isolation_verified",
    "architecture_boundaries_verified",
    "documentation_complete",
]

_FORBIDDEN_STRING_TOKENS = (
    "truth" + ".vcf",
    "mutations" + ".vcf",
    ".sdf",
    "confident" + "_regions",
    "hidden" + "_score",
    "leader" + "board",
    "final" + "_test",
)
_FORBIDDEN_IMPORT_TOKENS = (
    "truth",
    "mutation",
    "scoring",
    "happy",
    "hap_py",
    "evaluator",
    "evaluation",
    "layer2",
    "twin",
    "retrieval",
)

_LAYER1_DOCS = (
    "docs/layer1/ARCHITECTURE.md",
    "docs/layer1/INPUT_CONTRACT.md",
    "docs/layer1/FEATURE_CATALOG.md",
    "docs/layer1/SAMPLING_AND_WINDOWS.md",
    "docs/layer1/FILTERING_POLICY.md",
    "docs/layer1/REFERENCE_PROFILING.md",
    "docs/layer1/DETERMINISM.md",
    "docs/layer1/PERFORMANCE.md",
    "docs/layer1/TRUTH_ISOLATION.md",
    "docs/layer1/REAL_BAM_QUALIFICATION.md",
    "docs/layer1/LIMITATIONS.md",
    "docs/runbooks/LAYER1_PROFILE.md",
    "docs/runbooks/LAYER1_REAL_BAM_QUALIFICATION.md",
)


# --------------------------------------------------------------------------- #
# Identity hashes
# --------------------------------------------------------------------------- #
def profile_schema_hash() -> str:
    from minos_engine.layer1.contracts import BamProfile

    return canonical_hash(BamProfile.model_json_schema())


def profiler_config_hash() -> str:
    from minos_engine.layer1.config import load_layer1_config

    return load_layer1_config().config_hash


def profiler_version() -> str:
    from minos_engine.layer1.config import load_layer1_config

    return load_layer1_config().profiler_config_version


# --------------------------------------------------------------------------- #
# Behavior checks
# --------------------------------------------------------------------------- #
def contracts_schema_valid() -> bool:
    """Every committed Layer 1 schema is in sync with its pydantic model."""
    from minos_engine.layer1.contracts import (
        BamProfile,
        ContextFingerprint,
        ProfileManifest,
        ProfileRequest,
        ProfileResult,
        WindowRow,
    )
    from minos_engine.layer1.integration import IntegrationReport
    from minos_engine.schema_registry import load_schema

    pairs: dict[str, type[BaseModel]] = {
        "layer1-profile-request-v1": ProfileRequest,
        "layer1-profile-result-v1": ProfileResult,
        "layer1-fingerprint-v1": ContextFingerprint,
        "layer1-integration-report-v1": IntegrationReport,
        "bam-profile-v1": BamProfile,
        "window-profile-v1": WindowRow,
        "profile-manifest-v1": ProfileManifest,
    }
    for name, model in pairs.items():
        committed = load_schema(name)
        generated = dict(model.model_json_schema())
        for k in ("$schema", "$id", "title"):
            committed.pop(k, None)
            generated.pop(k, None)
        if committed != generated:
            return False
    return True


@dataclass
class _FakeRead:
    is_unmapped: bool = False
    is_secondary: bool = False
    is_supplementary: bool = False
    is_duplicate: bool = False
    is_qcfail: bool = False
    mapping_quality: int = 60


def filter_policy_verified() -> bool:
    from minos_engine.layer1.filters import ReadFilterPolicy

    p = ReadFilterPolicy()
    return (
        p.classify(_FakeRead(is_unmapped=True, is_duplicate=True)) == "unmapped"
        and p.classify(_FakeRead(is_duplicate=True)) == "duplicate"
        and p.eligible(_FakeRead())
        and p.policy_hash() == ReadFilterPolicy().policy_hash()
    )


def reference_profiler_verified() -> bool:
    from minos_engine.layer1.reference_profile import profile_reference_sequence

    r = profile_reference_sequence("ACGT" * 25)
    homo = profile_reference_sequence("AAAAAACGT", homopolymer_min_run=4)
    return (
        abs(r.gc_fraction - 0.5) < 1e-9
        and abs(r.entropy_bits - 2.0) < 1e-9
        and homo.homopolymer_length_histogram == {"6": 1}
    )


def _tiny_dataset(tmp: Path) -> tuple[Path, Path, Path, Path, str]:
    import pysam

    contig, contig_len = "chr1", 4000
    seq = ("ACGTACGTGCGCATATTTTTTTTAACCGG" * (contig_len // 29 + 1))[:contig_len]
    ref = tmp / "chr1.fa"
    lines = [f">{contig}"] + [seq[i : i + 60] for i in range(0, len(seq), 60)]
    ref.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pysam.faidx(str(ref))  # type: ignore[attr-defined]
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": [{"SN": contig, "LN": contig_len}],
            "RG": [{"ID": "1", "SM": "synthetic", "PL": "ILLUMINA"}],
        }
    )
    bam = tmp / "input.bam"
    reads = []
    for i in range(60):
        s1 = 100 + i * 20
        for read1, start, mate, tlen, rev in (
            (True, s1, s1 + 150, 260, False),
            (False, s1 + 150, s1, -260, True),
        ):
            a = pysam.AlignedSegment(header)
            a.query_name = f"p{i}"
            a.flag = 1 | 2 | (64 if read1 else 128) | (16 if rev else 0)
            a.reference_id = 0
            a.reference_start = start
            a.mapping_quality = 60
            a.cigartuples = [(0, 100)]
            a.query_sequence = "A" * 100
            a.query_qualities = pysam.qualitystring_to_array("I" * 100)
            a.next_reference_id = 0
            a.next_reference_start = mate
            a.template_length = tlen
            a.set_tag("NM", 0, "i")
            reads.append(a)
    reads.sort(key=lambda r: r.reference_start)
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for a in reads:
            out.write(a)
    pysam.index(str(bam))  # type: ignore[attr-defined]
    return bam, Path(str(bam) + ".bai"), ref, Path(str(ref) + ".fai"), f"{contig}:1-{contig_len}"


def _tiny_request(bam: Path, bai: Path, ref: Path, fai: Path, region: str) -> ProfileRequest:
    from minos_engine.layer1.config import load_layer1_config
    from minos_engine.layer1.contracts import ProfileRequest

    cfg = load_layer1_config()
    return ProfileRequest(
        round_id="qualcheck",
        bam_path=str(bam),
        bai_path=str(bai),
        reference_path=str(ref),
        fai_path=str(fai),
        region_source=region,
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )


def input_validation_complete() -> bool:
    from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
    from minos_engine.layer1.validation import Layer1InputError, validate_inputs

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bam, bai, ref, fai, region = _tiny_dataset(tmp)
        # valid inputs verify
        inputs = validate_inputs(
            bam_path=str(bam),
            bai_path=str(bai),
            reference_path=str(ref),
            fai_path=str(fai),
            region_source=region,
            region_convention="one_based_inclusive",
            adapter=PysamAdapter(),
        )
        inputs.alignment.close()
        inputs.fasta.close()
        # missing BAM fails closed
        bam.unlink()
        try:
            validate_inputs(
                bam_path=str(bam),
                bai_path=str(bai),
                reference_path=str(ref),
                fai_path=str(fai),
                region_source=region,
                region_convention="one_based_inclusive",
                adapter=PysamAdapter(),
            )
        except Layer1InputError:
            return True
    return False


def feature_known_answers_pass() -> bool:
    from minos_engine.layer1.service import Layer1Service

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        req = _tiny_request(*_tiny_dataset(tmp))
        bundle = Layer1Service(require_prerequisite=False).profile(req)
        p = bundle.profile
        fc = p.filter_counts
        return (
            fc.observed == fc.included  # all synthetic reads eligible
            and p.reads.proper_pair_fraction == 1.0
            and p.mapping_quality.mean == 60.0
            and p.coverage.fragment_primary.mean_depth_reads_per_base > 0.0
            and p.pairing.eligible_pair_count == 60
        )


def determinism_verified() -> bool:
    from minos_engine.layer1.service import Layer1Service

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        req = _tiny_request(*_tiny_dataset(tmp))
        svc = Layer1Service(require_prerequisite=False)
        a = svc.profile(req).fingerprint.fingerprint_hash
        b = svc.profile(req).fingerprint.fingerprint_hash
        return a == b and len(a) == 64


def deadline_behavior_verified() -> bool:
    from minos_engine.layer1.contracts import PileupMode, ProfileStatus
    from minos_engine.layer1.service import Layer1Service

    class _Stepping:
        def __init__(self, delta: float) -> None:
            self._t = 0.0
            self._d = delta

        def monotonic(self) -> float:
            self._t += self._d
            return self._t

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        req = _tiny_request(*_tiny_dataset(tmp))
        bundle = Layer1Service(clock=_Stepping(30.0), require_prerequisite=False).profile(req)
        return (
            bundle.profile.status is ProfileStatus.PARTIAL
            and bundle.profile.runtime_complexity.chosen_pileup_mode is PileupMode.SKIPPED
            and bool(bundle.profile.degradation)
            and "variant_evidence" not in bundle.profile.completion.completed_families
        )


def memory_policy_verified() -> bool:
    """Structural: the scanner uses bounded aggregators and never retains reads."""
    from minos_engine.layer1 import scan

    src = inspect.getsource(scan)
    tree = ast.parse(src)
    # No attribute assignment that appends the raw read object to a list.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == "read":
                    return False
    return "IntHistogram" in src and "Welford" in src and "CoverageAccumulator" in src


def _layer1_files(src_dir: Path) -> list[Path]:
    return sorted((src_dir / "layer1").rglob("*.py"))


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def truth_isolation_verified(src_dir: Path) -> bool:
    for path in _layer1_files(src_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                low = node.value.lower()
                if any(tok in low for tok in _FORBIDDEN_STRING_TOKENS):
                    return False
    return True


def architecture_boundaries_verified(src_dir: Path) -> bool:
    for path in _layer1_files(src_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(t in alias.name.lower() for t in _FORBIDDEN_IMPORT_TOKENS):
                        return False
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and any(t in node.module.lower() for t in _FORBIDDEN_IMPORT_TOKENS)
            ):
                return False
    return True


def documentation_complete(root: Path) -> bool:
    return all((root / rel).exists() for rel in _LAYER1_DOCS)
