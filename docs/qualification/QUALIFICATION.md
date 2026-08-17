# Stage-Gate Qualification Procedure

This document defines how the PROTOCOL-READY gate is produced and verified, the
exact (non-cyclic) hashing procedure, and the integrity-vs-promotion split.

## Stage 1 (TWIN-READY) qualification

TWIN-READY reuses the git-tree-bound, two-commit machinery with Stage-1 specifics:

- **Accepted prerequisite (pinned).** `twin/prerequisites.py` pins the accepted
  Stage 0 identity (gate hash `b9cda0ba…`, source `4a5a14d…`, tree `f248480…`).
  Verification returns three *separate* results — `protocol_ready_identity_accepted`,
  `protocol_ready_evidence_verified` (Stage 0 evidence rehashed from the qualified
  commit), and `protocol_ready_promotion_authorized`. A different, locally
  regenerated PROTOCOL-READY gate can never authorize Stage 1. Fails closed if the
  repository, accepted commit, tree, evidence, or identity is unavailable.
- **Stage 1 evidence set** (`twin_runner.STAGE1_EVIDENCE`): `src/minos_engine`,
  `schemas`, `configs`, `tests`, `docs`, `.github/workflows/ci.yml`,
  `pyproject.toml`, `Makefile`, the Stage 1 preimplementation audit, and the
  prerequisite `gates/protocol-ready.json` — hashed from the qualified Commit A
  tree (`git ls-tree` + `git cat-file`). It **excludes** `gates/twin-ready.json`
  and the Stage 1 report (no circular evidence).
- **Coherent source integrity**: evidence completeness, all Stage 1 required
  files tracked, all required working-tree files equal to the committed blob,
  qualified commit/tree present, worktree clean — one `SourceIntegrity` result.
- **Qualifier identity**: `stage1-twin-qualifier-v1` (distinct from Stage 0).
- **Non-mutating check mode**: `minos-engine twin qualify --check` and
  `minos-engine twin gate require-pass` verify a committed TWIN-READY gate
  (integrity, required checks, Stage 1 evidence from Commit A, qualifier identity,
  accepted prerequisite + its evidence, promotion, and Commit-B-descends-Commit-A)
  without regenerating artifacts. CI runs both, so a tampered gate fails CI.
- **Evidence rehashing** (Stage 0 and Stage 1) enumerates files from the *tree of
  the qualified ref* (`git ls-tree <ref>`), so digests reproduce from the
  qualified commit regardless of the current HEAD.
- **Runtime policy**: CPython **3.12.x** is the only supported/qualified runtime
  (matches the subnet). `python_runtime_is_3_12` is a mandatory TWIN-READY check;
  the gate records `python_runtime: CPython 3.12` (no patch level in the hash).
- **Git-history preflight**: because qualification verifies historical commits,
  trees, and blobs, CI checks out **full history** (`fetch-depth: 0`).
  `minos-engine git-history check --protocol-gate … --twin-gate … --base-dir .`
  runs before the tests and fails closed with distinct reason codes
  (`GIT_HISTORY_INCOMPLETE`, `QUALIFIED_COMMIT_UNAVAILABLE`,
  `QUALIFIED_TREE_UNAVAILABLE`, `QUALIFIED_TREE_MISMATCH`, `EVIDENCE_PATH_MISSING`,
  `EVIDENCE_HASH_MISMATCH`) — a missing historical commit (shallow clone) is never
  reported as an untracked evidence file.

## Layer 1 (L1-READY) qualification

L1-READY reuses the git-tree-bound, two-commit machinery with Layer-1 specifics:

- **Accepted prerequisites (pinned).** `layer1/prerequisites.py` pins the accepted
  TWIN-READY identity (gate hash `3464fb76…`, source `e9263ef…`, tree `f84c0661…`)
  and returns three separate results; the accepted PROTOCOL-READY prerequisite is
  reused from Stage 1. A locally-regenerated TWIN-READY gate can never authorize
  Layer 1.
- **Two-tier qualification.** `synthetic_ci_qualified` (mandatory pysam fixtures in
  CI) and `real_bam_qualified` (the committed `reports/LAYER1_REAL_BAM_REPORT.json`).
  The gate cannot PASS with `layer1_real_bam_qualified=false` — a missing real BAM
  yields HOLD, never a false PASS.
- **Qualifier identity**: `layer1-qualifier-v1`. The gate records the profile schema
  hash, profiler config hash, profiler version, both prerequisite gate hashes, the
  real-BAM integration-report hash, and the synthetic fixture identity.
- **Report/gate ordering.** The report is written first; the gate records the
  report's sha256 (checked by the Layer 2 entry gate) and the report does not embed
  the gate hash (no cycle). Evidence is the Commit-A Layer 1 source tree; the report
  and gate are Commit B and are not evidence.
- **Non-mutating check mode**: `minos-engine layer1 qualify --check` and
  `minos-engine layer1 gate require-pass` verify a committed L1-READY gate
  (integrity, required checks, Commit-A evidence, qualifier identity, both accepted
  prerequisites, promotion, and Commit-B-descends-Commit-A) without regenerating it.

## Two-commit qualification (provenance)

The gate must attest the *complete* source tree it qualifies. To avoid a gate
that attests a tree that does not yet contain the gate, qualification uses two
commits:

1. **Commit A — qualifiable source.** All source, tests, schemas, configs,
   docs, CI, specifications, and the gate-building code. The worktree is clean.
   Its commit SHA is `qualified_source_git_sha`; its tree object SHA is
   `qualified_source_tree_sha`.
2. **Run qualification** against exactly Commit A (`minos-engine qualify` or
   `scripts/build_protocol_ready_gate.py`). The gate records
   `qualified_source_git_sha`, `qualified_source_tree_sha`, and
   `qualification_tool_version`, and the mandatory check `qualified_source_clean`
   reflects the clean worktree.
3. **Commit B — qualification artifacts.** Only
   `reports/STAGE0_QUALIFICATION_REPORT.md` and `gates/protocol-ready.json`.
   Commit B's parent is Commit A, so the verifier can confirm
   `parent(HEAD) == qualified_source_git_sha`.

## Non-cyclic hashing

- The **gate hash** is `sha256(canonical_json(gate))` excluding `gate_hash`
  itself and `created_at`.
- **Evidence** hashes cover the Commit-A source tree only (see below). The gate
  never hashes the qualification report or the gate file, so there is **no
  cycle**.
- The **report** may reference the gate hash because the report is not part of
  the gate's evidence.

### Evidence set (hashed)
`reports/STAGE0_PREIMPLEMENTATION_AUDIT.md`, `schemas/`, `configs/`,
`src/minos_engine/`, `tests/`, `docs/` (incl. `docs/specifications/` with the
spec `.docx` and manifest), `docs/specifications/SPECIFICATION_MANIFEST.json`,
`pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`.

### File vs directory digest
- File: `sha256(exact bytes)` of the committed blob.
- Directory: enumerate the **tracked** files under it (`git ls-files`), normalize
  paths POSIX-relative, sort, then hash the canonical list
  `[{"path","sha256","size_bytes"}, ...]`. Untracked/ignored files are excluded.

## Tracked-source evidence rule (git-tree-bound)

A PASS qualification must never depend on ignored, untracked, or working-tree
drifted content — only on what is committed in the qualified commit. This closes
the defect where an ignored+untracked `configs/runtime/gatk_only.yaml` was hashed
locally and produced a PASS, while a fresh CI clone lacked the file and failed.

- Evidence is hashed from committed blobs (`git cat-file blob <commit>:<path>`),
  so untracked/ignored/drifted files are ineligible.
- Directory evidence enumerates `git ls-files` entries only.
- `required_source_tracked` fails the gate if any required file (configs incl.
  `configs/runtime/gatk_only.yaml`, schemas, fixtures, specs, workflow, build
  files, audit) is absent from `git ls-files`.
- `worktree_matches_head` fails the gate if any required working-tree file
  differs from its committed blob.
- If `root` is not a git repository, everything fails closed.
- The verifier re-hashes a git-bound gate's evidence from
  `qualified_source_git_sha`, so drifted working-tree content cannot satisfy
  verification. The recorded `qualified_source_tree_sha` and the evidence
  therefore describe the same committed source state.

## Required-check registry

`gates/required_checks.py` maps each gate name to the exact set of checks a PASS
must contain and set true. A PASS gate for a registered name cannot be built
from an arbitrary dictionary: every required check must be present and true;
unknown checks may be retained as supplemental but none may be false.
`evidence` items must all carry a non-null `sha256` for a PASS gate, and source
provenance must be present.

## Test & coverage accounting

Test results come from JUnit XML (`pytest --junitxml`), never terminal text
(`-q` can suppress the summary). A PASS requires `tests_collected > 0`,
`failures == 0`, `errors == 0`, and `exit_code == 0`. Coverage comes from the
Cobertura XML (`--cov-report=xml`); the gate enforces line coverage
≥ 90% (`coverage_threshold_met`). An unreadable report fails the check.

## Integrity vs promotion

- `verify_gate_integrity` / `minos-engine gate verify-integrity` (and the alias
  `gate verify`): schema valid, canonical hash matches, and (with `--base-dir`)
  every evidence hash matches. A structurally valid HOLD/REJECT **passes
  integrity**.
- `require_gate_pass` / `minos-engine gate require-pass`: integrity holds AND
  `status == PASS` AND the required-check set is present and true. Only this
  authorizes promotion; a HOLD/REJECT gate returns a non-zero exit code.

## L1-READY entry gate

`layer2/entry_gate.verify_l1_ready` verifies the full chain for a future
`l1-ready.json`: schema, canonical hash, PASS, `gate_name == "L1-READY"`, the
complete required-check set, report existence + SHA-256 match, Layer 1 schema
hash, profiler config hash, profiler version, qualified-source compatibility,
and every evidence file's existence + hash. No legitimate L1-READY exists yet,
so Layer 2 remains blocked.
