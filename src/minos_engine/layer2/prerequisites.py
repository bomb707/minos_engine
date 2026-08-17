"""Authoritative, repository-owned accepted Layer 1 prerequisite identities.

This module is the **single source of truth** for the exact identities Layer 2
requires before it may run. The values are pinned constants — there are:

  * no environment-variable overrides;
  * no CLI overrides;
  * no caller-supplied replacement identities;
  * no network lookups;
  * no timestamps in any identity.

The entry-gate verifier compares the committed ``l1-ready.json`` and the actual
git history against :data:`ACCEPTED`. Accepted identities are never inferred from
whatever gate happens to be present on disk — a locally regenerated gate can
never authorize Layer 2.

Update procedure (owner-only):
  A change to any pinned value is an explicit owner acceptance decision. It
  requires (1) a new Layer 1 qualification producing a new PASS ``l1-ready.json``,
  (2) an owner acceptance record analogous to
  ``reports/LAYER1_FINAL_ACCEPTANCE_DECISION.md``, and (3) editing the constants
  below in a reviewed commit. Do not edit these values to make a failing gate
  pass; that inverts the trust relationship this module exists to enforce.
"""

from __future__ import annotations

from .contracts import AcceptedPrerequisiteIdentity

__all__ = [
    "L1_READY_GATE_HASH",
    "PROTOCOL_READY_GATE_HASH",
    "TWIN_READY_GATE_HASH",
    "LAYER1_SCHEMA_HASH",
    "PROFILER_CONFIG_HASH",
    "PROFILER_VERSION",
    "QUALIFIED_SOURCE_COMMIT",
    "QUALIFIED_SOURCE_TREE",
    "ARTIFACT_COMMIT",
    "ARTIFACT_TREE",
    "V2_FRAMEWORK_COMMIT",
    "V2_EVIDENCE_COMMIT",
    "OWNER_ACCEPTANCE_COMMIT",
    "ACCEPTED",
]

# Accepted gate identities (SHA-256, 64 hex).
L1_READY_GATE_HASH = "aeabfea898edd09f68dbe5662b9aebe9dc87d69c97a10b7c8fb3e9d913b5ef5b"
PROTOCOL_READY_GATE_HASH = "b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675"
TWIN_READY_GATE_HASH = "3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a"

# Accepted Layer 1 identity bindings (SHA-256, 64 hex) recorded inside the gate.
LAYER1_SCHEMA_HASH = "cbb6efb28ad2c6a407c0658d0f2313df5b3cbae2cf0bbd364053aff62ac457a9"
PROFILER_CONFIG_HASH = "d01b8e7a9da8e31adad1b9cba17230506771a885256a67ec2e96c74a13c07670"
PROFILER_VERSION = "layer1-profiler-v1"

# Accepted git history (40-hex object ids).
QUALIFIED_SOURCE_COMMIT = "743c9d9f203c485010db2fa683b5767187fe62b0"
QUALIFIED_SOURCE_TREE = "0d1f827d53e61d66b055d2259ee89134b721344f"
ARTIFACT_COMMIT = "ceadf70ba16c044a62585e7fa88bbf47fbfefae1"
ARTIFACT_TREE = "b5d5f5cbe5ef53a59a2874d54b39460dbdbe970a"
V2_FRAMEWORK_COMMIT = "fe0c2d116e4e4771dbe51dbc3193b7626fa39e89"
V2_EVIDENCE_COMMIT = "fa2a7696a497254fd38251072eb39a278ff24d4d"
OWNER_ACCEPTANCE_COMMIT = "f96ea78e0943e33f751afe2eb1709512445e9437"

#: The single frozen accepted-identity contract used by the entry-gate verifier.
ACCEPTED = AcceptedPrerequisiteIdentity(
    l1_gate_hash=L1_READY_GATE_HASH,
    protocol_gate_hash=PROTOCOL_READY_GATE_HASH,
    twin_gate_hash=TWIN_READY_GATE_HASH,
    layer1_schema_hash=LAYER1_SCHEMA_HASH,
    profiler_config_hash=PROFILER_CONFIG_HASH,
    profiler_version=PROFILER_VERSION,
    qualified_source_commit=QUALIFIED_SOURCE_COMMIT,
    qualified_source_tree=QUALIFIED_SOURCE_TREE,
    artifact_commit=ARTIFACT_COMMIT,
    artifact_tree=ARTIFACT_TREE,
    v2_framework_commit=V2_FRAMEWORK_COMMIT,
    v2_evidence_commit=V2_EVIDENCE_COMMIT,
    owner_commit=OWNER_ACCEPTANCE_COMMIT,
)
