"""§4: importing l2f_tables must NOT perturb the accepted L2-B DB-READY fingerprint."""

from __future__ import annotations


def test_l2f_tables_do_not_change_base_metadata_or_fingerprint() -> None:
    from minos_engine.storage.fingerprint import (
        constraint_names,
        index_names,
        storage_schema_hash,
        table_names,
    )
    from minos_engine.storage.roles import role_policy_hash

    before = {
        "schema_hash": storage_schema_hash(),
        "role_policy": role_policy_hash(),
        "tables": table_names(),
        "constraints": constraint_names(),
        "indexes": index_names(),
    }
    # accepted L2-B baseline (recorded)
    assert (
        before["schema_hash"] == "4508728723ef44a5a4e27013b2cb550240614dc1a9a7a451684cb90c877b5977"
    )
    assert (
        before["role_policy"] == "1dfe6e56fdc6d0c884d0f0f4cf0914f307e89f803f708cd2e9088538d5aabf5f"
    )

    import minos_engine.storage.l2f_tables as L  # noqa: F401  (import triggers table creation)

    after = {
        "schema_hash": storage_schema_hash(),
        "role_policy": role_policy_hash(),
        "tables": table_names(),
        "constraints": constraint_names(),
        "indexes": index_names(),
    }
    assert after == before  # nothing about Base.metadata / fingerprint changed
    assert not any("l2f" in t for t in table_names())  # no L2-F table entered Base.metadata


def test_l2f_owned_and_stub_collections() -> None:
    import minos_engine.storage.l2f_tables as L

    assert [t.name for t in L.L2F_OWNED_TABLES] == [
        "l2f_experiment_plans",
        "l2f_experiment_plan_members",
        "l2f_config_payloads",
        "l2f_experiment_plan_configs",
        "l2f_experiment_jobs",
    ]
    assert len(L.L2F_EXTERNAL_TARGET_STUBS) == 6
    # stubs are metadata-only and clearly tagged; owned tables are not stubs
    assert all(t.info.get("l2f_external_target_stub") for t in L.L2F_EXTERNAL_TARGET_STUBS)
    assert all(not t.info.get("l2f_external_target_stub") for t in L.L2F_OWNED_TABLES)
    # every owned table is in the experiments schema
    assert all(t.schema == "experiments" for t in L.L2F_OWNED_TABLES)
