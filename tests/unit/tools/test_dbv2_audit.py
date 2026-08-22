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
