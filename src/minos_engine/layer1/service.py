"""Layer 1 service — the single production entry point (Layer 1 spec §1, §16).

``analyze`` is the stable public API. It performs, in order: the CPython 3.12
runtime preflight, the accepted TWIN-READY prerequisite verification, request and
input validation, the deadline-bounded orchestration, and atomic artifact
serialization. Dependencies (the pysam adapter, the monotonic clock, the base
directory used for gate verification) are injected so the workflow is testable and
never hides network access or dataset discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from minos_engine.common.clock import Clock, Deadline, SystemClock
from minos_engine.common.errors import ConfigValidationError, ContractValidationError, GateError
from minos_engine.common.hashing import canonical_hash
from minos_engine.common.runtime import require_supported_runtime

from .adapters.pysam_adapter import PysamAdapter
from .config import Layer1Config, load_layer1_config
from .contracts import ProfileRequest, ProfileResult, ProfileStatus
from .orchestrator import ProfileBundle, run_profile
from .prerequisites import verify_twin_ready_prerequisite
from .serializer import serialize_profile
from .validation import Layer1InputError, validate_inputs

__all__ = ["Layer1Service"]


def _discover_base_dir() -> Path:
    from minos_engine.qualification.git_tree import repo_root

    root = repo_root()
    if root is not None:
        return root
    return Path(__file__).resolve().parents[3]


class Layer1Service:
    """Public Layer 1 entry point (Layer 1 spec §1)."""

    def __init__(
        self,
        *,
        adapter: PysamAdapter | None = None,
        clock: Clock | None = None,
        base_dir: str | Path | None = None,
        require_prerequisite: bool = True,
    ) -> None:
        self._adapter = adapter or PysamAdapter()
        self._clock = clock or SystemClock()
        self._base_dir = Path(base_dir) if base_dir is not None else _discover_base_dir()
        self._require_prerequisite = require_prerequisite

    # -- preconditions ------------------------------------------------------- #
    def _check_prerequisite(self) -> None:
        result = verify_twin_ready_prerequisite(self._base_dir)
        if not result.ok:
            raise GateError("TWIN-READY prerequisite not satisfied: " + "; ".join(result.reasons))

    def _load_config(self, request: ProfileRequest) -> Layer1Config:
        config = load_layer1_config()
        if request.profiler_config_hash and request.profiler_config_hash != config.config_hash:
            raise ConfigValidationError(
                "profiler_config_hash does not match the loaded Layer 1 config"
            )
        return config

    # -- public API ---------------------------------------------------------- #
    def profile(self, request: ProfileRequest) -> ProfileBundle:
        """Produce the in-memory profile bundle (no artifact writing)."""
        require_supported_runtime()
        if not isinstance(request, ProfileRequest):
            raise ContractValidationError("analyze() requires a ProfileRequest")
        if self._require_prerequisite:
            self._check_prerequisite()
        config = self._load_config(request)

        inputs = validate_inputs(
            bam_path=request.bam_path,
            bai_path=request.bai_path,
            reference_path=request.reference_path,
            fai_path=request.fai_path,
            region_source=request.region_source,
            region_convention=request.region_coordinate_convention,
            adapter=self._adapter,
        )
        try:
            profile_id = canonical_hash(
                {
                    "bam_sha256": inputs.identity.bam_sha256,
                    "region": inputs.region.model_dump(mode="json"),
                    "config_hash": config.config_hash,
                    "profiler_version": config.profiler_config_version,
                }
            )[:32]
            budget = min(float(request.budget_seconds), float(config.budget.hard_seconds))
            deadline = Deadline.start(self._clock, budget)
            bundle = run_profile(
                profile_id=profile_id,
                inputs=inputs,
                config=config,
                clock=self._clock,
                deadline=deadline,
            )
        finally:
            inputs.alignment.close()
            inputs.fasta.close()
        return bundle

    def analyze(self, request: ProfileRequest, output_dir: str | Path) -> ProfileResult:
        """Profile and write the three artifacts atomically; return a ProfileResult."""
        try:
            bundle = self.profile(request)
        except Layer1InputError as exc:
            return ProfileResult(
                status=ProfileStatus.FAILED,
                failure_code="INPUT_VALIDATION_FAILED",
                fallback_required=True,
                warnings=(str(exc),),
            )
        created_at = datetime.now(UTC).isoformat()
        try:
            profile_path, windows_path, manifest_path = serialize_profile(
                profile=bundle.profile,
                windows=bundle.windows,
                fingerprint=bundle.fingerprint,
                output_dir=Path(output_dir),
                created_at=created_at,
            )
        except Exception as exc:  # noqa: BLE001 - serialization failure is a typed result
            return ProfileResult(
                status=ProfileStatus.FAILED,
                failure_code="SERIALIZATION_FAILURE",
                fallback_required=True,
                warnings=(str(exc),),
            )
        return ProfileResult(
            status=bundle.profile.status,
            profile_path=str(profile_path),
            windows_path=str(windows_path),
            manifest_path=str(manifest_path),
            fallback_required=bundle.profile.status is ProfileStatus.FAILED,
            warnings=bundle.profile.warnings,
        )
