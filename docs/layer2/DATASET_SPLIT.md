# Layer 2 — L2-C Dataset Registry & Deterministic 50/10/15 Split

L2-C establishes a **leakage-resistant, reproducible dataset-split foundation** only.
It freezes an immutable dataset registry and a deterministic train/validation/test
split over the 75 complete practice samples. It does **not** implement profiling
ingestion, feature ingestion, experiments, Optuna/SMAC, model training, prediction,
controller logic, configuration selection, or feedback (later stages).
`Layer2Service.select_config` remains blocked (`StageNotReadyError`).

## 1. Discovered corpus layout
The external practice corpus (never committed — see `.gitignore`) is discovered under a
dataset root with this exact layout:

```
<dataset-root>/
  practice/round_<round_id>/input.bam         # single-contig aligned reads
  practice/round_<round_id>/input.bam.bai      # BAM index (required)
  practice/round_<round_id>/truth.vcf.gz{,.tbi}      # truth  — NEVER read by L2-C
  practice/round_<round_id>/mutations.vcf.gz{,.tbi}  # labels — NEVER read by L2-C
  reference/<chrom>/<chrom>.fa                 # per-chromosome reference FASTA
  reference/<chrom>/<chrom>.fa.fai             # reference index
```

`round_id` is the bare lowercase-hex directory suffix (`round_<round_id>`), matching the
engine-wide Layer 1 `ProfileRequest.round_id` convention. The confirmed corpus contains
**exactly 75** complete samples — **15 per chromosome** for CHR18–CHR22 — each a
single-`@SQ` BAM whose contig length equals the reference FAI length. Discovery fails
closed on any deviation (see §7).

## 2. Dataset identity
Each sample's immutable identity record (`SampleIdentity`) contains:
`dataset_id` (`minos-<chrom>-<round_id>`, stable and path-independent), `round_id`,
`chromosome`, normalized `region_*` (full contig `[0, contig_length)` zero-based
half-open, derived from the BAM `@SQ` cross-checked to the FAI), `region_hash`,
`bam_sha256`, `bai_sha256`, `reference_sha256`, `fai_sha256`, `bam_size_bytes`,
`parameter_space_hash` (bound GATK documented parameter space), `feature_registry_hash`
(accepted `0d861270…`), `identity_tuple_hash` (over `bam/bai/reference/fai/region`),
`split_algorithm_version`, `split_salt`, `allocation_digest`, `partition`, `sort_order`.

No machine-specific absolute paths and no truth/mutation information appear in the
canonical manifest. Operational paths live only in the separate local input inventory.

## 3. Canonical serialization & hashing
The single canonicalizer (`common/canonical_json`) is used everywhere: UTF-8, keys
sorted lexicographically, compact `(",", ":")` separators, integers preserved, NaN/Inf
rejected. Rules:
- **`region_hash`** = `sha256(canonical{contig,start0,end0_exclusive,length_bp,coordinate_system})`.
- **`identity_tuple_hash`** = `sha256(canonical{bam,bai,reference,fai,region_hash})`.
- **`dataset_registry_hash`** = `sha256` over the `dataset_id`-sorted list of identity
  projections (partition/sort_order excluded).
- **`split_policy_hash`** = `sha256` over the fixed policy description (§4).
- **`manifest_hash`** = `sha256` over the canonical manifest content with `manifest_hash`
  itself excluded. The manifest contains **no** timestamp or git sha, so identical
  inputs regenerate **byte-identical** output regardless of enumeration order, CWD,
  dataset-root path, locale, timezone, or Python hash randomization.

## 4. Deterministic split algorithm
```
SALT = "minos-l2-split-v1"                       # changing it defines a NEW split identity
group complete samples by confirmed contig       # each chromosome has exactly 15
for each chromosome (after the contig is confirmed):
    digest(round_id) = sha256(f"{SALT}:{round_id}").hexdigest()      # lowercase hex
    order = sort(round_ids, key=digest)           # bytewise lowercase-hex ordering
    train      = order[0:10]
    validation = order[10:12]
    test       = order[12:15]
```
Bytewise lowercase-hex ordering is identical to numeric ordering of the 64-hex digest.
Totals: **train 50 / validation 10 / test 15** (10/2/3 per chromosome). The assignment
is a pure function of `{SALT, round_id, contig}` — unique, disjoint, deterministic, and
independent of filesystem order, wall-clock, and any truth/mutation/score/profile. The
split is never chosen for favorability.

## 5. Leakage & truth/mutation isolation
- Truth and mutation files are **never read, hashed, or referenced** during discovery or
  generation, and never appear in the canonical manifest or local inventory.
- If truth/mutation cryptographic identities must be recorded for later offline
  evaluation, they are stored **only** in `evaluation.dataset_evaluation_identity`
  (evaluation-schema, evaluator/admin only) — never exposed to trainer/runner/live/
  feature/controller roles.
- The feature-eligibility and leakage policy is unchanged from
  `reports/LAYER2_DATASET_SPLIT_POLICY.md` (coordinates/labels/round-id excluded as
  features; all transformations fit on the 50 training samples only; the 15 test samples
  stay locked until the final evaluation).

## 6. Database tables, constraints & role-access matrix
The immutable migration `0002_l2c_dataset_split` (self-contained, no ORM metadata) adds,
all owned by `minos_admin`:

| Object | Purpose |
|---|---|
| `catalog.dataset_registry` | one immutable identity row per sample |
| `catalog.split_allocations` | one immutable partition row per dataset (FK → registry) |
| `evaluation.dataset_evaluation_identity` | evaluator-only truth/mutation crypto ids |
| `catalog.training_dataset_allocations` (view) | train rows, minimal fields |
| `evaluation.locked_allocations` (view) | validation+test rows, minimal fields |

Constraints: unique `dataset_id`; unique `identity_tuple_hash`; unique
`(chromosome, round_id)`; unique `(bam,bai,reference,fai,region_hash)`; **unique
`split_allocations.dataset_registry_id`** (exactly one partition per dataset ⇒ split
overlap impossible); `partition ∈ {train,validation,test}`; region-bound and hex-format
CHECKs; append-only triggers reject UPDATE/DELETE (identity + partition immutable).

**SQL-level partition isolation** (enforced by GRANTs/views, not just Python):

| Role | training view | locked (val/test) view | base allocation tables | eval identity |
|---|---|---|---|---|
| `minos_trainer` | **SELECT** | denied | denied | denied |
| `minos_evaluator` | denied | **SELECT** | denied | **SELECT/INSERT** |
| `minos_live` | denied | denied | denied | denied |
| `minos_runner` | denied | denied | denied | denied |
| `minos_admin` | owner | owner | owner | owner |

Application roles have **no direct grant** on the base tables, so direct base-table
access cannot bypass the restricted, owner-defined views. Verified by connecting as each
real role in `tests/integration/layer2_split/test_role_isolation.py`.

## 7. Fail-closed discovery
Hard failures (never silently skipped): missing BAM/BAI/FASTA/FAI, empty files,
unreadable/multi-contig BAMs, `@SQ`/FAI length mismatch, unsupported chromosomes,
duplicate dataset/round ids, duplicate identity tuples, malformed regions, symlink escape
of the dataset root, path traversal, a file changing size during hashing, and wrong
per-chromosome or total counts. BAMs are streamed (never loaded into memory).

## 8. Migration behavior
`0002` upgrades on top of `0001_l2b_initial`; downgrade removes only L2-C-owned objects
and restores the prior L2-B state (10 tables, 7 schemas, head `0001`). Upgrade → downgrade
→ re-upgrade succeeds on PostgreSQL 16. The L2-B DB-READY verifier binds the L2-B
revision as the immutable *base* of the lineage (ancestor of the current head), so the
forward migration does not break DB-READY re-verification.

## 9. CLI usage
```bash
minos-engine layer2 split discover  --dataset-root <root>     # confirm corpus (no writes)
minos-engine layer2 split generate  --dataset-root <root>     # write manifest + inventory
minos-engine layer2 split verify    --manifest <file>         # non-mutating verification
minos-engine layer2 split summarize --manifest <file>         # counts / per-chromosome
minos-engine layer2 split qualify [--check]                   # SPLIT-FROZEN qualify/verify
minos-engine layer2 split gate require-pass                   # require committed gate PASS
```

## 10. Reproducibility procedure
Any engineer can regenerate and independently verify the manifest:
1. `minos-engine layer2 split generate --dataset-root <root>` → `manifests/…v1.json`.
2. `minos-engine layer2 split verify --manifest manifests/layer2_dataset_split_v1.json`
   (recomputes every hash, re-derives the split from `{SALT, round_id}`, checks the exact
   50/10/15 counts, disjoint/covering partitions, and absence of truth/mutation fields).
3. Compare `manifest_hash` to the committed manifest — identical inputs give identical
   bytes. `--dataset-root` may differ across machines; the canonical manifest does not.

## 11. Recovery / failure behavior
Generation fails closed on any corpus violation (§7) — no partial manifest is written.
Verification and gate `require-pass`/`qualify --check` return a non-zero exit and a list
of the specific failed checks; nothing is mutated.

## 12. Qualification & gate verification (`SPLIT-FROZEN`)
`minos-engine layer2 split qualify` runs the real PostgreSQL 16 L2-C integration suite
(migration lifecycle, role isolation, immutability, constraints), the full test suite,
coverage, and ruff/format/mypy, then assembles `gates/split-frozen.json` bound to the
qualified source commit/tree. It binds the accepted PROTOCOL/TWIN/L1/**DB-READY**
identities, the DB-READY source/tree/evidence commits, the canonical manifest / dataset
registry / split-policy / generator / L2-C migration hashes, the local input inventory
hash, the Alembic head, the Python/PostgreSQL identities, and the exact 50/10/15 counts.
`--check` verifies non-mutatingly: it recomputes the gate hash, re-hashes source evidence
from the exact qualified commit, proves the L2-C source **properly descends the DB-READY
evidence commit** (rejecting sibling/ancestor/unrelated sources and later merge-HEAD
laundering) and that HEAD descends the L2-C source, re-verifies the committed manifest
bytes and re-derives its content, and rejects duplicate/missing migration evidence and
consistently-tampered hash pairs. No value is trusted merely because it appears in the
gate. See `STORAGE_ARCHITECTURE.md`, `DATABASE_ROLES.md`, and `MIGRATIONS.md`.
