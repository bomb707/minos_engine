# L2-H — Production decision persistence

The Layer 2 specification's live decision algorithm ends with two ordered steps:

```
persist decision + all candidate reasons in one transaction
return exact canonical CONFIG bytes referenced by persisted hash
```

Persistence is not a side effect of returning a config. It is what makes the returned config
legitimate: an operator who later asks "what did the engine decide for round R, and why" must be
answered by the database, not by inference. Until this stage that requirement was met against the
filesystem (`decision_publication`), recorded as
`FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION` — **deferred, never waived**.

This document is what closed the deferral. It does **not** activate the service:
`Layer2Service.select_config` still raises.

---

## 1. What the contract requires

The specification's own DDL is the authority:

```sql
CREATE TABLE runtime.decisions (
 id uuid PRIMARY KEY, round_id text NOT NULL, profile_id uuid NOT NULL,
 controller_version text NOT NULL, model_bundle_id uuid,
 parameter_space_hash char(64) NOT NULL, baseline_config_id uuid NOT NULL,
 selected_config_id uuid NOT NULL, mode text NOT NULL,
 decision_manifest jsonb NOT NULL, decision_hash char(64) UNIQUE NOT NULL,
 decided_at timestamptz NOT NULL, UNIQUE(round_id, decision_hash)
);
INDEX decisions(round_id, decided_at DESC);
```

`0001_l2b_initial` created a strict subset: `id, round_id, decision_hash, decision_manifest_hash,
config_id, model_bundle_id, profile_id, created_at`. Missing were `mode`, `controller_version`,
`parameter_space_hash`, `baseline_config_id`, `selected_config_id`, `decision_manifest`,
`decided_at`, `NOT NULL` on `profile_id`, the global `UNIQUE(decision_hash)`, and the
`(round_id, decided_at DESC)` index. **A schema change was therefore unavoidable**: the existing
columns cannot carry the required state without giving old names new meanings, which is the one
thing that must not happen to a scientific ledger.

Two columns are added beyond the DDL, both because §J of the contract demands them rather than
because an earlier audit listed them: `requested_mode` and `fallback_reason`. The specification's
`mode` is the *actual* mode; recording only that would lose the distinction between
"SAFE was asked for" and "a contextual mode was asked for and reduced", which is exactly the
statement a SAFE-baseline-only engine has to be able to make.

**Candidate reasons.** Under `SAFE_BASELINE` the candidate set is the singleton `{baseline}` —
capability scope is `SAFE_BASELINE_ONLY` and the accepted controller qualification observed
`candidate_generation_count == 0` across all 200 decisions. The row carries the entire candidate
set (`baseline_config_id` and `selected_config_id`, equal by database CHECK) together with the
manifest recording `guards`, `requested_mode`, `actual_mode` and `fallback_reason`, all in one
transaction. `runtime.decision_candidates` becomes necessary when contextual candidate generation
is authorized; creating it empty now would be schema for a capability the frozen gate lists as
NOT AUTHORIZED.

---

## 2. Migration topology: why a second lineage

| Option | Verdict |
|---|---|
| **A** — no schema change | **Rejected.** Seven required columns do not exist, and reusing `config_id`/`decision_manifest_hash` for them is semantic overloading. |
| **A′** — append `0027` to the main chain | **Rejected.** The operational store sits at `0005_l2e_feature_view`; `0006`–`0026` are all L2-F/L2-F2 scientific campaign migrations. Reaching `0027` means dragging the operational database through twenty-one of them. |
| **A″** — branch label / second head in the main graph | **Rejected.** `alembic upgrade head` becomes ambiguous for every deployment and every test, and the task forbids casually creating a second head. |
| **B** — separate lineage with its own version table | **Chosen.** |

`migrations_runtime/` is an independent Alembic script location tracked in
`runtime.alembic_version_runtime`, driven by `alembic_runtime.ini` or
`storage.runtime_overlay.upgrade_runtime_overlay`. The two chains never reference each other's
revisions. Nothing in `migrations/` is edited, re-parented, merged or stamped; the main chain is
byte-identical and still has exactly one head (`0026_l2f2_phase_d_closure`).

Coupling runs one way and is checked at run time: `r0001_l2h_runtime_decisions` **requires**
`public.alembic_version` to contain exactly `0005_l2e_feature_view` and refuses otherwise. That
single requirement is also what structurally keeps the overlay off the TRAIN (`0020`) and
VALIDATION (`0026`) stores — proven by drilling it against `0001`, `0020` and `0026` and
confirming that nothing was applied. It also requires `runtime.decisions` to be **empty**: several
new columns are `NOT NULL` with no honest backfill, and inventing values for historical decisions
is not an option. That is a statement of fact today — the service has never been activated.

**This is not DB-V2.** DB-V2 was a replacement schema in a replacement database, and it stays
abandoned. This is the same database, the same seven schemas, the same tables, the same accepted
migration chain, and one additive revision that completes **one** table to its own specification
DDL, with an exact downgrade.

---

## 3. Two old columns, neither reinterpreted

`decision_manifest_hash` and `config_id` exist in `0001`, have no counterpart in the
specification's DDL, and had no defined preimage anywhere.

* **`decision_manifest_hash`** is given a meaning it can actually carry: the plain SHA-256 of the
  exact canonical manifest bytes stored in `decision_manifest`. That is a *different quantity*
  from `decision_hash` (the domain-separated scientific decision identity,
  `minos:l2h-safe-decision-manifest:v1`), so the two columns can never be confused, and it makes
  the JSONB round trip checkable: on readback the manifest is re-canonicalized and re-hashed.
* **`config_id`** never had a meaning, so it is **retired**, not quietly promoted to
  `selected_config_id`: `CHECK (config_id IS NULL)`. The config identities live in the two columns
  the contract names.

## 4. The database enforces the bindings

Every typed column that also appears in the manifest is equated to it by a CHECK, so a row whose
columns disagree with its own manifest cannot exist — not merely "is rejected by the writer":

`schema_version`, `round_id`, `actual_mode`, `requested_mode`, `fallback_reason`,
`controller_version`, `parameter_space_hash`. Plus:

* `(requested_mode = mode) = (fallback_reason = 'NONE')` — a mode change must say why;
* `mode = 'SAFE_BASELINE'` ⟹ `model_bundle_id IS NULL` **and**
  `decision_manifest->>'model_bundle_loaded' = 'false'` **and**
  `selected_config_id = baseline_config_id`;
* a contextual mode ⟹ `model_bundle_id IS NOT NULL`.

This store cannot imply that a contextual model executed, whatever a caller does.

## 5. The profile foreign key pointed at a table production never writes

`0001` aimed `profile_id` at `profiling.profiles`. `0004_l2d_profile_ingestion` then built the
real ingestion on `profiling.bam_profiles` — and that is where the operational L1 pipeline puts
profiles: the operational store holds **75 rows** in `profiling.bam_profiles` and **0** in
`profiling.profiles`, and no code path in this engine writes the latter. A live decision could
therefore never have satisfied `profile_id NOT NULL`, not because the development database happens
to be empty but because the referenced table is a superseded L2-B placeholder.

The overlay re-aims the foreign key at `profiling.bam_profiles` and adds `NOT NULL` at the same
time. That is a strengthened FK, not a weakened one. `minos_live` gains `SELECT` on
`profiling.bam_profiles` and `catalog.dataset_registry` — the minimum needed to resolve and
cross-check the owning profile — and nothing else; the downgrade restores both the old FK and the
exact previous grant shape.

## 6. Nothing is trusted from the caller

| | How it is obtained |
|---|---|
| the decision | **made here** from verified capabilities, never accepted as a parameter |
| the config row | resolved from `catalog.gatk_configs` **by accepted config hash**; absent or ambiguous fails closed; the row's `parameter_space_hash` must be the accepted one |
| the profile row | resolved by the **proven** logical `profile_id`, then cross-checked on ten identity fields plus `dataset_id`/`round_id`/`chromosome` reached through the schema's own FK |
| provisioning | `catalog.gatk_configs` is provisioned by an **administrative**, idempotent step that verifies the frozen payload bytes hash to the accepted config hash. `minos_live` holds only SELECT there and must: a live path that could insert catalog rows could insert a config nobody accepted and then decide in favour of it. |
| profiles | never provisioned by this engine. If Layer 1 ingestion has not written the row, persistence **fails closed** and names that production prerequisite. |

`profiling.bam_profiles` also holds VALIDATION and TEST members. It is opened **by name** — a
single equality on the proven `profile_id` — never listed, counted or filtered. The qualification
observes every statement the path issues and derives its isolation checks from that.

`profile_manifest_hash` is deliberately **not** cross-checked: nothing in this engine defines its
preimage (see `Layer1ProfileReference`), and requiring agreement on it would dress an
unauthenticated value up as evidence. `profile_sha256` is checked instead, and is stronger — it is
the hash of the whole profile document.

## 7. Transaction and idempotency semantics

* **Atomic.** One transaction resolves both foreign rows, inserts, and re-reads. Any failure rolls
  it back; a driver-level failure injected between the INSERT and the COMMIT is proven to leave
  nothing visible.
* **Nothing is returned before COMMIT.** After the commit the row is read again from a *second*
  transaction and compared field by field; only then is `VerifiedPersistedSafeDecision` minted. A
  caller cannot construct one from a dict.
* **Idempotency key** `(round_id, decision_hash)`, via `ON CONFLICT DO NOTHING`. Re-persisting the
  identical decision converges onto the identical row, and the **first writer's `decided_at`
  stands** — that is when the decision was made.
* **`decided_at` is server time**, never caller-supplied, and deliberately not part of the
  decision identity: a timestamp in the hash would make a retry mint a new identity and defeat
  convergence.
* **Same identity, different stored state → fail closed.** The readback comparison refuses, and
  the transaction rolls back.
* **One decision identity names one decision, forever**: the contract's global
  `UNIQUE(decision_hash)` refuses re-filing a persisted identity under another round.
* **A round may legally carry more than one decision.** The contract's uniqueness is
  `(round_id, decision_hash)` — not `(round_id)` — and its required index is
  `(round_id, decided_at DESC)`, which only makes sense if a round can have several. They are
  append-only and never overwrite each other; a correction is a superseding row, never an UPDATE.
  In practice the four requested modes produce four distinct, non-conflicting decisions per round.
* **Concurrent identical writers converge** on exactly one row. **Concurrent conflicting writers**
  are deterministic: different identities append; the same identity with different state is
  refused.

## 8. Privileges

`minos_live` holds `SELECT`/`INSERT` and nothing else, anywhere. Its only write targets are
`runtime.decisions` and `audit.events`. It cannot UPDATE, DELETE or TRUNCATE a decision (by grant
*and* by the append-only trigger), cannot write `catalog`, `profiling` or `evaluation`, and cannot
read `evaluation` at all. `minos_runner`, `minos_evaluator` and `minos_trainer` have no USAGE on
`runtime` and cannot forge a decision. PUBLIC holds nothing in the seven application schemas. The
owner is `minos_admin`, a NOLOGIN non-superuser. No SECURITY DEFINER function is introduced.

Even the owner cannot rewrite a persisted decision: `audit.minos_reject_mutation` refuses UPDATE
and DELETE with `restrict_violation`.

## 9. Qualification

`l2h-decision-persistence-qualification-v1` runs the real write path against real PostgreSQL 16:
a scratch database at the accepted schema plus the overlay, provisioned with the accepted SAFE
config row and the fifty TRAIN-owned identity rows carried across from the operational store by
name, then 49 rounds × 4 requested modes = **196 decisions** actually persisted, plus the failure
drills on a fiftieth round held back for exactly that purpose. Thirty checks, every one derived
from the observation rather than recorded. No truth, no scores, no VALIDATION, no TEST.

| | |
|---|---|
| qualification identity | `e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20` |
| file SHA-256 | `9bb98d5106f239e596715d79e91c8dee2055b2cc3f2b2a860eb625b2b5400775` (49 887 bytes) |
| qualified source commit / tree | `1b67ee2f755b82526028d97aca7e8fe8db56e816` / `e5de947544241edeb83527c59de61792e0c18bb5` |
| overlay contract hash | `4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25` |
| accepted SAFE-CONTROLLER-FROZEN | `504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add` |

`verify_persistence_report` re-derives all thirty checks and the status from the observation,
refuses any operational value (URL, host, path, credential) anywhere in the document, and
**proves** the qualified source commit against git rather than length-checking it — shape is not
provenance. The pinned constants live in the commit *after* the one that ran the campaign, so
nothing identifies itself.



## 10. Where the code lives

`minos_engine.layer2` is the pure decision domain and is forbidden by
`tests/leakage/test_architecture_boundaries.py` from importing SQLAlchemy, Alembic or psycopg, or
from reading the environment. Persistence is therefore in `minos_engine.storage`, alongside every
other module that owns database writes — the same split `storage/profile_ingest.py` describes as
"storage-side counterpart of the pure `layer2.ingest` validation". The controller itself is
untouched.

| | |
|---|---|
| `storage/runtime_decision_contract.py` | frozen inventory, contract hash, private Core mapping |
| `storage/runtime_overlay.py` | apply / revert / inspect the overlay lineage |
| `storage/decision_persistence.py` | the write path |
| `storage/decision_persistence_qualification.py` | the campaign and its verifier |
| `migrations_runtime/` + `alembic_runtime.ini` | the overlay lineage itself |

## 11. What this stage does not do

No gate is issued. `Layer2Service.select_config` still raises `StageNotReadyError`. Activation is
a separate, deliberately small step: verify the accepted frozen controller, verify the accepted
persistence authority, verify request ownership, select SAFE, persist transactionally, return the
exact CONFIG bytes.
