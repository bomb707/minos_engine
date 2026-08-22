"""Focused tests for the DB-V2 D1 report validator.

The validator is the only executable artifact of a design-only stage, so its guarantees are the
ones worth testing: strict JSON parsing, a deterministic contract hash that excludes its own
field, and cross-document checks that actually fail when a document drifts.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS = REPO_ROOT / "reports" / "database"

_spec = importlib.util.spec_from_file_location(
    "dbv2_audit", REPO_ROOT / "scripts" / "dbv2_audit.py"
)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


# --------------------------------------------------------------------------- #
# strict JSON
# --------------------------------------------------------------------------- #
def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.json"
    path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        audit.load_strict(path)


def test_nested_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.json"
    path.write_text('{"outer": {"b": 1, "b": 2}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        audit.load_strict(path)


def test_well_formed_json_parses(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    path.write_text('{"a": 1, "b": {"c": 2}}', encoding="utf-8")
    assert audit.load_strict(path) == {"a": 1, "b": {"c": 2}}


# --------------------------------------------------------------------------- #
# contract hash
# --------------------------------------------------------------------------- #
def test_contract_hash_excludes_its_own_field() -> None:
    base = {"x": 1, "y": [1, 2, 3]}
    without = audit.contract_hash(base)
    with_empty = audit.contract_hash({**base, audit.CONTRACT_HASH_FIELD: ""})
    with_value = audit.contract_hash({**base, audit.CONTRACT_HASH_FIELD: "deadbeef"})
    assert without == with_empty == with_value


def test_contract_hash_is_key_order_independent() -> None:
    assert audit.contract_hash({"a": 1, "b": 2}) == audit.contract_hash({"b": 2, "a": 1})


def test_contract_hash_changes_when_content_changes() -> None:
    assert audit.contract_hash({"a": 1}) != audit.contract_hash({"a": 2})


def test_contract_hash_is_domain_separated() -> None:
    import hashlib

    payload = {"a": 1}
    undomained = hashlib.sha256(audit.canonical_bytes(payload)).hexdigest()
    assert audit.contract_hash(payload) != undomained


def test_canonical_bytes_are_deterministic_and_newline_terminated() -> None:
    raw = audit.canonical_bytes({"b": 2, "a": 1})
    assert raw == b'{"a":1,"b":2}\n'


# --------------------------------------------------------------------------- #
# the committed reports
# --------------------------------------------------------------------------- #
def test_the_committed_reports_validate() -> None:
    assert audit.validate(REPORTS) == []


def test_the_committed_contract_hash_recomputes() -> None:
    contract = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CONTRACT.json")
    assert contract[audit.CONTRACT_HASH_FIELD] == audit.contract_hash(contract)


def test_every_live_object_is_mapped_exactly_once() -> None:
    inventory = audit.load_strict(REPORTS / "MINOS_DATABASE_V1_INVENTORY.json")
    mapping = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json")
    live = {
        f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"] if t["kind"] in {"r", "v"}
    }
    mapped = [e["source"] for e in mapping["mappings"]]
    assert live <= set(mapped)
    assert len(mapped) == len(set(mapped))


# --------------------------------------------------------------------------- #
# the cross-document checks must actually fail on drift
# --------------------------------------------------------------------------- #
def _copy_reports(tmp_path: Path) -> Path:
    out = tmp_path / "database"
    out.mkdir()
    for name in (
        "MINOS_DATABASE_V1_INVENTORY.json",
        "MINOS_DATABASE_V2_CONTRACT.json",
        "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json",
    ):
        (out / name).write_bytes((REPORTS / name).read_bytes())
    return out


def _rewrite(path: Path, mutate: Any) -> None:
    document = audit.load_strict(path)
    mutate(document)
    path.write_bytes(json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def test_a_tampered_contract_hash_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    _rewrite(
        reports / "MINOS_DATABASE_V2_CONTRACT.json",
        lambda d: d.__setitem__(audit.CONTRACT_HASH_FIELD, "0" * 64),
    )
    problems = audit.validate(reports)
    assert any("contract hash mismatch" in p for p in problems)


def test_a_silently_edited_contract_is_detected(tmp_path: Path) -> None:
    """Editing any contract field without rehashing must fail — that is the point of the hash."""
    reports = _copy_reports(tmp_path)
    _rewrite(
        reports / "MINOS_DATABASE_V2_CONTRACT.json",
        lambda d: d["schemas"][0]["tables"][0].__setitem__("purpose", "quietly changed"),
    )
    assert any("contract hash mismatch" in p for p in audit.validate(reports))


def test_an_unmapped_live_object_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    _rewrite(
        reports / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json",
        lambda d: d["mappings"].pop(0),
    )
    problems = audit.validate(reports)
    assert any("not mapped" in p or "action_totals" in p for p in problems)


def test_a_mapping_to_an_unknown_target_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        entry = next(e for e in d["mappings"] if e["action"] == "KEEP")
        entry["target"] = "catalog.nonexistent_table"

    _rewrite(reports / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json", _break)
    assert any("unknown target" in p for p in audit.validate(reports))


def test_a_foreign_key_to_an_unknown_table_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        table = d["schemas"][0]["tables"][0]
        table.setdefault("foreign_keys", []).append(
            {
                "name": "fk_bogus",
                "columns": ["id"],
                "references": "catalog.ghost",
                "referenced_columns": ["id"],
            }
        )
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_CONTRACT.json", _break)
    assert any("unknown target catalog.ghost" in p for p in audit.validate(reports))


def test_a_query_citing_an_undeclared_index_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        d["critical_queries"][0]["indexes"] = ["ix_does_not_exist"]
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_CONTRACT.json", _break)
    assert any("unknown index" in p for p in audit.validate(reports))


def test_a_wrong_table_count_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        d["table_counts"]["catalog"] = 999
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_CONTRACT.json", _break)
    assert any("table_counts" in p for p in audit.validate(reports))


def test_a_wrong_action_total_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    _rewrite(
        reports / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json",
        lambda d: d["action_totals"].__setitem__("KEEP", 999),
    )
    assert any("action_totals" in p for p in audit.validate(reports))


def test_a_table_without_a_primary_key_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        d["schemas"][0]["tables"][0]["primary_key"] = None
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_CONTRACT.json", _break)
    assert any("no primary_key" in p for p in audit.validate(reports))


@pytest.mark.parametrize(
    "leak",
    [
        "postgresql+psycopg://minos_runner:hunter2@db.internal:5432/minos_engine_db",
        "password = hunter2",
        "secret: abcdef",
    ],
)
def test_credential_shaped_material_is_detected(tmp_path: Path, leak: str) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        d["schemas"][0]["tables"][0]["purpose"] = leak
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_CONTRACT.json", _break)
    assert any("contains a" in p for p in audit.validate(reports))


def test_the_committed_reports_carry_no_credentials() -> None:
    """The control for the check above: the real reports are clean."""
    problems = audit.validate(REPORTS)
    assert not any("contains a" in p for p in problems)


def test_a_missing_report_is_detected(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    (reports / "MINOS_DATABASE_V2_CONTRACT.json").unlink()
    assert any("missing report" in p for p in audit.validate(reports))


# --------------------------------------------------------------------------- #
# TEST-CI-2 — inventory drift verification must catch changes that leave totals intact
# --------------------------------------------------------------------------- #
_spec_ti = importlib.util.spec_from_file_location(
    "test_inventory", REPO_ROOT / "scripts" / "test_inventory.py"
)
assert _spec_ti is not None and _spec_ti.loader is not None
inventory = importlib.util.module_from_spec(_spec_ti)
_spec_ti.loader.exec_module(inventory)

INVENTORY_REPORT = REPO_ROOT / "reports" / "testing" / "MINOS_TEST_INVENTORY.json"


def _inventory_copy(tmp_path: Path) -> Path:
    out = tmp_path / "MINOS_TEST_INVENTORY.json"
    out.write_bytes(INVENTORY_REPORT.read_bytes())
    return out


def _mutate_inventory(path: Path, mutate: Any) -> None:
    document = inventory.load_strict(path)
    mutate(document)
    path.write_bytes(json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _record(document: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return document["files"][index]


def test_the_committed_inventory_verifies() -> None:
    """6: the unmodified inventory succeeds."""
    assert inventory.verify_inventory(INVENTORY_REPORT) == []


def test_the_inventory_build_is_deterministic() -> None:
    """A committed report can only be verified if a fresh build reproduces it byte for byte."""
    first = inventory.canonical_for_test = json.dumps(inventory.build(), sort_keys=True)
    second = json.dumps(inventory.build(), sort_keys=True)
    assert first == second


def test_verify_does_not_rewrite_the_report(tmp_path: Path) -> None:
    """verify must never self-heal the document it is checking."""
    path = _inventory_copy(tmp_path)
    before = path.read_bytes()
    inventory.verify_inventory(path)
    assert path.read_bytes() == before


def test_a_changed_tier_is_detected(tmp_path: Path) -> None:
    """1: one existing file's tier changes; totals are untouched."""
    path = _inventory_copy(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        record = next(r for r in d["files"] if r["recommended_tier"] == "fast")
        record["recommended_tier"] = "full"

    _mutate_inventory(path, _break)
    problems = inventory.verify_inventory(path)
    assert any("recommended_tier drifted" in p for p in problems), problems


@pytest.mark.parametrize("field", ["decision", "reason", "replacement"])
def test_a_changed_decision_reason_or_replacement_is_detected(tmp_path: Path, field: str) -> None:
    """2: decision / reason / replacement changes."""
    path = _inventory_copy(tmp_path)
    value = {"decision": "remove", "reason": "invented", "replacement": "somewhere/else.py"}[field]
    _mutate_inventory(path, lambda d: _record(d).__setitem__(field, value))
    problems = inventory.verify_inventory(path)
    assert any(f"{field} drifted" in p for p in problems), problems


@pytest.mark.parametrize(
    "field",
    [
        "requires_postgres",
        "requires_filesystem",
        "requires_posix",
        "requires_privileged",
        "requires_gate_evidence",
        "category",
        "contract_protected",
        "dependencies",
        "source_modules_covered",
    ],
)
def test_changed_classification_inputs_are_detected(tmp_path: Path, field: str) -> None:
    """3: the markers/classification inputs a tier is derived from."""
    path = _inventory_copy(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        record = _record(d)
        current = record[field]
        record[field] = (not current) if isinstance(current, bool) else ["tampered"]

    _mutate_inventory(path, _break)
    problems = inventory.verify_inventory(path)
    assert any(f"{field} drifted" in p for p in problems), problems


def test_counts_exchanged_between_two_records_are_detected(tmp_path: Path) -> None:
    """4: swapping per-file counts leaves every total identical — and must still fail."""
    path = _inventory_copy(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        a, b = next(
            (x, y)
            for x in d["files"]
            for y in d["files"]
            if x["path"] < y["path"]
            and x["test_count"] != y["test_count"]
            and x["line_count"] != y["line_count"]
        )
        a["test_count"], b["test_count"] = b["test_count"], a["test_count"]
        a["line_count"], b["line_count"] = b["line_count"], a["line_count"]

    _mutate_inventory(path, _break)
    document = inventory.load_strict(path)
    fresh = inventory.build()
    assert document["totals"] == fresh["totals"], "the swap must leave totals identical"
    problems = inventory.verify_inventory(path)
    assert any("test_count drifted" in p for p in problems), problems
    assert any("line_count drifted" in p for p in problems), problems


def test_a_duplicate_json_key_is_detected(tmp_path: Path) -> None:
    """5: strict parsing rejects a duplicate key rather than silently taking the last one."""
    path = _inventory_copy(tmp_path)
    raw = path.read_text(encoding="utf-8")
    tampered = raw.replace('"report":', '"report": "SHADOWED",\n  "report":', 1)
    path.write_text(tampered, encoding="utf-8")
    problems = inventory.verify_inventory(path)
    assert any("duplicate JSON key" in p for p in problems), problems


def test_an_added_or_removed_file_record_is_detected(tmp_path: Path) -> None:
    path = _inventory_copy(tmp_path)
    _mutate_inventory(path, lambda d: d["files"].pop(0))
    assert any("not in the inventory" in p for p in inventory.verify_inventory(path))


def test_a_tampered_redundancy_section_is_detected(tmp_path: Path) -> None:
    path = _inventory_copy(tmp_path)
    _mutate_inventory(path, lambda d: d["redundancy"].__setitem__("exact_duplicate_functions", 999))
    assert any("redundancy drifted" in p for p in inventory.verify_inventory(path))


def test_a_missing_inventory_is_detected(tmp_path: Path) -> None:
    assert any("missing" in p for p in inventory.verify_inventory(tmp_path / "absent.json"))


def test_no_provenance_only_field_is_excluded_from_equality() -> None:
    """Every committed field participates in equality: there is no timestamp to exempt."""
    assert frozenset() == inventory.PROVENANCE_ONLY_FIELDS
