# ADR-0005 — SAFE_BASELINE is the only authorizable control mode

## Status
Accepted (L2-H). Supersedes nothing; constrains all future Layer 2 controller work.

## Context

Layer 2 has always defined four control modes (`layer2/contracts.py:ControlMode`):
`SAFE_BASELINE`, `BOUNDED`, `FULL_CONTEXTUAL`, `REFINEMENT`. The last three choose a GATK
configuration using a learned model; `SAFE_BASELINE` emits the qualified baseline and is the
architecture's fail-safe.

Two scientific programmes asked whether a model could beat the qualified baseline:

| Programme | Question | Outcome |
|---|---|---|
| L2-G v1 | predict utility `U(BAM, config)` | `NO_CONTEXTUAL_MODEL_QUALIFIED_ON_TRAIN`, shortlist empty |
| L2-G v2 | predict advantage `DELTA = U(i, θ) − U(i, θ_safe)` | `NO_CONTEXTUAL_SELECTOR_QUALIFIED_ON_TRAIN_V2`, shortlist empty |

v2 asked the strictly easier question and still failed, under a promotion rule fixed before any
model was fitted. Both freezes are committed: `1c2039de…` (v1) and `42310a97…` (v2). The v2 freeze
records `CONTEXTUAL_SELECTOR_RESEARCH_CLOSED` and
`models_qualified_status = HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL`.

There is therefore no qualified model bundle, and the MODELS-QUALIFIED gate does not exist.

## Decision

1. **Contextual modes require a qualified model bundle.** `BOUNDED`, `FULL_CONTEXTUAL` and
   `REFINEMENT` are disabled. `REFINEMENT` is disabled *for configuration selection*: refining a
   configuration is still choosing one.
2. **`SAFE_BASELINE` is the only currently authorizable control mode.** This mode returns no
   learned prediction and changes no GATK parameter relative to the qualified baseline — the
   emitted config is byte-identical to the already-qualified `157d88d1…` payload.
3. **This is not a model qualification.** An empty shortlist is not evidence of a model; it is the
   absence of one. MODELS-QUALIFIED stays `HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL` and must
   never be issued on the strength of this ADR or of the safe controller working correctly.
4. **The mode set is structural, not configurable.** `safe_controller_policy.ALLOWED_MODES` names
   exactly one mode; there is no environment variable, caller argument, request field or override
   that widens it. A request for a contextual mode is deterministically reduced to `SAFE_BASELINE`
   with fallback reason `SAFE_BASELINE_FORCED`, never executed and never silently relabelled.
5. **Restoring contextual capability requires a NEW explicitly authorized scientific program** that
   produces a qualified model bundle — not a modification of this controller, not a relaxation of
   the promotion rule, and not a reinterpretation of either frozen campaign.

## Consequences

- The safe controller is a *degraded* control mode that is nonetheless fully authorized: its
  correctness rests on the BASELINE-QUALIFIED lineage, not on any model evidence.
- Gate naming must not overstate capability. A gate covering this controller is mode-scoped
  (see `docs/layer2/L2H_SAFE_CONTROLLER.md` §gate naming); `CONTROLLER-FROZEN` remains reserved for
  a controller with `FULL_CONTEXTUAL` capability and therefore requires MODELS-QUALIFIED PASS.
- `Layer2Service.select_config` stays blocked until controller qualification and the decision
  persistence path are both closed. The safe controller is reachable only through its own source
  API, which is what qualification drills will exercise.
- Any future reader finding an empty shortlist and a working safe controller should read this ADR
  before concluding that the model programme succeeded. It did not.
