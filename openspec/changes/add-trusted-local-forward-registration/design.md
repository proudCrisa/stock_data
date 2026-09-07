## Context

Registration `/4` means a pinned trust registry plus signed component authorities. Changing
that meaning in place would make one schema identifier represent two trust models. The new
local boundary therefore uses `/5` and an explicit authority mode.

## Decisions

### Preserve `/4`

All `/4` readers, writers, signatures, and negative tests remain unchanged. No `/4`
registration can omit its registry, signer coverage, or authority envelopes.

### Make local authority explicit

Registration `/5` fixes `authority_mode` to `trusted_local_mechanical`. Its prerequisite
files contain canonical source receipts, one exact-panel calendar, and one exact-panel
market-rule artifact. Its prerequisite payload binds their content identities, source
receipt closure, availability/effective/cutoff maps, policy coverage, and the existing
collector capability. Signature-shaped fields are forbidden.

The prerequisite materializer consumes explicit canonical local calendar and market-rule
facts. It does not infer trading sessions from weekdays or synthesize rule facts from the
panel or internal defaults. Every receipt binding must be consumed by one exact artifact
record, and every artifact record must be bound by its declared receipt.

### Keep point-in-time and continuity gates

Registration must precede every panel session. Static artifacts must be available no later
than registration and before each decision cutoff. Registration remains exclusive and
no-follow, and its exact bytes are bound by one `REGISTRATION_BOUND` ledger event. Capture
recomputes the static prerequisites and collector identity before any provider call.
Provider export repeats the `/5` registration-to-receipt-to-artifact binding check; a
negative readiness report cannot substitute for provenance closure.

### Do not upgrade evidence authority

Local mechanical trust establishes only immutable input identity and temporal closure. It
does not independently attest the data, set a readiness component to true, create a Judge
verdict, or authorize release.

### Expose a separate E1 research-replay export

A fully verified `/5` bundle MAY produce the read-only sibling schema
`stockdata-rqgm-research-replay-export/1`. It is not a new version or reinterpretation of
`stockdata-rqgm-provider-export/1`. For `/5`, the regular provider export and every component
readiness decision remain `ready=false` with the blocker
`trusted_local_mechanical_has_no_readiness_authority`, even when research replay is eligible.

The research export has exactly these top-level fields: `schema_version`,
`research_replay_eligible`, `scope`, `max_evidence_grade`, `registration_reference`,
`provider_checkout_reference`, `provider_bundle_reference`, `database_snapshot_reference`,
`ledger_snapshot_reference`, `continuity_closure_reference`, `panel_reference`,
`collector_schedule_reference`, `source_receipt_references`, `adjustment_references`,
`component_references`, `replay_policy_binding`, `outcome_control`, `blockers`,
`task_8_6b_credit`, and `authority_grants`. No field named `ready` is permitted in this
schema.

`scope` is exactly `TRUSTED_LOCAL_RESEARCH_ONLY`, `max_evidence_grade` is exactly
`E1_RETROSPECTIVE_RESEARCH`, and `task_8_6b_credit` is false. `registration_reference`
contains exactly `schema_version`, `sha256`, `authority_mode`, `registered_at`, and
`outcome_feedback_used`, and binds `/5`, `trusted_local_mechanical`, and false outcome
feedback. Every artifact reference is an explicit schema version plus SHA-256 content
identity; a filesystem path, timestamp, mutable alias, or latest-file selection is not an
identity.

The read-only verifier requires a separately recomputed `expected_bindings` closure;
omitting it, passing `None`, or passing a non-object is invalid. The closure exactly binds registration, checkout,
bundle, database, ledger, continuity, panel, collector schedule, source receipts,
adjustments, components, and replay policies. Its receipt identities and receipt-binding
sets, including the complete receipt-reference sequence, must exactly equal the export closure, with no missing, extra, reordered, substituted, unused, or unbound
receipt or artifact record. The verifier cannot derive this closure by mirroring the
candidate export or by consulting a mutable default or current cache.

The provider emits the research export through one pure, keyword-only builder that accepts
only this mandatory `expected_bindings` object. It copies the verified binding fields and
adds the fixed research-only schema, scope, E1 ceiling, outcome-control values, empty
blockers, false task 8.6b credit, and all-false authority grants before passing the complete
object through the same verifier. The builder reads no path, current database, cache,
callback, result, or authority and does not create or attest the independent closure; that
closure remains a separately produced trusted-local E1 process input.

`panel_reference` contains exactly `sha256`, `ordered_cells`, `symbol_count`,
`session_count`, and `panel_cell_count`; eligibility requires 12 symbols, three sessions,
and exactly 36 unique ordered symbol-session cells with no subset, superset, duplicate, or
alias substitution. Each symbol matches exactly `^[0-9]{6}\.(SH|SZ)$`, and the 36 cells may
contain both exchanges. Each session is a real canonical date. `registration_reference.registered_at` is an ISO
datetime with an explicit UTC offset; converted to `Asia/Shanghai`, its date must be
strictly earlier than every panel session. `collector_schedule_reference` contains exactly `sha256`,
`terminal_step_count`, and `completed_step_ordinals`; eligibility requires twelve terminal
steps and ordinals `[0,1,2,3,4,5,6,7,8,9,10,11]`, representing three sessions by four
ordered phases, with no failure, dangling attempt, duplicate, reorder, or foreign step.

`adjustment_references` contains exactly separate `execution` and `signal` artifact
references. `component_references` contains exactly the fixed nine provider component keys;
each value contains exactly `artifact_reference`, `mechanically_complete`, and `blockers`.
These are research completeness facts, never component readiness. Eligibility requires
every component to be mechanically complete with no blocker, exact source-receipt closure,
and byte-consistent registration, checkout, bundle, database, ledger, continuity, panel,
schedule, adjustment, and component identities.

`replay_policy_binding` contains exactly `research_authorization_reference`,
`shared_cash_policy_reference`, `market_rule_cost_policy_binding`, and
`risk_policy_reference`. `market_rule_cost_policy_binding` contains exactly
`schema_version`, `policy_reference`, `market_rule_artifact_reference`, and `sha256`;
`schema_version` is `stockdata-market-rule-cost-policy-binding/1`, and `sha256` is the
lowercase SHA-256 over canonical JSON of every binding field except `sha256`. The shared-cash policy binds one chronological account, one
initial-capital identity, allocation and order priority, and forbids per-symbol sleeves. The
market-rule/cost policy binds the provider market-rule artifact plus dated lot size, T+1,
suspension, price limits, listing age, commission, minimum fee, transfer fee, stamp duty,
slippage, and order lifecycle. Defaults or caller booleans cannot synthesize either policy.
The binding's `market_rule_artifact_reference` exactly equals
`component_references.market_rules.artifact_reference`, and the independent expected closure
exactly matches the entire `market_rule_cost_policy_binding`.

RQGM creates the downstream plan with exact one-way relations:
`plan.research_authorization_reference == export.replay_policy_binding.research_authorization_reference`,
`plan.shared_cash_policy_reference == export.replay_policy_binding.shared_cash_policy_reference`,
`plan.risk_policy_reference == export.replay_policy_binding.risk_policy_reference`,
`plan.transaction_cost_policy_reference == export.replay_policy_binding.market_rule_cost_policy_binding.policy_reference`,
`plan.market_rule_policy_reference == export.component_references.market_rules.artifact_reference`,
and `plan.corporate_action_policy_reference == export.component_references.corporate_actions.artifact_reference`.
The provider export does not
bind the final RQGM replay-plan hash or accept a final-plan reference as an eligibility
input; RQGM constructs and content-addresses that plan afterward.

`outcome_control` contains exactly `registration_outcome_feedback_used`,
and `eligibility_inputs_outcome_free`; they must respectively be false and true. Eligibility
is computed without accepting any candidate result, metric, label, or result-derived policy
input. The RQGM consumer separately freezes its final replay plan before result access.
`authority_grants` contains exactly `component_readiness`, `execution_readiness`, `judge`,
`release`, `production`, and `advice`; every value is false.

The exporter fails closed on any missing or extra field, stale or ambiguous registration,
identity drift, incomplete or reordered closure, late or backfilled capture, result-shaped
input, outcome feedback, detached replay policy, or authority override. It adds no signature,
trust-root, network, Judge, release, or production dependency.

### Materialize the verified export without mutable lookup

The reference-only research export is not itself replay data. After that export and its
independently recomputed `expected_bindings` pass, the provider MAY emit one read-only,
content-addressed `stockdata-rqgm-research-replay-materialization/1`. The materialization
binds the exact verified export and expected-closure references; the canonical bytes for all
nine component artifacts; the shared-cash and risk policy bodies; and a canonical body hash.
Each component and policy body MUST independently reproduce the SHA-256 already named by the
export. An aggregate materialization hash cannot substitute for any per-component equality.

Its top-level field set is exactly `schema_version`, `provider_export_reference`,
`provider_expected_bindings_reference`, `component_payloads`,
`shared_cash_policy_body`, `risk_policy_body`, and `materialization_sha256`.
`component_payloads` has exactly the existing nine provider component keys and each value is
the canonical JSON object whose SHA-256 and schema equal that component's export reference.
The two policy bodies likewise reproduce their export references.
`provider_expected_bindings_reference` uses
`stockdata-rqgm-research-replay-expected-bindings/1` plus the canonical SHA-256 of the exact
independent closure supplied to the verifier. `materialization_sha256` hashes every
materialization field except itself.

`shared_cash_policy_body` contains exactly `schema_version`, `initial_capital`,
`allocation_policy`, `order_priority`, `single_cash_pool`, and `per_symbol_sleeves`.
Its schema is `rqgm-trusted-local-shared-cash-policy/1`; capital is finite and positive;
allocation is `pro_rata_then_ticker`; order priority is `sells_then_buys_then_ticker`;
single cash is true and sleeves are false. `risk_policy_body` contains exactly
`schema_version`, `long_only`, `leverage_allowed`, `target_weight_min`,
`target_weight_max`, and `gross_target_weight_limit`. Its schema is
`rqgm-trusted-local-risk-policy/1`; long-only is true, leverage is false, bounds are finite
with `0 <= min <= max <= 1`, and gross limit is finite in `(0, 1]`.

The materialization contains no filesystem path, mutable alias, callback, candidate result,
RQGM plan reference, readiness decision, or authority grant. Its loader is pure and read-only:
it accepts explicit canonical bytes, rejects missing, extra, reordered, detached, or
non-canonical content, and never falls back to the current database, latest artifact, cache,
or a caller default.

### Resolve one formally published bundle without mirroring the candidate export

The provider-side
`resolve_trusted_local_research_replay_inputs(bundle_file, *, replay_policy_binding)`
bridge consumes only a lexical-absolute formally published `bundle.json` and one mandatory,
outcome-blind replay-policy binding. It is a read-only resolver, not the pure export or
materialization builder. It MUST reuse the existing no-follow retained-descriptor bundle
verification, keep every locator descriptor live through semantic and final identity
reverification, reject physical aliases and ABA drift, and fail when either body validation
or descriptor cleanup fails.

The resolver independently derives the expected closure from the verified bundle,
registration, database snapshot, ledger snapshot, continuity closure, panel, collector
schedule, receipts, adjustments, component bytes, and supplied replay policy. It MUST NOT
copy or inspect a candidate research export. It requires `/5 trusted_local_mechanical`, the
exact 36-cell panel, completed collector ordinals `0..11`, outcome-free capture, exact
receipt closure, and all nine mechanically complete component payloads. Mechanical
completeness is computed from the retained canonical bytes and existing provider semantic
verifiers; caller-supplied `mechanically_complete=true` is not evidence.

#### 4.7a-d Retained database receipt domains and unsigned reconstruction

The registration receipt domain contains exactly the two `/5` prerequisite receipts and
closes only `trading_calendar` and `market_rules` through their declared receipt bindings.
The resolver SHALL rebuild the collector receipt domain directly from the retained database
snapshot for `execution_prices`, `signal_prices`, `decision_context`, `universe`,
`instrument_status`, and `corporate_actions`; a registration receipt MUST NOT be borrowed
for any of those records. Both domains close bidirectionally before their references are
combined. The resulting reference sequence is non-empty, sorted by `(schema_version,sha256)`,
has no repeated SHA-256 across schemas, and rejects a missing, extra, unconsumed, colliding,
or cross-domain receipt.

Availability closure preserves both canonical generic market-rule branches. For a generic
market-rule artifact it keys records by `(panel_entry, st_status)` and requires exactly one
`is_st=false` and one `is_st=true` record per panel cell; availability rows resolve the same
branch through the immutable record hash. Other components, and legacy single-rule artifacts,
remain one record per panel cell.

For each pre-open session, the resolver rebuilds `universe_id` as the SHA-256 of canonical
`stockdata-forward-universe-identity/1` data containing `effective_date`, `pre_open`, source,
the canonical database receipt ID, and the SHA-256 of the complete sorted `is_member=true`
membership. Every cell in that session shares that identity. It rebuilds instrument status
from the retained status rows only after comparing them with the same full-market receipt.

The resolver requires one real pre-open corporate-action capture for each session, exact
coverage of the twelve symbols, canonical receipt/body verification, and three distinct
collector receipts. The current 8.6a path materializes only genuine zero-event captures as
`{"events":[]}`. It rejects every positive event until a versioned, deterministic mapping
proves an offset-aware announcement timestamp and one unambiguous supported event type; it
MUST NOT synthesize a time from a date.

The exact return object contains only `schema_version`, `expected_bindings`, and
`component_payloads`, with schema
`stockdata-rqgm-research-replay-resolved-inputs/1`. It contains no path, mutable alias,
ordinary readiness upgrade, export, materialization, policy body, RQGM plan, candidate
result, callback, signature, or authority grant. The regular provider export remains
`ready=false`; a caller must separately pass the returned closure to the existing research
export builder and the returned bytes plus separately frozen shared-cash/risk policy bodies
to the existing materialization builder.

#### 4.8 Materialize one completed collector snapshot without caller semantic input

A production materializer bridge and CLI SHALL accept exactly one `/5` registration file,
its collector database, and an output directory. Before creating output or private staging,
it requires the registered schedule to have all twelve terminal steps and acquires one
retained, no-follow snapshot lifecycle over the registration, database, ledger, and every
registered prerequisite. The lifecycle and final ABA checks remain live through semantic
verification, `materialize_provider_bundle`, final publication, and cleanup; the bridge MUST
NOT reopen a mutable live path or replace retained bytes with caller-supplied semantic data.

From that same completed snapshot, the bridge derives the exact panel, separate execution
and signal adjustment artifacts, and all nine canonical component files required by
`materialize_provider_bundle`. `trading_calendar`, `market_rules`, and their exact two source
receipts come only from the registration's `prerequisite_files`. Execution prices, signal
prices, and decision context reuse `reconstruct_intrinsic_evidence`; universe, instrument
status, and corporate actions reuse `reconstruct_forward_component_evidence`; availability
is built by one production builder from those verified canonical component and receipt
closures. Missing or ambiguous adjustment identity, positive corporate-action normalization,
or any reconstruction mismatch fails closed rather than accepting a fixture, callback, or
caller-created component bytes.

All derived files and the provider bundle are written under one private sibling staging
directory. The bridge verifies the exact canonical bytes and bundle closure before one final
rename publishes the output directory; any validation, ABA, write, rename, or cleanup
failure publishes nothing and removes private partial state. The result remains input to the
existing research resolver, export builder, and materialization builder. It does not change
the ordinary `/5` provider export from `ready=false` and grants no readiness, task 8.6b,
Judge, release, production, or advice authority.

#### 4.9 Own the one-shot trusted-local research replay projection

The provider SHALL expose one read-only, one-shot bridge whose input is exactly one
lexical-absolute formally published `bundle.json` and one canonical
`stockdata-rqgm-trusted-local-research-replay-policy-request/1`. The policy request contains
exactly `schema_version`, `replay_policy_binding`, `shared_cash_policy_body`, and
`risk_policy_body`. The bridge accepts no pre-resolved closure, component payload, candidate
export, materialization, RQGM plan, result, callback, or alternate implementation.

Inside one fixed call path, the provider bridge calls
`resolve_trusted_local_research_replay_inputs`, then
`build_trusted_local_research_replay_export`, then
`build_trusted_local_research_replay_materialization`. It passes the resolver's
`expected_bindings` unchanged to the export builder, and passes that same closure, the
resolver's component payloads, the verified export, and the request's policy bodies to the
materialization builder. Every intermediate output verifies before the next call. RQGM
consumes only the final bridge envelope and MUST NOT call, replace, or compose those provider
steps itself.

The exact output schema is
`stockdata-rqgm-trusted-local-research-replay-envelope/1`, with exactly
`schema_version`, `provider_export`, `provider_expected_bindings`, and
`provider_materialization`. The fields are the already verified canonical outputs, not
reconstructed mirrors. Any input, intermediate, or final-envelope failure returns no partial
envelope. The bridge performs no replay and introduces no second resolver, export,
materialization, market-data authority, writer, readiness decision, or downstream authority.

## Rejected Alternatives

- Reinterpret `/4`: rejected because it breaks schema identity.
- Generate local placeholder signatures: rejected because it fabricates independence.
- Let RQGM write the registration: rejected because `stock_data` owns market authority.
- Reuse provider `ready`: rejected because research eligibility is not execution authority.
- Let RQGM resolve references from mutable provider paths: rejected because it detaches the
  replay from the verified export and makes a same-reference rerun non-reproducible.
