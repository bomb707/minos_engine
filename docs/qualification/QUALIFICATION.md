# Stage-Gate Qualification Procedure

This document defines how the PROTOCOL-READY gate is produced and verified, the
exact (non-cyclic) hashing procedure, and the integrity-vs-promotion split.

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
- File: `sha256(exact bytes)`.
- Directory: enumerate regular files (excluding `__pycache__`, `*.pyc`, caches,
  `.git`, `.venv`), normalize paths POSIX-relative, sort, then hash the
  canonical list `[{"path","sha256","size_bytes"}, ...]`.

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
