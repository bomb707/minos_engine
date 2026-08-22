"""Focused tests for the DB-V2 D1 report validator.

The validator is the only executable artifact of a design-only stage, so its guarantees are the
ones worth testing: strict JSON parsing, a deterministic contract hash that excludes its own
field, and cross-document checks that actually fail when a document drifts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, cast

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
        "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json",
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


# --------------------------------------------------------------------------- #
# DB-V2 D1.1 — the physical shadow namespace and the Alembic deployment sequence
# --------------------------------------------------------------------------- #
PHYSICAL_REPORT = REPORTS / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"

CANONICAL_SCHEMAS = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)


def _physical() -> dict[str, Any]:
    return audit.load_strict(PHYSICAL_REPORT)


def _v1_relations() -> set[str]:
    inventory = audit.load_strict(REPORTS / "MINOS_DATABASE_V1_INVENTORY.json")
    return {
        f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"] if t["kind"] in {"r", "v"}
    }


def test_exactly_38_logical_tables() -> None:
    """G1."""
    contract = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CONTRACT.json")
    logical = [f"{s['schema']}.{t['table']}" for s in contract["schemas"] for t in s["tables"]]
    assert len(logical) == 38
    assert len(set(logical)) == 38
    assert sorted(_physical()["logical_tables"]) == sorted(logical)


def test_exactly_37_shadow_tables_plus_one_shared_alembic_table() -> None:
    """G2."""
    doc = _physical()
    assert len(doc["physical_shadow_tables"]) == 37
    assert doc["shared_table"] == "public.alembic_version"
    assert doc["counts"]["logical_tables"] == 38
    assert doc["counts"]["physical_shadow_tables"] == 37
    assert doc["counts"]["shared_tables"] == 1
    assert doc["counts"]["d2_tables_created"] == 37


def test_every_logical_table_has_exactly_one_deployment_mapping() -> None:
    """G3."""
    doc = _physical()
    seen: dict[str, int] = {}
    for entry in doc["deployment_mapping"]:
        seen[entry["logical_table"]] = seen.get(entry["logical_table"], 0) + 1
    assert sorted(seen) == sorted(doc["logical_tables"])
    assert all(count == 1 for count in seen.values())


def test_no_two_logical_tables_map_to_the_same_physical_table() -> None:
    """G4."""
    physical = [e["d2_physical_table"] for e in _physical()["deployment_mapping"]]
    assert len(physical) == len(set(physical)) == 38


def test_no_shadow_table_collides_with_the_frozen_v1_inventory() -> None:
    """G5: the whole point of the dbv2_* namespace."""
    doc = _physical()
    v1 = _v1_relations()
    for ident in doc["physical_shadow_tables"]:
        assert ident not in v1, ident
        assert ident.split(".", 1)[0].startswith("dbv2_"), ident


def test_the_collision_that_motivated_the_namespace_is_recorded() -> None:
    """The canonical names really are taken — including the two named in the brief."""
    doc = _physical()
    collisions = doc["problem_statement"]["physical_collision"]["colliding_identities"]
    v1 = _v1_relations()
    assert "catalog.artifacts" in collisions
    assert "profiling.bam_profiles" in collisions
    assert len(collisions) == doc["problem_statement"]["physical_collision"]["collision_count"]
    for ident in collisions:
        assert ident in v1, ident


def test_the_shared_alembic_table_is_never_duplicated() -> None:
    doc = _physical()
    assert "public.alembic_version" not in doc["physical_shadow_tables"]
    shared = next(
        e for e in doc["deployment_mapping"] if e["logical_table"] == "public.alembic_version"
    )
    assert shared["disposition"] == "shared"
    assert shared["d2_physical_table"] == "public.alembic_version"
    assert shared["post_cutover_table"] == "public.alembic_version"


def test_every_foreign_key_target_translates_consistently() -> None:
    """G6: every FK in the logical contract resolves inside the shadow namespace."""
    contract = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CONTRACT.json")
    doc = _physical()
    physical_by_logical = {
        e["logical_table"]: e["d2_physical_table"] for e in doc["deployment_mapping"]
    }
    schema_map = doc["schema_mapping"]["canonical_to_shadow"]
    checked = 0
    for schema in contract["schemas"]:
        for table in schema["tables"]:
            for fk in table.get("foreign_keys", []):
                referenced = fk["references"]
                assert referenced in physical_by_logical, referenced
                canon_schema, name = referenced.split(".", 1)
                assert physical_by_logical[referenced] == f"{schema_map[canon_schema]}.{name}"
                checked += 1
    assert checked > 0


def test_the_schema_map_covers_every_canonical_schema() -> None:
    mapping = _physical()["schema_mapping"]["canonical_to_shadow"]
    assert set(mapping) == set(CANONICAL_SCHEMAS)
    assert all(v == f"dbv2_{k}" for k, v in mapping.items())
    assert "public" not in mapping


def test_cutover_and_rollback_are_complete_inverses() -> None:
    """G7: applying cutover then rollback returns every schema to its starting name."""
    doc = _physical()
    forward: dict[str, str] = {}
    for step in doc["cutover_mapping"]["steps"]:
        forward.update(step.get("mapping") or {})
    backward: dict[str, str] = {}
    for step in doc["rollback_mapping"]["steps"]:
        backward.update(step.get("mapping") or {})

    assert forward and backward
    assert set(backward) == set(forward.values())
    for src, dst in forward.items():
        assert backward[dst] == src, (src, dst)
    # and the permutation is total over the canonical schemas
    assert set(CANONICAL_SCHEMAS) <= set(forward)
    assert doc["rollback_mapping"]["transactional"] is True
    assert doc["rollback_mapping"]["inverse_of_cutover"] is True


def test_the_revision_path_is_exactly_0005_to_0009() -> None:
    """G8."""
    revision = _physical()["revision_path"]
    assert revision["operational_preparation_path"] == [
        "0005_l2e_feature_view",
        "0006_l2f_experiment_plan",
        "0007_l2f_job_claiming",
        "0008_l2f_execution_results",
        "0009_dbv2_shadow_schema",
    ]
    assert revision["source_revision"] == "0005_l2e_feature_view"
    assert revision["planned_d2_down_revision"] == "0008_l2f_execution_results"
    assert revision["development_lifecycle_path"] == [
        "0008_l2f_execution_results",
        "0009_dbv2_shadow_schema",
        "0008_l2f_execution_results",
        "0009_dbv2_shadow_schema",
    ]
    assert revision["authorized_in_d1_1"] is False


def test_no_stamp_skip_or_multiple_head_strategy_is_present() -> None:
    """G9: the shortcuts are named as forbidden, and never used as the plan."""
    doc = _physical()
    forbidden = " ".join(doc["forbidden_migration_shortcuts"]).lower()
    for shortcut in ("stamp", "skipping", "rewriting", "multiple-head", "in-place", "suffix"):
        assert shortcut in forbidden, shortcut
    plan = json.dumps(
        {k: v for k, v in doc.items() if k != "forbidden_migration_shortcuts"}
    ).lower()
    assert "alembic stamp" not in plan
    assert "multiple heads" not in plan


def test_intermediate_revision_invariants_are_stated() -> None:
    invariants = " ".join(_physical()["revision_path"]["intermediate_revision_invariants"]).lower()
    assert "byte-identical" in invariants
    assert "zero business rows" in invariants
    assert "no artifact publication" in invariants
    assert "automatically" in invariants


def test_d2_must_preserve_every_v1_relation() -> None:
    doc = _physical()
    preserved = set(doc["v1_objects_d2_must_preserve"]["relations"])
    assert preserved == _v1_relations()
    assert doc["v1_objects_d2_must_preserve"]["relation_count"] == len(preserved)
    rule = doc["v1_objects_d2_must_preserve"]["rule"].lower()
    for verb in ("rename", "alter", "delete", "write"):
        assert verb in rule


def test_the_schema_rename_dependency_analysis_is_explicit() -> None:
    """A rename alone is not a cutover: function bodies and search_path do not follow."""
    analysis = _physical()["cutover_mapping"]["postgresql_dependency_analysis"]
    does_not = " ".join(analysis["renames_that_do_NOT_follow_automatically"]).lower()
    assert "function body" in does_not
    assert "search_path" in does_not
    assert "regclass" in does_not
    assert analysis["renames_that_follow_automatically"]


def test_the_physical_contract_hash_recomputes() -> None:
    doc = _physical()
    assert doc[audit.CONTRACT_HASH_FIELD] == audit.contract_hash(doc)


def test_strict_parsing_rejects_a_duplicate_key_in_the_physical_report(tmp_path: Path) -> None:
    """G10."""
    target = tmp_path / "phys.json"
    raw = PHYSICAL_REPORT.read_text(encoding="utf-8")
    target.write_text(raw.replace('"report":', '"report": "SHADOWED",\n  "report":', 1))
    with pytest.raises(ValueError, match="duplicate JSON key"):
        audit.load_strict(target)


def test_the_current_to_target_mapping_distinguishes_all_three_targets() -> None:
    mapping = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json")
    doc = _physical()
    physical_by_logical = {
        e["logical_table"]: e["d2_physical_table"] for e in doc["deployment_mapping"]
    }
    for entry in mapping["mappings"]:
        assert "logical_target" in entry
        assert "d2_physical_target" in entry
        assert "post_cutover_target" in entry
        assert entry["logical_target"] == entry["target"]
        if entry["target"] is None:
            assert entry["d2_physical_target"] is None
            continue
        assert entry["post_cutover_target"] == entry["target"]
        assert entry["d2_physical_target"] == physical_by_logical[entry["target"]]


def test_the_logical_contract_records_the_physical_deployment() -> None:
    contract = audit.load_strict(REPORTS / "MINOS_DATABASE_V2_CONTRACT.json")
    physical = contract["physical_deployment"]
    assert physical["canonical_to_shadow"] == _physical()["schema_mapping"]["canonical_to_shadow"]
    assert physical["shared_untouched"] == ["public.alembic_version"]
    assert (
        physical["operational_preparation_path"]
        == (_physical()["revision_path"]["operational_preparation_path"])
    )


def test_migration_0009_exists_with_the_declared_identity() -> None:
    """DB-V2 D2 created it. Exactly one file, with the exact revision identity."""
    versions = REPO_ROOT / "migrations" / "versions"
    found = sorted(p.name for p in versions.glob("0009*.py"))
    assert found == ["0009_dbv2_shadow_schema.py"]
    source = (versions / found[0]).read_text(encoding="utf-8")
    assert 'revision: str = "0009_dbv2_shadow_schema"' in source
    assert 'down_revision: str | None = "0008_l2f_execution_results"' in source


def test_migrations_0006_to_0008_remain_byte_identical() -> None:
    """G14-adjacent: the accepted lineage bytes never change."""
    import hashlib

    expected = {
        "0006_l2f_experiment_plan": (
            "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
        ),
        "0007_l2f_job_claiming": (
            "bc247e0a68f82ad6e52868e115db3f1e237b637def98567c596e3cc0a4e42625"
        ),
        "0008_l2f_execution_results": (
            "95614d67fbfbafb735a0651275dd06f1949ae513b43b96b3776a5a90c436f3ff"
        ),
    }
    for name, digest in expected.items():
        path = REPO_ROOT / "migrations" / "versions" / f"{name}.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name


def test_the_validator_detects_a_shadow_collision(tmp_path: Path) -> None:
    """The collision guard must actually fire."""
    reports = _copy_reports(tmp_path)
    (reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json").write_bytes(
        PHYSICAL_REPORT.read_bytes()
    )

    def _break(d: dict[str, Any]) -> None:
        d["physical_shadow_tables"][0] = "catalog.artifacts"
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("collides with a V1 relation" in p for p in problems), problems


def test_the_validator_detects_a_broken_inverse(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    (reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json").write_bytes(
        PHYSICAL_REPORT.read_bytes()
    )

    def _break(d: dict[str, Any]) -> None:
        for step in d["rollback_mapping"]["steps"]:
            if step["action"] == "rename_retired_back_to_canonical":
                step["mapping"]["v1_retired_catalog"] = "wrong_name"
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("not the inverse" in p for p in problems), problems


def test_the_validator_detects_a_wrong_revision_path(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    (reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json").write_bytes(
        PHYSICAL_REPORT.read_bytes()
    )

    def _break(d: dict[str, Any]) -> None:
        d["revision_path"]["operational_preparation_path"] = [
            "0005_l2e_feature_view",
            "0009_dbv2_shadow_schema",
        ]
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("revision path" in p for p in problems), problems


def test_the_validator_detects_a_missing_physical_report(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)
    (reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json").unlink()
    problems = audit.validate(reports)
    assert any("MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# DB-V2 D1.2 — recovery-set ordering, rollback boundaries, retirement targets
# --------------------------------------------------------------------------- #
def _phases() -> dict[str, Any]:
    return {p["phase"]: p for p in _physical()["recovery_set_protocol"]["phases"]}


def _boundaries() -> dict[str, Any]:
    return {b["boundary"]: b for b in _physical()["rollback_boundaries"]}


def test_r1_occurs_before_migration_0009() -> None:
    """G1: the pre-migration record cannot live in a table the migration creates."""
    r1 = _phases()["R1"]
    assert r1["occurs_before_revision"] == "0009_dbv2_shadow_schema"
    assert r1["occurs_after_revision"] is None
    assert r1["storage"] == ("external file beneath MINOS_DB_RECOVERY_ROOT, outside PostgreSQL")
    assert r1["artifact_path"].endswith(".recovery.json")
    assert r1["immutable"] is True
    # it must NOT be described as a database row
    assert "not a database row" in r1["rationale"].lower()


def test_r1_binds_every_required_identity_field() -> None:
    bound = set(_phases()["R1"]["bound_fields"])
    for field in (
        "database_name",
        "source_alembic_revision",
        "database_backup_sha256",
        "wal_start_lsn",
        "wal_end_lsn",
        "artifact_count",
        "artifact_total_bytes",
        "artifact_snapshot_sha256",
        "created_at",
        "postgresql_version",
        "backup_tool_version",
        "artifact_verification_tool_version",
    ):
        assert field in bound, field
    assert "incomplete" in _phases()["R1"]["completeness_rule"].lower()


def test_r2_occurs_after_shadow_creation_and_before_transformation() -> None:
    """G2."""
    r2 = _phases()["R2"]
    assert r2["occurs_after_revision"] == "0009_dbv2_shadow_schema"
    assert "transformation" in r2["occurs_before_step"]
    assert r2["storage"] == "dbv2_catalog.backup_sets"


def test_r2_never_writes_the_v1_relation_that_does_not_exist() -> None:
    r2 = _phases()["R2"]
    assert r2["forbidden_target"] == "catalog.backup_sets"
    inventory = audit.load_strict(REPORTS / "MINOS_DATABASE_V1_INVENTORY.json")
    v1 = {f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"]}
    assert "catalog.backup_sets" not in v1, "the defect: V1 has no such relation"


def test_r2_requires_equality_and_completeness_before_transformation() -> None:
    rules = " ".join(_phases()["R2"]["rules"]).lower()
    assert "equality" in rules
    assert "completeness = 'complete'" in rules
    assert _phases()["R2"]["retention_of_external_manifest"]


def test_the_external_manifest_survives_a_downgrade_of_0009() -> None:
    """Why the file, not the row, is the authoritative pre-migration record."""
    retention = _phases()["R2"]["retention_of_external_manifest"].lower()
    assert "downgrading 0009" in retention
    assert "not the file" in retention


def test_every_rollback_boundary_has_exactly_one_applicable_procedure() -> None:
    """G3."""
    boundaries = _boundaries()
    assert sorted(boundaries) == ["B1", "B2", "B3"]
    for name, boundary in boundaries.items():
        assert boundary["procedure"], name
        assert boundary["applicable_actions"], name
    # the boundaries are disjoint: no action set is shared between two boundaries
    action_sets = [frozenset(b["applicable_actions"]) for b in boundaries.values()]
    assert len(set(action_sets)) == 3


def test_b2_removes_only_shadow_objects_and_never_touches_v1() -> None:
    b2 = _boundaries()["B2"]
    assert b2["touches_v1"] is False
    assert b2["drops_v2"] is True
    procedure = " ".join(b2["procedure"]).lower()
    assert "only dbv2_" in procedure
    assert "must not alter, rename, delete or write any v1 object" in procedure
    assert "alembic" in procedure


def test_post_cutover_rollback_never_drops_v2() -> None:
    """G5: after cutover the V2 tables ARE the live system."""
    b3 = _boundaries()["B3"]
    assert b3["drops_v2"] is False
    assert not any("drop" in action for action in b3["applicable_actions"])
    assert "not dropped" in b3["drops_v2_note"].lower()
    procedure = " ".join(b3["procedure"]).lower()
    assert "quiesce" in procedure
    assert "rename each canonical v2 schema back" in procedure
    assert "rename each v1_retired_" in procedure


def test_the_contradictory_rollback_statement_is_recorded_as_withdrawn() -> None:
    """G8: the stale text is not merely deleted — it is recorded as wrong, with a reason."""
    withdrawn = _physical()["withdrawn_statements"]
    dropping = next(w for w in withdrawn if "dropping the shadow tables" in w["statement"])
    assert "delete the migrated database" in dropping["why_wrong"].lower()
    assert dropping["replaced_by"]
    statements = " | ".join(w["statement"] for w in withdrawn)
    assert "reports/database/recovery/R1_RECOVERY_MANIFEST.json" in statements
    assert "Point the application back" in statements
    assert "one recovery point" in statements
    assert "covers every active artifact" in statements
    assert len(withdrawn) == 7


def test_forward_cutover_followed_by_rollback_is_the_identity_permutation() -> None:
    """G4: composing the two mappings returns every schema to its starting name."""
    doc = _physical()
    forward: dict[str, str] = {}
    for step in doc["cutover_mapping"]["steps"]:
        forward.update(step.get("mapping") or {})
    backward: dict[str, str] = {}
    for step in doc["rollback_mapping"]["steps"]:
        backward.update(step.get("mapping") or {})
    composed = {src: backward[dst] for src, dst in forward.items()}
    assert composed == {src: src for src in forward}
    assert set(forward) == set(CANONICAL_SCHEMAS) | {f"dbv2_{s}" for s in CANONICAL_SCHEMAS}


def test_retirement_targets_only_the_retired_namespace() -> None:
    """G6."""
    eligible = _physical()["retirement"]["eligible_targets"]
    targets = [*eligible["tables"], *eligible["views"], *eligible["archived_source_objects"]]
    assert targets
    for target in targets:
        assert target.startswith("v1_retired_"), target


def test_canonical_v2_objects_can_never_be_retirement_targets() -> None:
    """G7: the machine check that stops the runbook destroying the migrated system."""
    doc = _physical()
    eligible = doc["retirement"]["eligible_targets"]
    targets = [*eligible["tables"], *eligible["views"], *eligible["archived_source_objects"]]
    logical = set(doc["logical_tables"])
    for target in targets:
        schema = target.split(".", 1)[0]
        assert schema not in CANONICAL_SCHEMAS, target
        assert target not in logical, target
    for schema in CANONICAL_SCHEMAS:
        assert f"{schema}.*" in doc["retirement"]["must_survive_retirement"]


def test_the_exact_defect_catalog_datasets_is_both_a_v2_table_and_was_a_retirement_target() -> None:
    """The sharpest instance of the D1 defect, now impossible."""
    doc = _physical()
    assert "catalog.datasets" in doc["logical_tables"], "it IS a live V2 table after cutover"
    eligible = doc["retirement"]["eligible_targets"]
    assert "catalog.datasets" not in eligible["tables"]
    assert "v1_retired_catalog.datasets" in eligible["tables"]


def test_the_named_retired_targets_are_all_present() -> None:
    tables = _physical()["retirement"]["eligible_targets"]["tables"]
    for expected in (
        "v1_retired_profiling.profiles",
        "v1_retired_experiments.jobs",
        "v1_retired_experiments.results",
        "v1_retired_catalog.datasets",
        "v1_retired_catalog.gatk_configs",
    ):
        assert expected in tables, expected
    views = _physical()["retirement"]["eligible_targets"]["views"]
    assert len(views) == 10, "the ten V1 views"


def test_retirement_requires_per_object_verification() -> None:
    checks = " ".join(_physical()["retirement"]["per_object_checks_before_removal"]).lower()
    assert "foreign key" in checks
    assert "row count" in checks
    assert "identity" in checks
    assert "written since cutover" in checks
    assert "individually passed" in _physical()["retirement"]["schema_drop_rule"].lower()


def test_qualification_period_semantics_are_unambiguous() -> None:
    """F: what each namespace means while V2 is on trial."""
    semantics = _physical()["qualification_period_semantics"]
    assert semantics["canonical_schemas_mean"].startswith("V2")
    assert "rollback source" in semantics["v1_retired_holds"]
    assert semantics["dbv2_names_after_forward_cutover"].startswith("absent")
    assert semantics["dbv2_names_after_rollback"].startswith("present")
    assert semantics["writes_to_retired_v1"].lower().startswith("none")
    assert semantics["deletions_of_v2"].lower().startswith("none")


def test_no_stale_contradictory_execution_text_remains() -> None:
    """G8: the three withdrawn instructions are gone from the runbook."""
    plan = (REPO_ROOT / "docs" / "database" / "MINOS_DATABASE_V2_MIGRATION_PLAN.md").read_text(
        encoding="utf-8"
    )
    assert "Record a `catalog.backup_sets` row" not in plan
    assert "drop the shadow tables" not in plan
    # Every row of the eligible-targets table must name a v1_retired_* object. The surrounding
    # prose may quote a canonical name while EXPLAINING the defect; an instruction may not.
    import re

    retirement = plan[plan.index("### 3.4 Final retirement") :]
    rows = re.findall(r"^\| `([a-z0-9_.]+)` \|", retirement, re.M)
    assert rows, "no eligible-target rows found"
    for target in rows:
        assert target.startswith("v1_retired_"), target
    # and the surviving canonical namespaces are stated explicitly
    assert "must **survive** retirement" in retirement
    for schema in CANONICAL_SCHEMAS:
        assert f"`{schema}.*`" in retirement, schema


def test_the_physical_contract_hash_still_recomputes_after_d1_2() -> None:
    """G10."""
    doc = _physical()
    assert doc[audit.CONTRACT_HASH_FIELD] == audit.contract_hash(doc)


def test_the_validator_detects_a_canonical_retirement_target(tmp_path: Path) -> None:
    """The guard must actually fire."""
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        d["retirement"]["eligible_targets"]["tables"].append("catalog.datasets")
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("canonical ACTIVE schema" in p for p in problems), problems


def test_the_validator_detects_a_b3_that_drops_v2(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        b3 = next(b for b in d["rollback_boundaries"] if b["boundary"] == "B3")
        b3["drops_v2"] = True
        b3["applicable_actions"].append("drop_shadow_schema")
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("B3 must never drop" in p for p in problems), problems
    assert any("must not contain a drop action" in p for p in problems), problems


def test_the_validator_detects_r2_targeting_the_v1_relation(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        r2 = next(p for p in d["recovery_set_protocol"]["phases"] if p["phase"] == "R2")
        r2["storage"] = "catalog.backup_sets"
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("dbv2_catalog.backup_sets" in p for p in problems), problems


def test_the_validator_detects_r1_stored_in_the_database(tmp_path: Path) -> None:
    reports = _copy_reports(tmp_path)

    def _break(d: dict[str, Any]) -> None:
        r1 = next(p for p in d["recovery_set_protocol"]["phases"] if p["phase"] == "R1")
        r1["storage"] = "dbv2_catalog.backup_sets"
        d[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(d)

    _rewrite(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json", _break)
    problems = audit.validate(reports)
    assert any("external file beneath MINOS_DB_RECOVERY_ROOT" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# DB-V2 D1.3 — external recovery storage, R1<->R2 byte binding, stale runbook text
# --------------------------------------------------------------------------- #
DOCS = REPO_ROOT / "docs" / "database"
CONTRACT_REPORT = REPORTS / "MINOS_DATABASE_V2_CONTRACT.json"


def _contract() -> dict[str, Any]:
    return cast("dict[str, Any]", audit.load_strict(CONTRACT_REPORT))


def _copy_root(tmp_path: Path) -> Path:
    """A minimal repository root: the four reports, the four docs, an empty versions dir."""
    root = tmp_path / "root"
    reports = root / "reports" / "database"
    reports.mkdir(parents=True)
    for name in (
        "MINOS_DATABASE_V1_INVENTORY.json",
        "MINOS_DATABASE_V2_CONTRACT.json",
        "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json",
        "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json",
        "MINOS_DATABASE_V2_DATABASE_API.json",
    ):
        (reports / name).write_bytes((REPORTS / name).read_bytes())
    testing = root / "reports" / "testing"
    testing.mkdir(parents=True)
    (testing / "MINOS_TEST_INVENTORY.json").write_bytes(INVENTORY_REPORT.read_bytes())
    docs = root / "docs" / "database"
    docs.mkdir(parents=True)
    for doc in sorted(DOCS.glob("*.md")):
        (docs / doc.name).write_bytes(doc.read_bytes())
    versions = root / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "0008_l2f_execution_results.py").write_text("", encoding="utf-8")
    real = REPO_ROOT / "migrations" / "versions" / "0009_dbv2_shadow_schema.py"
    (versions / real.name).write_bytes(real.read_bytes())
    return root


def _validate_root(root: Path) -> list[str]:
    return cast("list[str]", audit.validate(root / "reports" / "database", root))


def _break_physical(root: Path, mutate: Any) -> None:
    path = root / "reports" / "database" / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"

    def _apply(document: dict[str, Any]) -> None:
        mutate(document)
        document[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(document)

    _rewrite(path, _apply)


def _break_contract(root: Path, mutate: Any) -> None:
    path = root / "reports" / "database" / "MINOS_DATABASE_V2_CONTRACT.json"

    def _apply(document: dict[str, Any]) -> None:
        mutate(document)
        document[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(document)

    _rewrite(path, _apply)


def _table(document: dict[str, Any], schema: str, table: str) -> dict[str, Any]:
    section = next(s for s in document["schemas"] if s["schema"] == schema)
    return next(t for t in section["tables"] if t["table"] == table)


def test_the_committed_design_validates_as_a_whole(tmp_path: Path) -> None:
    """The unmodified design must pass every guard — the baseline every negative needs."""
    assert _validate_root(_copy_root(tmp_path)) == []


# --- G1/G2: the recovery root is external and has no implicit default ---------------------- #
def test_no_recovery_path_points_inside_the_repository() -> None:
    """1: the live recovery contract names no repository directory."""
    physical = _physical()
    live = {k: v for k, v in physical["recovery_storage_contract"].items() if k != "supersedes"}
    texts = audit._strings(live) + audit._strings(physical["recovery_set_protocol"]["phases"])
    offenders = [t for t in texts if audit.REPO_RELATIVE_RE.search(t)]
    assert offenders == [], offenders


def test_the_superseded_repository_path_is_still_recorded() -> None:
    """The defect is recorded rather than silently deleted."""
    supersedes = _physical()["recovery_storage_contract"]["supersedes"]
    assert supersedes["previous_artifact_path"] == (
        "reports/database/recovery/R1_RECOVERY_MANIFEST.json"
    )


def test_the_recovery_root_has_no_default_and_no_repository_fallback() -> None:
    """2: an unset MINOS_DB_RECOVERY_ROOT fails closed; nothing selects the checkout."""
    storage = _physical()["recovery_storage_contract"]
    assert storage["env_var"] == "MINOS_DB_RECOVERY_ROOT"
    assert storage["has_default"] is False
    assert storage["git_rules"]["repository_relative_fallback"] is False
    assert storage["git_rules"]["never_committed"] is True


def test_the_validator_detects_a_repository_relative_recovery_path(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        phase = next(p for p in document["recovery_set_protocol"]["phases"] if p["phase"] == "R1")
        phase["artifact_path"] = "reports/database/recovery/R1_RECOVERY_MANIFEST.json"

    _break_physical(root, _break)
    problems = _validate_root(root)
    assert any("points inside the repository" in p for p in problems), problems


def test_the_validator_detects_an_implicit_recovery_root_default(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    _break_physical(root, lambda d: d["recovery_storage_contract"].__setitem__("has_default", True))
    problems = _validate_root(root)
    assert any("must have no default" in p for p in problems), problems


def test_the_validator_detects_a_repository_relative_fallback(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    _break_physical(
        root,
        lambda d: d["recovery_storage_contract"]["git_rules"].__setitem__(
            "repository_relative_fallback", True
        ),
    )
    problems = _validate_root(root)
    assert any("repository-relative recovery fallback" in p for p in problems), problems


# --- G3/G4/G5: the three artifact bindings ------------------------------------------------- #
def test_backup_sets_binds_all_three_recovery_artifacts() -> None:
    """3: manifest, database backup and artifact snapshot each have id + digest + media type."""
    columns = {c["name"] for c in _table(_contract(), "catalog", "backup_sets")["columns"]}
    for prefix in (
        ("recovery_manifest_artifact_id", "recovery_manifest_sha256"),
        ("database_backup_artifact_id", "database_backup_sha256"),
        ("artifact_snapshot_manifest_artifact_id", "artifact_snapshot_sha256"),
    ):
        for column in prefix:
            assert column in columns, column


def test_every_recovery_foreign_key_resolves_to_a_unique_artifact_target() -> None:
    """4: each FK target is a declared UNIQUE target on catalog.artifacts."""
    contract = _contract()
    artifacts = _table(contract, "catalog", "artifacts")
    unique = {tuple(u["columns"]) for u in artifacts["unique_constraints"]}
    unique.add(tuple(artifacts["primary_key"]["columns"]))
    fks = _table(contract, "catalog", "backup_sets")["foreign_keys"]
    assert len(fks) == 3
    for fk in fks:
        assert fk["references"] == "catalog.artifacts"
        assert tuple(fk["referenced_columns"]) in unique, fk["name"]


def test_every_digest_is_bound_to_the_same_artifact_as_its_id() -> None:
    """5: id, digest and media type travel in ONE composite key, so they cannot disagree."""
    contract, physical = _contract(), _physical()
    fks = {
        fk["columns"][0]: fk for fk in _table(contract, "catalog", "backup_sets")["foreign_keys"]
    }
    phases = {p["phase"]: p for p in physical["recovery_set_protocol"]["phases"]}
    for role, binding in phases["R2"]["artifact_bindings"].items():
        fk = fks[binding["id_column"]]
        assert fk["columns"][1] == binding["digest_column"], role
        assert fk["columns"][2].endswith("media_type"), role


def test_the_validator_detects_a_digest_bound_to_a_different_artifact(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        fk = next(
            f
            for f in _table(document, "catalog", "backup_sets")["foreign_keys"]
            if f["name"] == "fk_backup_sets_database_backup"
        )
        fk["columns"][1] = "artifact_snapshot_sha256"

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("not bound to the same artifact" in p for p in problems), problems


def test_the_validator_detects_a_missing_binding_column(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        table = _table(document, "catalog", "backup_sets")
        table["columns"] = [
            c for c in table["columns"] if c["name"] != "recovery_manifest_artifact_id"
        ]

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("has no id_column" in p for p in problems), problems


def test_the_validator_detects_a_foreign_key_to_a_non_unique_target(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        artifacts = _table(document, "catalog", "artifacts")
        artifacts["unique_constraints"] = [
            u for u in artifacts["unique_constraints"] if u["name"] != "uq_artifacts_id_sha_media"
        ]

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("not a declared UNIQUE target" in p for p in problems), problems


# --- G6/G7/G8: exact media types ----------------------------------------------------------- #
@pytest.mark.parametrize(
    ("role", "media_type"),
    [
        ("recovery_manifest", "application/vnd.minos.db-recovery-manifest+json"),
        ("database_backup", "application/vnd.postgresql.dump"),
        ("artifact_snapshot_manifest", "application/vnd.minos.artifact-snapshot+json"),
    ],
)
def test_each_recovery_media_type_is_exact(role: str, media_type: str) -> None:
    """6, 7, 8: the declared media types agree across both reports and the check constraints."""
    contract, physical = _contract(), _physical()
    assert contract["recovery_media_types"][role] == media_type
    assert physical["recovery_media_types"][role] == media_type
    phases = {p["phase"]: p for p in physical["recovery_set_protocol"]["phases"]}
    assert phases["R2"]["artifact_bindings"][role]["media_type"] == media_type
    checks = " ".join(
        c["expression"] for c in _table(contract, "catalog", "backup_sets")["check_constraints"]
    )
    assert media_type in checks


def test_the_validator_detects_a_wrong_media_type(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        phases = {p["phase"]: p for p in document["recovery_set_protocol"]["phases"]}
        phases["R2"]["artifact_bindings"]["database_backup"]["media_type"] = (
            "application/octet-stream"
        )

    _break_physical(root, _break)
    problems = _validate_root(root)
    assert any("media type" in p for p in problems), problems


# --- G9: the R1 canonical bytes are independently hashable --------------------------------- #
def test_the_r1_canonical_digest_is_reproducible_and_total() -> None:
    """9: hash a manifest independently — order-independent, and covering every field."""
    manifest = {field: f"value-of-{field}" for field in audit.REQUIRED_R1_FIELDS}
    reordered = dict(reversed(list(manifest.items())))
    digest = hashlib.sha256(audit.canonical_bytes(manifest)).hexdigest()
    assert digest == hashlib.sha256(audit.canonical_bytes(reordered)).hexdigest()
    for field in audit.REQUIRED_R1_FIELDS:
        perturbed = dict(manifest, **{field: "changed"})
        assert hashlib.sha256(audit.canonical_bytes(perturbed)).hexdigest() != digest, field


def test_the_validator_detects_a_missing_canonical_bytes_rule(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        phase = next(p for p in document["recovery_set_protocol"]["phases"] if p["phase"] == "R1")
        phase["canonical_bytes_rule"] = "the manifest is hashed somehow"

    _break_physical(root, _break)
    problems = _validate_root(root)
    assert any("canonical bytes" in p for p in problems), problems


# --- G10: R2 represents every immutable R1 field ------------------------------------------- #
def test_every_immutable_r1_field_maps_to_exactly_one_column() -> None:
    """10: no R1 field is unrepresentable, and no column represents two fields."""
    table = _table(_contract(), "catalog", "backup_sets")
    mapping = table["r1_field_to_column"]
    columns = {c["name"] for c in table["columns"]}
    assert set(mapping) == set(audit.REQUIRED_R1_FIELDS)
    assert set(mapping.values()) <= columns
    assert len(set(mapping.values())) == len(mapping)


def test_the_validator_detects_an_unrepresentable_r1_field(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        _table(document, "catalog", "backup_sets")["r1_field_to_column"].pop("wal_end_lsn")

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("r1_field_to_column missing" in p for p in problems), problems


def test_the_validator_detects_a_field_mapped_to_an_absent_column(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        _table(document, "catalog", "backup_sets")["r1_field_to_column"]["created_at"] = "nope"

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("maps to absent column" in p for p in problems), problems


# --- G11/G12/G13: completeness, idempotency, conflicting metadata --------------------------- #
def test_completeness_requires_all_three_verified_artifacts() -> None:
    """11: 'complete' is unreachable until every referenced artifact is verified."""
    rule = _table(_contract(), "catalog", "backup_sets")["completeness_rule"].lower()
    assert "three" in rule and "verified" in rule


def test_the_validator_detects_a_weakened_completeness_rule(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        _table(document, "catalog", "backup_sets")["completeness_rule"] = (
            "completeness may become 'complete' whenever the operator says so."
        )

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("completeness rule" in p for p in problems), problems


def test_reregistration_is_idempotent_and_conflicts_fail_closed() -> None:
    """12, 13: a same-data re-run is a no-op; any differing immutable value fails closed."""
    table = _table(_contract(), "catalog", "backup_sets")
    assert "no-op" in table["idempotency_rule"]
    assert "fails closed" in table["idempotency_rule"]
    assert "never overwrites" in table["idempotency_rule"]
    uniques = {tuple(u["columns"]) for u in table["unique_constraints"]}
    assert ("recovery_set_id",) in uniques
    assert ("backup_key",) in uniques
    assert ("recovery_manifest_sha256",) in uniques


def test_the_validator_detects_a_missing_conflict_constraint(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        table = _table(document, "catalog", "backup_sets")
        table["unique_constraints"] = [
            u for u in table["unique_constraints"] if u["name"] != "uq_backup_sets_recovery_set"
        ]

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("UNIQUE(recovery_set_id)" in p for p in problems), problems


def test_the_validator_detects_a_lost_idempotency_rule(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        _table(document, "catalog", "backup_sets")["idempotency_rule"] = "re-runs overwrite."

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("idempotent re-registration" in p for p in problems), problems


# --- G14: no stale drop-the-shadow rollback instruction ------------------------------------ #
def test_no_post_cutover_drop_the_shadow_instruction_remains() -> None:
    """14: every surviving mention is inside a paragraph that records it as withdrawn."""
    for doc in sorted(DOCS.glob("*.md")):
        for paragraph in doc.read_text(encoding="utf-8").split("\n\n"):
            if not audit.DROP_SHADOW_RE.search(paragraph):
                continue
            lowered = paragraph.lower()
            assert any(m in lowered for m in audit.WITHDRAWAL_MARKERS), (doc.name, paragraph[:120])


def test_the_strategy_table_rollback_entry_is_boundary_aware() -> None:
    """The exact corrected wording, not a paraphrase."""
    text = (DOCS / "MINOS_DATABASE_V2_MIGRATION_PLAN.md").read_text(encoding="utf-8")
    assert "Point the application back; drop the shadow |" not in text
    assert "pre-cutover `downgrade 0009 → 0008`" in text
    assert "post-cutover transactional inverse schema rename" in text


def test_the_validator_detects_a_reintroduced_drop_the_shadow_instruction(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    doc = root / "docs" / "database" / "MINOS_DATABASE_V2_MIGRATION_PLAN.md"
    doc.write_text(
        doc.read_text(encoding="utf-8")
        + "\n\nTo roll back after cutover, point the application back and drop the shadow tables.\n",
        encoding="utf-8",
    )
    problems = _validate_root(root)
    assert any("stale drop-the-shadow" in p for p in problems), problems


# --- G15: no canonical V2 object is a retirement target ------------------------------------ #
def test_no_canonical_v2_object_is_a_retirement_target_after_d1_3() -> None:
    """15: still true after the D1.3 edits."""
    eligible = _physical()["retirement"]["eligible_targets"]
    targets = [
        *eligible["tables"],
        *eligible["views"],
        *eligible.get("archived_source_objects", []),
    ]
    assert targets
    for target in targets:
        assert target.split(".", 1)[0].startswith("v1_retired_"), target


# --- G16/G17/G18/G19: migration absence, strict parsing, hashes, links ---------------------- #
def test_the_migration_chain_is_intact_through_0009() -> None:
    """16, updated for D2: 0008 is still there, and 0009 now sits on top of it."""
    versions = REPO_ROOT / "migrations" / "versions"
    assert versions.is_dir()
    assert sorted(p.stem for p in versions.glob("0008*.py")) == ["0008_l2f_execution_results"]
    assert sorted(p.stem for p in versions.glob("0009*.py")) == ["0009_dbv2_shadow_schema"]


def test_the_validator_detects_a_missing_versions_directory(tmp_path: Path) -> None:
    """The 0009-absence check must never pass by looking at nothing."""
    root = _copy_root(tmp_path)
    for child in sorted((root / "migrations" / "versions").iterdir()):
        child.unlink()
    (root / "migrations" / "versions").rmdir()
    problems = _validate_root(root)
    assert any("no migrations directory" in p for p in problems), problems


def test_the_validator_detects_a_missing_migration_0009(tmp_path: Path) -> None:
    """D2: the guard now fires when 0009 is absent, or when its identity is wrong."""
    root = _copy_root(tmp_path)
    versions = root / "migrations" / "versions"
    (versions / "0009_dbv2_shadow_schema.py").unlink()
    assert any("must be exactly 0009_dbv2_shadow_schema.py" in p for p in _validate_root(root))


def test_the_validator_detects_a_wrong_down_revision_on_0009(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    path = root / "migrations" / "versions" / "0009_dbv2_shadow_schema.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'down_revision: str | None = "0008_l2f_execution_results"',
            'down_revision: str | None = "0007_l2f_job_claiming"',
        ),
        encoding="utf-8",
    )
    problems = _validate_root(root)
    assert any("does not declare down_revision" in p for p in problems), problems


def test_every_committed_json_report_parses_strictly_and_rehashes() -> None:
    """17, 18: duplicate keys are rejected and every embedded hash recomputes."""
    checked = 0
    for path in sorted(REPORTS.glob("*.json")):
        document = audit.load_strict(path)
        if audit.CONTRACT_HASH_FIELD in document:
            assert document[audit.CONTRACT_HASH_FIELD] == audit.contract_hash(document), path.name
            checked += 1
    assert checked >= 2


def test_the_validator_detects_a_duplicate_key_in_any_report(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    path = root / "reports" / "database" / "MINOS_DATABASE_V1_INVENTORY.json"
    path.write_text('{"live": 1, "live": 2}', encoding="utf-8")
    problems = _validate_root(root)
    assert any("duplicate" in p for p in problems), problems


def test_every_documentation_link_resolves() -> None:
    """19: no DB-V2 document points at a file that does not exist."""
    assert audit._validate_docs_and_migrations(REPO_ROOT) == []


def test_the_validator_detects_a_broken_documentation_link(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    doc = root / "docs" / "database" / "MINOS_DATABASE_V2_ERD.md"
    doc.write_text(
        doc.read_text(encoding="utf-8") + "\n\nSee [the plan](MINOS_DATABASE_V2_GONE.md).\n",
        encoding="utf-8",
    )
    problems = _validate_root(root)
    assert any("broken link" in p for p in problems), problems


def test_the_logical_and_physical_hashes_changed_in_d1_3() -> None:
    """The D1.2 hashes must no longer be the committed ones — the contract really changed."""
    assert _contract()[audit.CONTRACT_HASH_FIELD] != (
        "db135128d5abc9c9695b770c50f66fd635efc1bb0cc18640f9c363e7ca40b395"
    )
    assert _physical()[audit.CONTRACT_HASH_FIELD] != (
        "cb08322bdb9a8011327398bb235215ebbf230151b3661eaf7af4eb9a56d4ef71"
    )


# --------------------------------------------------------------------------- #
# DB-V2 D1.4 — snapshot eligibility, row shapes, the database API, the ACL matrix
# --------------------------------------------------------------------------- #
API_REPORT = REPORTS / "MINOS_DATABASE_V2_DATABASE_API.json"


def _api() -> dict[str, Any]:
    return cast("dict[str, Any]", audit.load_strict(API_REPORT))


def _break_api(root: Path, mutate: Any) -> None:
    path = root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json"

    def _apply(document: dict[str, Any]) -> None:
        mutate(document)
        document[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(document)

    _rewrite(path, _apply)
    # both contracts pin the API hash, so re-pin it or every negative fires the wrong guard
    api = audit.load_strict(path)
    for name in ("MINOS_DATABASE_V2_CONTRACT.json", "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"):
        other = root / "reports" / "database" / name

        def _repin(document: dict[str, Any]) -> None:
            document["database_api"][audit.CONTRACT_HASH_FIELD] = api[audit.CONTRACT_HASH_FIELD]
            document[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(document)

        _rewrite(other, _repin)


def _acl_record(api: dict[str, Any], object_type: str, obj: str, principal: str) -> dict[str, Any]:
    return next(
        r
        for r in api["acl"]["records"]
        if (r["object_type"], r["object"], r["principal"]) == (object_type, obj, principal)
    )


# --- K1/K2: recovery artifacts can never enter the snapshot they describe ------------------- #
def test_the_snapshot_predicate_excludes_recovery_artifacts() -> None:
    """1: the exact predicate, not 'all active artifacts'."""
    predicate = _contract()["artifact_snapshot_predicate"]
    assert predicate["where"] == "lifecycle_state = 'active' AND backup_scope = 'operational'"
    assert predicate["excluded_by_construction"] == "backup_scope = 'recovery'"
    assert predicate["table"] == "catalog.artifacts"


def test_registering_a_recovery_set_does_not_change_the_snapshot_it_hashes() -> None:
    """1: the fixed-point property the D1.3 contract did not have. Modelled, then asserted."""
    operational = [
        {
            "content_sha256": "a" * 64,
            "size_bytes": 3,
            "artifact_kind": "vcf",
            "lifecycle_state": "active",
            "backup_scope": "operational",
        },
        {
            "content_sha256": "b" * 64,
            "size_bytes": 5,
            "artifact_kind": "bam",
            "lifecycle_state": "active",
            "backup_scope": "operational",
        },
    ]
    recovery = [
        {
            "content_sha256": c * 64,
            "size_bytes": 9,
            "artifact_kind": kind,
            "lifecycle_state": "active",
            "backup_scope": "recovery",
        }
        for c, kind in (("c", "recovery_manifest"), ("d", "backup"), ("e", "snapshot"))
    ]

    def snapshot(rows: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
        selected = [
            r
            for r in rows
            if r["lifecycle_state"] == "active" and r["backup_scope"] == "operational"
        ]
        return sorted((r["content_sha256"], r["size_bytes"], r["artifact_kind"]) for r in selected)

    before = snapshot(operational)
    after = snapshot(operational + recovery)  # R2 published three more ACTIVE artifacts
    assert before == after, "R2 must not change the inventory R1 hashed"
    assert len(before) == 2


def test_backup_scope_is_immutable_and_two_valued() -> None:
    """2: an artifact's scope is fixed at publication; it can never be reclassified."""
    artifacts = _table(_contract(), "catalog", "artifacts")
    column = next(c for c in artifacts["columns"] if c["name"] == "backup_scope")
    assert column["nullable"] is False
    assert column["mutability"] == "immutable"
    checks = {c["name"]: c["expression"] for c in artifacts["check_constraints"]}
    assert checks["ck_artifacts_backup_scope"] == "backup_scope IN ('operational','recovery')"
    assert "backup_scope" in _api()["immutable_column_inventory"]["catalog.artifacts"]


def test_an_artifact_changes_lifecycle_without_changing_backup_scope() -> None:
    """12 of section C: the documented independence of the two columns."""
    text = _table(_contract(), "catalog", "artifacts")["lifecycle_and_backup_scope"]
    assert "orthogonal" in text
    assert "backup_scope is\nimmutable".replace("\n", " ") in text.replace("\n", " ")


def test_the_validator_detects_an_unqualified_snapshot_predicate(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    _break_contract(
        root,
        lambda d: d["artifact_snapshot_predicate"].__setitem__(
            "where", "lifecycle_state = 'active'"
        ),
    )
    problems = _validate_root(root)
    assert any("snapshot predicate WHERE" in p for p in problems), problems


def test_the_validator_detects_a_mutable_backup_scope(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        artifacts = _table(document, "catalog", "artifacts")
        next(c for c in artifacts["columns"] if c["name"] == "backup_scope")["mutability"] = (
            "mutable"
        )

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("backup_scope must be immutable" in p for p in problems), problems


def test_the_validator_detects_a_missing_snapshot_index(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        artifacts = _table(document, "catalog", "artifacts")
        artifacts["indexes"] = [
            i for i in artifacts["indexes"] if i["name"] != "ix_artifacts_operational_snapshot"
        ]

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("no index named" in p for p in problems), problems


# --- K3: the digest formula is exact and independently reproducible ------------------------- #
def test_the_snapshot_digest_is_domain_separated_and_order_dependent() -> None:
    """3: computed here, not asserted from prose."""
    digest = _contract()["artifact_snapshot_digest"]
    assert digest["domain"] == "minos:db-v2-artifact-snapshot:v1\n"
    assert digest["entry_fields"] == ["content_sha256", "size_bytes", "artifact_kind"]
    domain = digest["domain"].encode("utf-8")
    entries = [
        {"artifact_kind": "vcf", "content_sha256": "a" * 64, "size_bytes": 3},
        {"artifact_kind": "bam", "content_sha256": "b" * 64, "size_bytes": 5},
    ]
    manifest = {
        "artifact_count": 2,
        "artifact_total_bytes": 8,
        "entries": entries,
        "predicate": "p",
        "recovery_set_id": "r",
        "schema_version": "v1",
    }
    value = hashlib.sha256(domain + audit.canonical_bytes(manifest)).hexdigest()
    assert value != hashlib.sha256(audit.canonical_bytes(manifest)).hexdigest()
    swapped = dict(manifest, entries=list(reversed(entries)))
    assert value != hashlib.sha256(domain + audit.canonical_bytes(swapped)).hexdigest()
    assert digest["domain"] != audit.CONTRACT_HASH_DOMAIN.decode("utf-8")


def test_the_validator_detects_a_digest_without_domain_separation(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    _break_contract(
        root,
        lambda d: d["artifact_snapshot_digest"].__setitem__(
            "formula", "artifact_snapshot_sha256 = sha256(canonical_json_bytes(manifest))"
        ),
    )
    problems = _validate_root(root)
    assert any("must be domain-separated" in p for p in problems), problems


# --- K4/K5: the two row shapes, and immutable completeness ---------------------------------- #
def test_the_two_row_shapes_are_mutually_exclusive_and_exhaustive() -> None:
    """4: parsed from the CHECK, not from the prose beside it."""
    table = _table(_contract(), "catalog", "backup_sets")
    checks = {c["name"]: c["expression"] for c in table["check_constraints"]}
    shapes = audit._shape_disjuncts(checks["ck_backup_sets_shape"])
    assert len(shapes) == 2
    assert {s["completeness"] for s in shapes} == {"complete", "database_only"}
    by_state = {s["completeness"]: s["nullness"] for s in shapes}
    for column in audit.SNAPSHOT_SHAPE_COLUMNS:
        assert by_state["complete"][column] is False, column
        assert by_state["database_only"][column] is True, column


def test_the_database_only_shape_is_structurally_possible() -> None:
    """6 of section B, corrected: every snapshot column is nullable and has no default."""
    columns = {c["name"]: c for c in _table(_contract(), "catalog", "backup_sets")["columns"]}
    for column in audit.SNAPSHOT_SHAPE_COLUMNS:
        assert columns[column]["nullable"] is True, column
        assert "default" not in columns[column], column


def test_completeness_is_immutable_and_never_upgraded_in_place() -> None:
    """5: no mutable database_only -> complete transition exists anywhere."""
    table = _table(_contract(), "catalog", "backup_sets")
    columns = {c["name"]: c for c in table["columns"]}
    assert columns["completeness"]["mutability"] == "immutable"
    assert table["mutable_columns"] == ["restore_tested_at"]
    machine = _api()["state_machines"]["backup_set_immutability"]
    assert machine["transitions"] == []
    assert ["database_only", "complete"] in machine["forbidden"]
    assert _api()["mutable_column_rules"]["catalog.backup_sets"] == {
        "restore_tested_at": ("the ONLY permitted UPDATE target on this table; forward-only")
    }


def test_the_validator_detects_a_not_null_snapshot_column(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        table = _table(document, "catalog", "backup_sets")
        next(c for c in table["columns"] if c["name"] == "artifact_count")["nullable"] = False

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("structurally impossible" in p for p in problems), problems


def test_the_validator_detects_a_mutable_completeness(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        table = _table(document, "catalog", "backup_sets")
        next(c for c in table["columns"] if c["name"] == "completeness")["mutability"] = "mutable"

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("completeness must be immutable" in p for p in problems), problems


def test_the_validator_detects_a_half_populated_shape(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        table = _table(document, "catalog", "backup_sets")
        check = next(c for c in table["check_constraints"] if c["name"] == "ck_backup_sets_shape")
        check["expression"] = check["expression"].replace(
            "AND artifact_count IS NULL AND artifact_total_bytes IS NULL",
            "AND artifact_total_bytes IS NULL",
        )

    _break_contract(root, _break)
    problems = _validate_root(root)
    assert any("says nothing about artifact_count" in p for p in problems), problems


# --- K6: cross-table completeness enforcement ------------------------------------------------ #
def test_the_completeness_gate_is_a_constraint_trigger_checking_all_fifteen_conditions() -> None:
    """6: a CHECK cannot reference another table, so the gate must exist and be exact."""
    api = _api()
    gate = next(f for f in api["functions"] if f["name"] == "catalog.enforce_backup_set_shape")
    assert gate["security_mode"] == "DEFINER"
    assert "catalog.artifacts" in gate["tables_read"]
    assert "catalog.artifact_locations" in gate["tables_read"]
    assert len(gate["sqlstates"]) == 15
    declared = " | ".join(gate["sqlstates"])
    for phrase in audit.GATE_REQUIRED_CHECKS:
        assert phrase in declared, phrase
    trigger = next(t for t in api["triggers"] if t["function"] == gate["name"])
    assert trigger["constraint_trigger"] is True
    assert trigger["deferrability"] == "DEFERRABLE INITIALLY IMMEDIATE"
    assert "INSERT" in trigger["event"]


def test_the_validator_detects_a_gate_that_skips_a_condition(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        gate = next(
            f for f in document["functions"] if f["name"] == "catalog.enforce_backup_set_shape"
        )
        gate["sqlstates"] = [
            s for s in gate["sqlstates"] if "a recovery artifact appears in the snapshot" not in s
        ]

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("does not reject: a recovery artifact appears" in p for p in problems), problems


def test_the_validator_detects_a_non_constraint_gate(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        trigger = next(t for t in document["triggers"] if t["name"] == "trg_backup_sets_shape")
        trigger["constraint_trigger"] = False

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("must be a CONSTRAINT trigger" in p for p in problems), problems


# --- K7/K12: every immutable column and every protected table has its trigger ---------------- #
def test_every_immutable_column_is_covered_by_a_declared_trigger() -> None:
    """7: 286 annotations, every one of them enforced."""
    api, contract = _api(), _contract()
    by_table: dict[str, list[dict[str, Any]]] = {}
    for trigger in api["triggers"]:
        by_table.setdefault(trigger["table"], []).append(trigger)
    covered = 0
    for schema in contract["schemas"]:
        for table in schema["tables"]:
            ident = f"{schema['schema']}.{table['table']}"
            if ident == "public.alembic_version":
                continue
            immutable = [c["name"] for c in table["columns"] if c["mutability"] == "immutable"]
            assert api["immutable_column_inventory"][ident] == immutable, ident
            functions = {t["function"] for t in by_table[ident]}
            assert functions & audit.IMMUTABILITY_FUNCTIONS, ident
            covered += len(immutable)
    assert covered == api["counts"]["immutable_columns"] == 286


def test_every_table_refuses_delete() -> None:
    """12: no DB-V2 table accepts DELETE from any role."""
    api = _api()
    deleting = {t["table"] for t in api["triggers"] if t["event"] == "DELETE"}
    assert deleting == set(api["no_delete_tables"]["tables"])
    assert len(deleting) == 37


def test_the_validator_detects_an_unprotected_immutable_column(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["immutable_column_inventory"]["catalog.datasets"].pop()

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("immutable column inventory does not match" in p for p in problems), problems


def test_the_validator_detects_a_missing_delete_guard(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["triggers"] = [
            t for t in document["triggers"] if t["name"] != "trg_events_no_delete"
        ]

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("no DELETE protection trigger" in p for p in problems), problems


def test_the_validator_detects_a_generic_guard_with_wrong_arguments(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        trigger = next(
            t for t in document["triggers"] if t["name"] == "trg_artifacts_immutable_columns"
        )
        trigger["arguments"] = trigger["arguments"][:-1]

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("do not equal the immutable columns" in p for p in problems), problems


# --- K8/K9: transitions -------------------------------------------------------------------- #
def test_every_declared_transition_uses_a_real_state() -> None:
    """8: every state named by a machine is in its table's CHECK domain and is reachable."""
    api, contract = _api(), _contract()
    checked = 0
    for key, machine in api["state_machines"].items():
        if machine["column"].startswith("("):
            continue
        schema_name, table_name = machine["table"].split(".", 1)
        table = _table(contract, schema_name, table_name)
        domain = audit._check_domain(table, machine["column"]) | {"(null)"}
        if not domain - {"(null)"}:
            continue
        for source, target in machine["transitions"]:
            assert source in domain, (key, source)
            assert target in domain, (key, target)
        checked += 1
    assert checked >= 10


def test_no_transition_is_both_allowed_and_forbidden() -> None:
    """9: the two lists are disjoint, and every machine forbids something."""
    for key, machine in _api()["state_machines"].items():
        allowed = {tuple(t) for t in machine["transitions"]}
        forbidden = {tuple(t) for t in machine["forbidden"]}
        assert forbidden, key
        assert not allowed & forbidden, key


def test_the_validator_detects_a_transition_to_an_undeclared_state(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["state_machines"]["job_state"]["transitions"].append(["RUNNING", "PAUSED"])

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("is not in the CHECK domain" in p for p in problems), problems


def test_the_validator_detects_a_contradictory_transition(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        machine = document["state_machines"]["job_state"]
        machine["forbidden"].append(machine["transitions"][0])

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("both allowed and forbidden" in p for p in problems), problems


def test_the_validator_detects_an_unreachable_declared_state(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        machine = document["state_machines"]["job_state"]
        machine["transitions"] = [t for t in machine["transitions"] if t != ["CLAIMED", "RUNNING"]]

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("unreachable through any allowed transition" in p for p in problems), problems


# --- K10/K11/K19: function contracts ---------------------------------------------------------- #
def test_every_function_has_an_exact_signature_and_security_contract() -> None:
    """10: 34 functions, each with a pinned search_path and PUBLIC revoked."""
    functions = _api()["functions"]
    assert len(functions) == 34
    assert len({f["name"] for f in functions}) == 34
    for function in functions:
        assert function["signature"].startswith(function["name"] + "("), function["name"]
        assert function["security_mode"] in {"INVOKER", "DEFINER"}
        assert function["configured_search_path"] == "pg_catalog"
        assert "PUBLIC" in function["revoked_roles"]
        assert function["sqlstates"], function["name"]
        assert not set(function["executable_roles"]) & set(function["revoked_roles"])


def test_every_trigger_references_a_declared_function() -> None:
    """11: 89 triggers, no dangling function reference, no duplicate name."""
    api = _api()
    names = {f["name"] for f in api["functions"]}
    triggers = api["triggers"]
    assert len(triggers) == 89
    assert len({t["name"] for t in triggers}) == 89
    for trigger in triggers:
        assert trigger["function"] in names, trigger["name"]


def test_every_function_is_recreated_in_the_cutover_transaction() -> None:
    """19: a plpgsql body is text, so a schema rename does not follow it."""
    api, physical = _api(), _physical()
    mapping = {m["canonical_function"]: m for m in physical["function_deployment_mapping"]}
    assert set(mapping) == {f["name"] for f in api["functions"]}
    for function in api["functions"]:
        assert function["cutover_recreation_required"] is True, function["name"]
    for key in ("cutover_mapping", "rollback_mapping"):
        actions = [s["action"] for s in physical[key]["steps"]]
        assert "recreate_function_bodies" in actions
        assert actions.index("recreate_function_bodies") < actions.index("revalidate")


def test_the_validator_detects_an_unpinned_search_path(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["functions"][0]["configured_search_path"] = "public, pg_catalog"

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("search_path must be pinned" in p for p in problems), problems


def test_the_validator_detects_a_dangling_trigger_function(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["triggers"][0]["function"] = "catalog.does_not_exist"

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("names undeclared function" in p for p in problems), problems


def test_the_validator_detects_a_function_not_recreated_at_cutover(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["functions"][0]["cutover_recreation_required"] = False

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("must be re-created in the cutover transaction" in p for p in problems), problems


# --- K13/K14/K15/K16: the ACL matrix ---------------------------------------------------------- #
def test_the_acl_matrix_is_complete_and_unambiguous() -> None:
    """13, 14: 80 objects x 10 principals, exactly one record each."""
    api, contract = _api(), _contract()
    acl = api["acl"]
    schemas = {s["schema"] for s in contract["schemas"]}
    tables = {f"{s['schema']}.{t['table']}" for s in contract["schemas"] for t in s["tables"]}
    functions = {f["name"] for f in api["functions"]}
    assert set(acl["objects"]["schemas"]) == schemas
    assert set(acl["objects"]["tables"]) == tables
    assert set(acl["objects"]["functions"]) == functions
    pairs = [(r["object_type"], r["object"], r["principal"]) for r in acl["records"]]
    assert len(pairs) == len(set(pairs)) == 800
    assert len(acl["principals"]) == 10
    assert (len(schemas) + len(tables) + len(functions)) * 10 == 800


def test_public_holds_no_application_privilege() -> None:
    """15: every PUBLIC record is empty, and the default grant is revoked."""
    api = _api()
    public = [r for r in api["acl"]["records"] if r["principal"] == "PUBLIC"]
    assert len(public) == 80
    for record in public:
        assert not any(record["privileges"].values()), record["object"]
        assert record["grant_option"] is False
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in api["acl"]["public_revocation"]


def test_no_runtime_role_holds_a_ddl_privilege() -> None:
    """16: no CREATE, no TRUNCATE, no REFERENCES, no TRIGGER, anywhere."""
    api = _api()
    assert set(api["acl"]["create_privilege"]["granted_to"]) == {"minos_migrate", "minos_owner"}
    for record in api["acl"]["records"]:
        if record["object_type"] != "table" or record["principal"] not in audit.RUNTIME_ROLES:
            continue
        for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
            assert record["privileges"][privilege] is False, (record["object"], privilege)


def test_the_frozen_role_rules_hold_exactly() -> None:
    """H6, H7, H8: the runner, the truth bindings and the verifier."""
    api = _api()
    jobs = _acl_record(api, "table", "experiments.experiment_jobs", "minos_runner")
    assert jobs["privileges"]["SELECT"] is True
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert jobs["privileges"][privilege] is False
    readers = {
        r["principal"]
        for r in api["acl"]["records"]
        if r["object_type"] == "table"
        and r["object"] == "evaluation.truth_bindings"
        and r["privileges"]["SELECT"]
    }
    assert readers == {"minos_evaluator", "minos_owner"}
    for record in api["acl"]["records"]:
        if record["object_type"] == "table" and record["principal"] == "minos_verifier":
            assert not any(
                record["privileges"][p] for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
            ), record["object"]


def test_the_validator_detects_a_privilege_granted_to_public(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        record = next(
            r
            for r in document["acl"]["records"]
            if r["principal"] == "PUBLIC" and r["object_type"] == "table"
        )
        record["privileges"]["SELECT"] = True

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("PUBLIC holds ['SELECT']" in p for p in problems), problems


def test_the_validator_detects_a_ddl_privilege_on_a_runtime_role(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        record = next(
            r
            for r in document["acl"]["records"]
            if r["principal"] == "minos_runner" and r["object_type"] == "table"
        )
        record["privileges"]["TRUNCATE"] = True

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("that is a DDL privilege" in p for p in problems), problems


def test_the_validator_detects_a_duplicate_privilege_record(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["acl"]["records"].append(dict(document["acl"]["records"][0]))

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("privilege records - the matrix is ambiguous" in p for p in problems), problems


def test_the_validator_detects_an_acl_object_that_does_not_resolve(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["acl"]["objects"]["tables"].append("catalog.no_such_table")

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("ACL names an undeclared table" in p for p in problems), problems


def test_the_validator_detects_a_truth_binding_leak(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        record = next(
            r
            for r in document["acl"]["records"]
            if r["object"] == "evaluation.truth_bindings" and r["principal"] == "minos_verifier"
        )
        record["privileges"]["SELECT"] = True

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("may not read evaluation.truth_bindings" in p for p in problems), problems


# --- K17/K18: role provisioning ---------------------------------------------------------------- #
def test_migration_0009_preflights_roles_and_creates_none() -> None:
    """17, and D2 B1: every check runs before a transaction-scoped elevation."""
    provisioning = _api()["role_provisioning"]
    assert provisioning["roles_created_by_0009"] == []
    assert provisioning["roles_altered_by_0009"] == []
    assert provisioning["roles_dropped_by_0009"] == []
    assert set(provisioning["required_roles"]) == set(audit.REQUIRED_ROLES)
    assert "raises BEFORE the first CREATE, ALTER or GRANT" in provisioning["failure_mode"]
    assert provisioning["scratch_test_rule"]
    assert provisioning["operational_provisioning"]
    order = provisioning["preflight_order"]
    elevate = next(i for i, step in enumerate(order) if audit.ELEVATION_STATEMENT in step)
    create = next(i for i, step in enumerate(order) if step.startswith("only then: CREATE SCHEMA"))
    for fragment in audit.PREFLIGHT_CHECKS_BEFORE_ELEVATION:
        assert next(i for i, step in enumerate(order) if fragment in step) < elevate, fragment
    assert elevate < create
    assert not any("SET ROLE" in step and "SET LOCAL ROLE" not in step for step in order)
    elevation = provisioning["elevation"]
    assert elevation["statement"] == "SET LOCAL ROLE minos_owner"
    assert elevation["leaks_after_commit"] is False
    assert elevation["leaks_after_rollback"] is False
    assert elevation["manual_reset_issued"] is False


def test_the_d2_physical_acl_is_scoped_to_new_objects_only() -> None:
    """D2 B2: 780 records over 78 shadow objects; nothing shared, V1 or database-level."""
    api = _api()
    d2 = api["d2_physical_acl"]
    assert d2["counts"] == {
        "functions": 34,
        "objects": 78,
        "principals": 10,
        "records": 780,
        "schemas": 7,
        "tables": 37,
    }
    assert api["acl"]["applies_at"] == "after cutover"
    for record in d2["records"]:
        assert record["object"].startswith("dbv2_"), record["object"]
    forbidden = " | ".join(d2["forbidden_statements"])
    for statement in audit.D2_FORBIDDEN_ACL_TARGETS:
        assert statement in forbidden, statement


def test_a_downgrade_never_drops_a_cluster_role() -> None:
    """18."""
    assert "NEVER drops" in _api()["role_provisioning"]["downgrade_rule"]


def test_no_credential_appears_anywhere_in_the_api_contract() -> None:
    """I6: the report carries role names, never authentication material."""
    text = API_REPORT.read_text(encoding="utf-8")
    for pattern in (r"postgresql(\+\w+)?://[^\s\"]*:[^\s\"@]*@", r"(?i)\bpassword\b\s*[:=]\s*\S"):
        assert re.search(pattern, text) is None, pattern


def test_the_validator_detects_a_role_created_by_0009(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["role_provisioning"]["roles_created_by_0009"] = ["minos_runner"]

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("must create no cluster role" in p for p in problems), problems


def test_the_validator_detects_a_downgrade_that_drops_roles(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)

    def _break(document: dict[str, Any]) -> None:
        document["role_provisioning"]["downgrade_rule"] = "the downgrade drops the cluster roles"

    _break_api(root, _break)
    problems = _validate_root(root)
    assert any("never drop a cluster role" in p for p in problems), problems


# --- K20/K21/K22/K23 ------------------------------------------------------------------------- #
def test_the_database_api_hash_recomputes_and_is_pinned_by_both_contracts() -> None:
    """22: the API document is the leaf of a one-way hash graph."""
    api, contract, physical = _api(), _contract(), _physical()
    assert api[audit.CONTRACT_HASH_FIELD] == audit.contract_hash(api)
    assert contract["database_api"][audit.CONTRACT_HASH_FIELD] == api[audit.CONTRACT_HASH_FIELD]
    assert physical["database_api"][audit.CONTRACT_HASH_FIELD] == api[audit.CONTRACT_HASH_FIELD]
    assert audit.CONTRACT_HASH_FIELD not in json.dumps(api["designed_against"])


def test_the_validator_detects_a_stale_pinned_api_hash(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    path = root / "reports" / "database" / "MINOS_DATABASE_V2_CONTRACT.json"

    def _break(document: dict[str, Any]) -> None:
        document["database_api"][audit.CONTRACT_HASH_FIELD] = "0" * 64
        document[audit.CONTRACT_HASH_FIELD] = audit.contract_hash(document)

    _rewrite(path, _break)
    problems = _validate_root(root)
    assert any("pins a stale database API hash" in p for p in problems), problems


def test_the_validator_detects_a_missing_database_api_report(tmp_path: Path) -> None:
    """21, 22 depend on the report being present at all."""
    root = _copy_root(tmp_path)
    (root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json").unlink()
    problems = _validate_root(root)
    assert any("MINOS_DATABASE_V2_DATABASE_API" in p for p in problems), problems


def test_the_validator_detects_a_duplicate_key_in_the_api_report(tmp_path: Path) -> None:
    """21."""
    root = _copy_root(tmp_path)
    path = root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json"
    path.write_text('{"functions": [], "functions": []}', encoding="utf-8")
    problems = _validate_root(root)
    assert any("duplicate" in p for p in problems), problems


def test_the_d1_4_hashes_differ_from_the_d1_3_hashes() -> None:
    """The contract really changed: neither frozen D1.3 hash survives."""
    assert _contract()[audit.CONTRACT_HASH_FIELD] != (
        "20f8b6eaa19622c2fff7bcc67c9e58b1f4667dc90795c9c2f4fa18efcb6020ba"
    )
    assert _physical()[audit.CONTRACT_HASH_FIELD] != (
        "9611245a6bd9a4fd2bad7f73c44e6ec2cdc4b62974b6faa3f8ff40620854d61b"
    )
