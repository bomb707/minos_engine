"""L2-F committed live-GATK PROVENANCE BINDING — behavioral tests (no network).

The accepted loader ``load_committed_live_gatk_parameter_space()`` takes no arguments and reads
only the two fixed committed repository paths. These tests exercise it against temporary copies
of the committed artifact pair (the module's path constants are redirected), so a rejection is
proven behaviorally rather than by inspecting source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from minos_engine.experiments import gatk_live_space as G
from minos_engine.experiments.gatk_live_space import (
    LIVE_SOURCE_ARTIFACT_PATH,
    LiveSpaceError,
    load_committed_live_gatk_parameter_space,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO_ROOT / "manifests" / "l2f_gatk_parameter_space_v1.json"
_SOURCE = _REPO_ROOT / "manifests" / "l2f_gatk_source_object_v1.json"

MutateJson = Callable[[dict[str, Any]], None]
MutateBytes = Callable[[bytes], bytes]


@pytest.fixture
def committed_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., None]]:
    """Redirect the accepted loader at a writable copy of the committed artifact pair."""

    def _install(
        *,
        manifest: MutateJson | None = None,
        source: MutateJson | None = None,
        source_bytes: MutateBytes | None = None,
        manifest_bytes: MutateBytes | None = None,
    ) -> None:
        raw_source = _SOURCE.read_bytes()
        if source is not None:
            doc = json.loads(raw_source)
            source(doc)
            raw_source = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
        if source_bytes is not None:
            raw_source = source_bytes(raw_source)
        src_path = tmp_path / "source.json"
        src_path.write_bytes(raw_source)

        doc = json.loads(_MANIFEST.read_bytes())
        # a faithful copy re-binds to the (possibly rewritten) source bytes unless a test
        # deliberately breaks that binding.
        doc["source_gatk_object_sha256"] = hashlib.sha256(raw_source).hexdigest()
        if manifest is not None:
            manifest(doc)
        raw_manifest = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
        if manifest_bytes is not None:
            raw_manifest = manifest_bytes(raw_manifest)
        man_path = tmp_path / "manifest.json"
        man_path.write_bytes(raw_manifest)

        monkeypatch.setattr(G, "_LIVE_SPACE_PATH", man_path)
        monkeypatch.setattr(G, "_LIVE_SOURCE_PATH", src_path)

    yield _install


# --------------------------------------------------------------------------- #
# positive: the UNMODIFIED committed pair must pass
# --------------------------------------------------------------------------- #
def test_unmodified_committed_pair_loads() -> None:
    space = load_committed_live_gatk_parameter_space()
    assert space.parameter_space_hash == (
        "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
    )
    assert space.source_artifact_path == LIVE_SOURCE_ARTIFACT_PATH
    assert len(space.parameters) == 25


def test_faithful_copy_of_the_pair_loads(committed_pair: Callable[..., None]) -> None:
    committed_pair()
    assert load_committed_live_gatk_parameter_space().parameter_space_hash == (
        "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
    )


# --------------------------------------------------------------------------- #
# the exact defect this corrective closes
# --------------------------------------------------------------------------- #
def test_regression_fabricated_provenance_is_rejected(committed_pair: Callable[..., None]) -> None:
    """REGRESSION: a manifest whose scientific content is untouched but whose provenance fields
    are fabricated was previously ACCEPTED. It must now be rejected."""

    def _fabricate(doc: dict[str, Any]) -> None:
        doc["source_artifact_path"] = "manifests/does_not_exist.json"
        doc["source_gatk_object_sha256"] = "d" * 64
        doc["source_raw_response_sha256"] = "e" * 64

    committed_pair(manifest=_fabricate)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


# --------------------------------------------------------------------------- #
# artifact-path and byte binding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    ["manifests/does_not_exist.json", "manifests/other.json", "", "l2f_gatk_source_object_v1.json"],
)
def test_changed_source_artifact_path_is_rejected(
    committed_pair: Callable[..., None], path: str
) -> None:
    committed_pair(manifest=lambda d: d.__setitem__("source_artifact_path", path))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_missing_source_artifact_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    man = tmp_path / "manifest.json"
    man.write_bytes(_MANIFEST.read_bytes())
    monkeypatch.setattr(G, "_LIVE_SPACE_PATH", man)
    monkeypatch.setattr(G, "_LIVE_SOURCE_PATH", tmp_path / "absent.json")
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_wrong_source_artifact_byte_hash_is_rejected(committed_pair: Callable[..., None]) -> None:
    committed_pair(manifest=lambda d: d.__setitem__("source_gatk_object_sha256", "a" * 64))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_source_artifact_byte_mutation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating even one byte of the source artifact breaks the recorded hash binding."""
    src = tmp_path / "source.json"
    src.write_bytes(_SOURCE.read_bytes().replace(b'"note"', b'"Note"', 1))
    man = tmp_path / "manifest.json"
    man.write_bytes(_MANIFEST.read_bytes())  # keeps the ORIGINAL committed hash
    monkeypatch.setattr(G, "_LIVE_SPACE_PATH", man)
    monkeypatch.setattr(G, "_LIVE_SOURCE_PATH", src)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


# --------------------------------------------------------------------------- #
# cross-artifact agreement
# --------------------------------------------------------------------------- #
def test_retrieved_at_mismatch_between_artifacts_is_rejected(
    committed_pair: Callable[..., None],
) -> None:
    committed_pair(manifest=lambda d: d.__setitem__("retrieved_at", "2020-01-01T00:00:00+00:00"))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_raw_response_hash_mismatch_between_artifacts_is_rejected(
    committed_pair: Callable[..., None],
) -> None:
    committed_pair(manifest=lambda d: d.__setitem__("source_raw_response_sha256", "b" * 64))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_source_endpoint_mismatch_is_rejected(committed_pair: Callable[..., None]) -> None:
    committed_pair(
        source=lambda d: d.__setitem__("source_endpoint", "https://evil.example/parameter-ranges")
    )
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_wrong_options_key_in_source_artifact_is_rejected(
    committed_pair: Callable[..., None],
) -> None:
    committed_pair(
        source=lambda d: d["tools_gatk"].__setitem__("options_key", "deepvariant_options")
    )
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


# --------------------------------------------------------------------------- #
# re-derivation: the two artifacts must describe the SAME parameters
# --------------------------------------------------------------------------- #
def test_source_parameter_change_not_reflected_in_the_manifest_is_rejected(
    committed_pair: Callable[..., None],
) -> None:
    def _bump_source(doc: dict[str, Any]) -> None:
        for p in doc["tools_gatk"]["parameters"]:
            if p["name"] == "min_pruning":
                p["max"] = 12  # the manifest still says 10

    committed_pair(source=_bump_source)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_normalized_parameter_change_not_reflected_in_the_source_is_rejected(
    committed_pair: Callable[..., None],
) -> None:
    def _bump_manifest(doc: dict[str, Any]) -> None:
        for p in doc["parameters"]:
            if p["name"] == "min_pruning":
                p["max"] = 12  # the source artifact still says 10

    committed_pair(manifest=_bump_manifest)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_reordered_manifest_parameters_are_rejected(committed_pair: Callable[..., None]) -> None:
    """Parameter ORDER is part of the re-derived equality."""

    def _swap(doc: dict[str, Any]) -> None:
        doc["parameters"][0], doc["parameters"][1] = doc["parameters"][1], doc["parameters"][0]

    committed_pair(manifest=_swap)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_dropped_source_parameter_is_rejected(committed_pair: Callable[..., None]) -> None:
    committed_pair(source=lambda d: d["tools_gatk"]["parameters"].pop())
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


# --------------------------------------------------------------------------- #
# strict structure: unknown fields and duplicate JSON keys
# --------------------------------------------------------------------------- #
def test_extra_source_artifact_field_is_rejected(committed_pair: Callable[..., None]) -> None:
    committed_pair(source=lambda d: d.__setitem__("extra_authority", {"trust": True}))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_extra_tools_gatk_field_is_rejected(committed_pair: Callable[..., None]) -> None:
    committed_pair(source=lambda d: d["tools_gatk"].__setitem__("unsupported_parameters", []))
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_duplicate_json_keys_in_the_manifest_are_rejected(
    committed_pair: Callable[..., None],
) -> None:
    def _dupe(raw: bytes) -> bytes:
        return raw.replace(b'{\n  "caller"', b'{\n  "caller": "gatk",\n  "caller"', 1)

    committed_pair(manifest_bytes=_dupe)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


def test_duplicate_json_keys_in_the_source_artifact_are_rejected(
    committed_pair: Callable[..., None],
) -> None:
    def _dupe(raw: bytes) -> bytes:
        return raw.replace(b'{\n  "note"', b'{\n  "note": "x",\n  "note"', 1)

    committed_pair(source_bytes=_dupe)
    with pytest.raises(LiveSpaceError):
        load_committed_live_gatk_parameter_space()


# --------------------------------------------------------------------------- #
# the generic parser is private and non-authoritative
# --------------------------------------------------------------------------- #
def test_generic_document_parser_is_private_and_unexported() -> None:
    assert "load_committed_live_gatk_parameter_space" in G.__all__
    assert "load_live_gatk_parameter_space" not in G.__all__
    assert not hasattr(G, "load_live_gatk_parameter_space")
    assert hasattr(G, "_parse_live_space_document")  # private, structural only


def test_live_gatk_parameter_space_uses_the_committed_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied document cannot authorize the production live domain."""
    monkeypatch.setattr(G, "_COMMITTED", None)
    calls = {"n": 0}
    original = G.load_committed_live_gatk_parameter_space

    def _counted() -> Any:
        calls["n"] += 1
        return original()

    monkeypatch.setattr(G, "load_committed_live_gatk_parameter_space", _counted)
    space = G.live_gatk_parameter_space()
    assert calls["n"] == 1
    assert space.parameter_space_hash == (
        "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
    )
    monkeypatch.setattr(G, "_COMMITTED", None)
