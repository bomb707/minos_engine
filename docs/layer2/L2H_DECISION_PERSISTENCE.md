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

`r0001` re-aims the foreign key at `profiling.bam_profiles` and adds `NOT NULL` at the same time.
That is a strengthened FK, not a weakened one.

## 5a. The privilege defect `r0002` closes

`r0001` also granted `minos_live` **SELECT on `profiling.bam_profiles` and
`catalog.dataset_registry`**, because the write path needed to resolve the owning profile. Those
are the raw identity tables. Between them they carry the profile and dataset identities of every
partition, **TEST included**, and the L2-D architecture deliberately never grants an application
role a raw identity table — it exposes partition-scoped views instead, and
`evaluation.sealed_test_profile_members` is granted to no application role at all.

The v1 qualification then certified those grants as least privilege on the strength of a SQL
recorder showing the code only ever queried by `profile_id`. **That is a statement about
behaviour. Least privilege is a statement about capability.** With the grants in place:

```
SET ROLE minos_live;
SELECT count(*) FROM profiling.bam_profiles;   -- 75, every partition
```

`r0002` revokes both grants and replaces them with a narrow lookup surface:

| | |
|---|---|
| function | `runtime.l2h_resolve_owned_profile(text, text)` |
| owner | `minos_admin` (NOLOGIN, **not** a superuser) |
| security | `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp` |
| language | `plpgsql`, no dynamic SQL, no caller predicate |
| arguments | two mandatory scalars; NULL or empty **raises** (`null_value_not_allowed`) rather than matching everything |
| result | at most one identity row; more than one **raises** (`cardinality_violation`) |
| grants | `PUBLIC` EXECUTE revoked; EXECUTE to `minos_live` alone |
| returns | exactly the fields the resolver cross-checks, plus the row id — **never** `profile_document` |

Afterwards every enumeration attempt is refused by PostgreSQL with SQLSTATE `42501`, and
`has_table_privilege('minos_live', …, 'SELECT')` is false for all seven partition-bearing
relations — the sealed ones probed *by privilege*, never by reading a row.

The function is an **exact-lookup surface, not an authorization surface**. Whether a round may be
decided for at all is settled upstream by the verified round/profile ownership authority, which
is why the function is deliberately not partition-scoped: scoping it to `train` would bake a
research partition into the live production path. A caller who already holds a 32-hex profile id
can confirm that profile exists; what the seal protects against — and what `r0002` restores — is
*enumeration*.

`r0002` also grants `minos_live` SELECT on `public.alembic_version` and
`runtime.alembic_version_runtime`. Those are different in kind: one revision string each, no
identity, no partition, nothing sealed. Without them the live path could not read its own schema
version, and under a real least-privilege connection it could never have run at all — a second
thing v1 could not see, because v1 ran its entire campaign as the superuser.

`r0001` is **not rewritten**. It is committed, qualified and applied; the corrective is additive,
with `down_revision = r0001_l2h_runtime_decisions`, and its downgrade restores r0001's exact
privilege and function shape.

## 6. Nothing is trusted from the caller

| | How it is obtained |
|---|---|
| the decision | **made here** from verified capabilities, never accepted as a parameter |
| the config row | resolved from `catalog.gatk_configs` **by accepted config hash**; absent or ambiguous fails closed; the row's `parameter_space_hash` must be the accepted one |
| the profile row | resolved through `runtime.l2h_resolve_owned_profile` by the **proven** `profile_id` *and* `round_id`, then cross-checked on ten identity fields plus `dataset_id`/`round_id`/`chromosome` |
| provisioning | `catalog.gatk_configs` is provisioned by an **administrative**, idempotent step that verifies the frozen payload bytes hash to the accepted config hash. `minos_live` holds only SELECT there and must: a live path that could insert catalog rows could insert a config nobody accepted and then decide in favour of it. |
| profiles | never provisioned by this engine. If Layer 1 ingestion has not written the row, persistence **fails closed** and names that production prerequisite. |

`profiling.bam_profiles` also holds VALIDATION and TEST members. After `r0002` the live role
cannot read it at all; the only way in is the narrow function, by exact name. The qualification
proves this by *executing* enumeration attempts as the live role and requiring SQLSTATE `42501`,
not by observing that the code did not try.

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
`runtime.decisions` and `audit.events`. After `r0002` it holds **no raw identity table at all**:
its complete grant set is `audit.events:INSERT`, `catalog.artifacts:SELECT`,
`catalog.datasets:SELECT`, `catalog.gatk_configs:SELECT`, `models.model_bundles:SELECT`,
`public.alembic_version:SELECT`, `runtime.alembic_version_runtime:SELECT`,
`runtime.decisions:INSERT`, `runtime.decisions:SELECT`, plus EXECUTE on the one narrow function.

It cannot UPDATE, DELETE or TRUNCATE a decision (by grant *and* by the append-only trigger),
cannot write `catalog`, `profiling` or `evaluation`, and cannot read `evaluation` at all.
`minos_runner`, `minos_evaluator` and `minos_trainer` have no USAGE on `runtime`, cannot forge a
decision, and cannot execute the resolver. PUBLIC holds nothing in the seven application schemas
and cannot execute the resolver. The owner is `minos_admin`, a NOLOGIN non-superuser. Exactly one
SECURITY DEFINER function exists and it is the resolver.

Even the owner cannot rewrite a persisted decision: `audit.minos_reject_mutation` refuses UPDATE
and DELETE with `restrict_violation`.

## 9. Qualification

`l2h-decision-persistence-qualification-v1` runs the real write path against real PostgreSQL 16:
a scratch database at the accepted schema plus the overlay, provisioned with the accepted SAFE
config row and the fifty TRAIN-owned identity rows carried across from the operational store by
name, then 49 rounds × 4 requested modes = **196 decisions** actually persisted — on a connection that
has **assumed `minos_live`**, so every statement is bounded by the live role's real grants — plus
the failure drills on a fiftieth round held back for exactly that purpose, and a capability audit
that executes every enumeration attempt as the live role and requires SQLSTATE `42501`. Forty-one checks, every one derived
from the observation rather than recorded. No truth, no scores, no VALIDATION, no TEST.

| | |
|---|---|
| qualification identity | `7bfd8f9f816a387653125d6be820b8cf05ccf017498009bc7e706d29fd3337b3` |
| file SHA-256 | `1e54930d571befc9543c351fcd7cf5dbd93f3ef1ac335da716cc615e3970cf6a` (67 453 bytes) |
| qualified source commit / tree | `c05aad9ca160f6fa975c5230e175385132362656` / `5ffe3d765b2fc8aaa4a850beb8399bfc5d34266d` |
| overlay head revision | `r0002_l2h_live_profile_lookup` |
| overlay head contract hash | `2af0f847037039c413c3e3b277839cacd4c10063869f7d1210aa9715c687d120` |
| r0001 contract hash (unchanged) | `4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25` |
| accepted SAFE-CONTROLLER-FROZEN | `504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add` |
| supersedes | v1 `e88f6cf8…` (file `9bb98d51…`), `SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION` |

`verify_persistence_report` re-derives all forty-one checks and the status from the observation,
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
| `migrations_runtime/` + `alembic_runtime.ini` | the overlay lineage: `r0001` then the `r0002` privilege corrective |

## 11. What this stage does not do, and what activation still needs

No gate is issued. `Layer2Service.select_config` still raises `StageNotReadyError`.

Two prerequisites remain, and the second is the larger one:

**1. The SAFE config row is not provisioned.** `catalog.gatk_configs` is empty in the operational
store, so the live path fails closed there today. `provision_safe_config_row` is the administrative
step that binds it — idempotent, keyed on the accepted config hash, payload-byte verified, and
deliberately outside the live decision transaction. It is a separately authorized activation step
and is **not** run merely because a corrective ran.

**2. No genuinely new LIVE round can obtain a `VerifiedRoundProfileAuthority`.**
`l2h-round-profile-ownership-v2` is TRAIN-only by construction: `load_verified_round_profile_corpus`
builds the corpus from the frozen fifty-row TRAIN schedule (`manifests/l2f2_train_schedule_v1.json`)
against `/home/hr/bittensor/minos_l2d_corpus`, and `owned()` refuses any round outside it —

```
c.owned("a-brand-new-live-round-2026")
RoundProfileAuthorityError: round '...' is not in the accepted profile snapshot
```

`select_safe_baseline` requires an instance of that exact class, whose constructor token only the
TRAIN loader holds, and the decision manifest records `profile_corpus_identity` and
`profile_ownership_anchors` that are TRAIN-campaign anchors (TRAIN schedule, Phase-A authority,
split manifest, registry snapshot). Nor can production ingestion route around it: `ingest_profile`
requires the dataset to be a member of the requested split epoch's allocation set, so a brand-new
BAM cannot be ingested without a new split epoch either.

**Supporting LIVE rounds will therefore require changing and requalifying the frozen controller
source.** `round_profile_authority.py` must be able to mint an authority for a round that is not a
TRAIN snapshot member, and `safe_controller.py` must accept it; both are inside the qualified S3
source `7d064fe8…` that the accepted SAFE-CONTROLLER-FROZEN gate binds. It will very likely also
need a decision-manifest v2 whose ownership anchors mean something for a live round — which in
turn needs a further runtime-overlay revision, because `r0001`'s
`ck_decisions_manifest_schema` pins `l2h-safe-decision-manifest-v1`. That work is deliberately
**not** done here, and no part of the S3 source was touched to make LIVE fit.

Activation, once both are resolved, should still be small: verify the accepted frozen controller,
verify the accepted persistence authority, verify request ownership, select SAFE, persist
transactionally, return the exact CONFIG bytes.
