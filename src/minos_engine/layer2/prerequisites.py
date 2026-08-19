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
    "L2A_CLOSURE_SOURCE_COMMIT",
    "L2A_CLOSURE_SOURCE_TREE",
    "L2A_CLOSURE_EVIDENCE_COMMIT",
    "L2A_CLOSURE_EVIDENCE_TREE",
    "ACCEPTED_FEATURE_REGISTRY_HASH",
    "DB_READY_GATE_HASH",
    "DB_READY_SOURCE_COMMIT",
    "DB_READY_SOURCE_TREE",
    "DB_READY_EVIDENCE_COMMIT",
    "SPLIT_FROZEN_GATE_HASH",
    "SPLIT_FROZEN_SOURCE_COMMIT",
    "SPLIT_FROZEN_SOURCE_TREE",
    "SPLIT_FROZEN_EVIDENCE_COMMIT",
    "SPLIT_FROZEN_V2_GATE_HASH",
    "SPLIT_FROZEN_V2_SOURCE_COMMIT",
    "SPLIT_FROZEN_V2_SOURCE_TREE",
    "SPLIT_FROZEN_V2_EVIDENCE_COMMIT",
    "INGEST_READY_GATE_HASH",
    "INGEST_READY_SOURCE_COMMIT",
    "INGEST_READY_SOURCE_TREE",
    "INGEST_READY_EVIDENCE_COMMIT",
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

# Final accepted L2-A closure identities (the authoritative accepted Layer 1/L2-A
# state that downstream stages bind to; supersedes the earlier owner/audit commit).
L2A_CLOSURE_SOURCE_COMMIT = "c2ceed0cd8566442ca229eaa41d9a096c0b4ccea"
L2A_CLOSURE_SOURCE_TREE = "e581ff76223210895ceff1521dabba751de72f9a"
L2A_CLOSURE_EVIDENCE_COMMIT = "70d08daa7a5fce76ca347e1635507757ef792c88"
L2A_CLOSURE_EVIDENCE_TREE = "eadb6d6f19d99a1f20de7bfb231771d832dc6114"
ACCEPTED_FEATURE_REGISTRY_HASH = "0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c"

# Accepted L2-B DB-READY closure identities (bound by the L2-C SPLIT-FROZEN gate; the
# L2-C qualified source must properly descend the DB-READY evidence commit). A change
# to any of these is an explicit owner acceptance decision (see update procedure above).
DB_READY_GATE_HASH = "259986a0423a1b8317bb4c6b1a1cb9213708444a8a6764fc8c7703cf80499698"
DB_READY_SOURCE_COMMIT = "695901ee95c529acf8a434c1babe06f364efa790"
DB_READY_SOURCE_TREE = "462106b2a98a5c00ca49faa771531d2730b435da"
DB_READY_EVIDENCE_COMMIT = "2df03a2cdf37b8c83d34d3e0347ba06a7159310d"

# Accepted L2-C SPLIT-FROZEN (v1) closure identities. The v2 epoched split supersedes v1
# *within* stage L2-C: the SPLIT-FROZEN-v2 gate binds these accepted v1 identities and its
# qualified source must properly descend the v1 SPLIT-FROZEN evidence commit. v1 stays
# byte-identical and historical (its gate/manifest/migration are never modified). A change
# to any of these is an explicit owner acceptance decision (see update procedure above).
#   * source commit (Commit Y): the v1 verifier-closure source (inventory-path identity).
#   * evidence commit: adds gates/split-frozen.json + the v1 final closure report only.
SPLIT_FROZEN_GATE_HASH = "5520328868f408fe705a9d6618e3d67c081fa4e0aaa8dd764bb933aea866c702"
SPLIT_FROZEN_SOURCE_COMMIT = "5ff8c361acc19613f0db7e4f93f88fe4aab9bfd5"
SPLIT_FROZEN_SOURCE_TREE = "49b49c53137528da309ebb39ee3a9e456f6ead4a"
SPLIT_FROZEN_EVIDENCE_COMMIT = "b03ac174672a70c360f6678ca28e324b49852c26"

# Accepted L2-C SPLIT-FROZEN-V2 (epoched split) closure identities — the corrected
# closure (exact v1 inheritance, zero transitions) merged to dev via PR #1 and accepted
# by the owner. L2-D (INGEST-READY) binds these and its qualified source must properly
# descend the SPLIT-FROZEN-V2 evidence commit. A change to any of these is an explicit
# owner acceptance decision (see update procedure above).
#   * source commit (Commit Y): the corrected v2 source (inheritance + sealed test).
#   * evidence commit (Commit Z): adds gates/split-frozen-v2.json + the closure report.
SPLIT_FROZEN_V2_GATE_HASH = "6bd9f472720d56055e57ada0a6e955a8ab0b617a0fe849021a5b0ddfafd19392"
SPLIT_FROZEN_V2_SOURCE_COMMIT = "8c641dd1363573ab685df49540561cfe818de17c"
SPLIT_FROZEN_V2_SOURCE_TREE = "5d6569801e5e75aa398c3f0f835d3d189c506eee"
SPLIT_FROZEN_V2_EVIDENCE_COMMIT = "a8940ac44eef72cbcbdc8f943a163e33f3a3b742"

# Accepted L2-D INGEST-READY closure identities (capability gate; per-epoch
# PROFILE-SNAPSHOT-FROZEN evidence is separate). Owner-accepted after the corrective
# review rounds; CI green on both commits. A change to any of these is an explicit
# owner acceptance decision (see update procedure above).
INGEST_READY_GATE_HASH = "91f55da0bfe4df8620508ddb9566a0fd9ed838ca1beb2d2522bcb655d8061599"
INGEST_READY_SOURCE_COMMIT = "87835a99918812172343eabb7a1e8037e317eaec"
INGEST_READY_SOURCE_TREE = "06e8f6ab9832f382624c06b986f207cc75810247"
INGEST_READY_EVIDENCE_COMMIT = "5ed620a6371f771be2cfead8caeb712bf4701121"

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
