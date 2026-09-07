## ADDED Requirements

### Requirement: Trusted-local registration has a distinct schema identity
The provider SHALL emit `rqgm-forward-panel-registration/5` only with
`authority_mode=trusted_local_mechanical`. Registration `/4` SHALL retain its existing
signed-authority meaning and validation. A `/5` payload MUST reject registry, signer,
signature, trust-root, publisher, or authority-envelope fields.

#### Scenario: Local authority is explicit
- **WHEN** a caller registers a fresh exact panel using trusted local data
- **THEN** the output uses `/5`, records no external authority claim, and binds only mechanically verified prerequisites

#### Scenario: Signed and local schemas are mixed
- **WHEN** a `/5` payload contains a `/4` signature or trust field, or a `/4` payload omits one
- **THEN** the provider rejects it before registration or capture

### Requirement: Trusted-local prerequisites remain point-in-time and immutable
Registration `/5` SHALL bind canonical source receipts, exact-panel trading calendar,
exact-panel market rules, availability/effective/decision-cutoff maps, policy coverage,
the exact 36-cell panel (12 symbols by 3 sessions; registration `workspace_count` counts
panel cells), and the complete collector capability. Every session MUST be future to
registration, every static input MUST be available by registration and before its
decision cutoff, and outcome feedback MUST be false. The local prerequisite materializer
MUST consume explicit canonical calendar and market-rule facts and MUST NOT infer a trading
day from weekdays or create market-rule facts from the panel or internal defaults. Fact
coverage MUST equal the panel and required rule branches exactly.

The complete receipt closure MUST be exact at both receipt-ID and receipt-binding levels:
every bound receipt and every binding inside it MUST be used by an exact artifact record,
and every artifact record MUST be bound by its declared receipts. Materialization and
verified export MUST independently recheck that the registration binds the same receipt
IDs and calendar/rule artifact hashes they consume.

#### Scenario: Static content drifts after registration
- **WHEN** a source receipt, calendar, market rule, panel, database, ledger, genesis, cohort, or physical identity differs before capture
- **THEN** capture fails before any provider call and writes no market evidence

#### Scenario: Local facts are absent or incomplete
- **WHEN** the materializer lacks explicit canonical facts, a panel session is marked non-trading, or a required rule branch is absent
- **THEN** materialization fails without creating a partial prerequisite directory

#### Scenario: A receipt contains an unused binding
- **WHEN** a bound receipt includes any extra, outside-panel, or otherwise unconsumed binding
- **THEN** registration, materialization, and export reject the closure

### Requirement: Trusted-local registration preserves collector continuity
The provider SHALL write `/5` exclusively without overwrite and bind its exact canonical
hash, panel, sessions, and prerequisite hash in exactly one `REGISTRATION_BOUND` event. A
crash MAY complete a missing bind only for byte-identical registration and prerequisites.

#### Scenario: Registration is retried
- **WHEN** the canonical registration and ledger binding already match
- **THEN** the retry is idempotent and appends no duplicate event

### Requirement: Trusted-local mode grants no readiness authority
Trusted-local registration and continuity SHALL be negative provenance gates only. They
MUST NOT populate missing components, make a component ready, create Judge evidence, or
grant release or production authority.

#### Scenario: Mechanical inputs are complete but a component is absent
- **WHEN** provider materialization lacks any required execution component
- **THEN** readiness remains false even though registration `/5` and continuity are valid

### Requirement: Complete trusted-local closure may enable a separate E1 research export
The provider SHALL emit `stockdata-rqgm-research-replay-export/1` only after independently
verifying a `/5` registration, immutable checkout and bundle, database snapshot, ledger
snapshot, continuity closure, exact source-receipt closure, separate execution/signal
adjustments, all nine mechanically complete components, the exact ordered 36-cell panel,
and all twelve ordered terminal collector steps. The export carries
`research_replay_eligible=true`, `scope=TRUSTED_LOCAL_RESEARCH_ONLY`, and
`max_evidence_grade=E1_RETROSPECTIVE_RESEARCH`.

The read-only verifier SHALL require a separately recomputed `expected_bindings` input and
MUST reject an omitted, `None`, or non-object value. That closure SHALL exactly match the export's
registration, checkout, bundle, database, ledger, continuity, panel, collector schedule,
source receipts, adjustments, components, and replay policies. Receipt IDs,
the complete receipt-reference sequence, receipt-binding sets, and artifact records SHALL
close in both directions with no missing, extra, reordered, substituted, unused, or unbound
entry; the expected closure MUST NOT be reconstructed from the
candidate export or a mutable default or cache.

The export SHALL use exactly the top-level and nested field sets defined in the design and
content identities rather than mutable paths or aliases. It MUST contain no `ready` field,
MUST set `task_8_6b_credit=false`, and MUST keep every `authority_grants` value false. It
MUST NOT change or substitute for `stockdata-rqgm-provider-export/1`; for `/5`, the regular
provider export and each component remain `ready=false` with
`trusted_local_mechanical_has_no_readiness_authority`.

#### Scenario: Receipt domains are independently closed
- **WHEN** the resolver derives research inputs from a retained `/5` bundle
- **THEN** exactly two registration receipts close calendar and market rules, collector receipts
  rebuild the six database-derived components, each domain rejects missing, extra, duplicate,
  unconsumed, or cross-domain receipts, and only the sorted collision-free union enters
  availability and `source_receipt_references`

#### Scenario: Retained unsigned records reproduce component bytes
- **WHEN** universe, instrument status, or corporate actions are supplied in the bundle
- **THEN** the resolver rebuilds their canonical records and database receipts from the retained
  snapshot and rejects any byte, record hash, receipt, panel, cutoff, or timing difference

#### Scenario: Generic market-rule branches remain distinct
- **WHEN** a `/5` generic market-rule artifact supplies both ST branches for a panel cell
- **THEN** availability closure consumes exactly one `is_st=false` and one `is_st=true` record
  keyed by `(panel_entry, st_status)` and rejects a missing, duplicate, or substituted branch
  without changing the one-record closure of other components

#### Scenario: Universe identity is session-complete
- **WHEN** a pre-open full-market context receipt covers a panel session
- **THEN** every panel cell shares the SHA-256 of canonical
  `stockdata-forward-universe-identity/1` data over that session, source receipt, and complete
  sorted true-member set; a partial membership projection is rejected

#### Scenario: Corporate actions are genuine zero-event captures
- **WHEN** the current 8.6a resolver materializes corporate actions
- **THEN** it requires three distinct verified pre-open captures with exact twelve-symbol
  coverage and emits only `events=[]`; any positive, ambiguous, date-only, unknown, or
  non-canonical event fails closed without a synthesized timestamp or type

#### Scenario: All cells and steps are complete
- **WHEN** the exact 12-symbol by 3-session panel and collector step ordinals `0..11` independently verify with complete mechanical component closure
- **THEN** research replay eligibility may be true while provider readiness, task 8.6b credit, and every authority grant remain false

#### Scenario: One cell or collector step is invalid
- **WHEN** any of the 36 cells or twelve terminal steps is missing, extra, duplicated, reordered, failed, dangling, late, backfilled, or unverifiable
- **THEN** research replay eligibility is false with an explicit blocker

#### Scenario: Independent expected closure is absent or detached
- **WHEN** `expected_bindings` is omitted, is `None`, or differs in panel, schedule, receipt identity, receipt binding, artifact, component, adjustment, or policy closure
- **THEN** verification rejects before a research-eligible export is returned

#### Scenario: Panel exchange or registration time is invalid
- **WHEN** a cell is not a canonical six-digit `.SH` or `.SZ` symbol with a real session date, or `registered_at` lacks an offset or is not earlier in `Asia/Shanghai` than every session
- **THEN** verification rejects the export before research replay

### Requirement: Research replay is outcome-blind and binds one economic policy
The research export SHALL bind an explicit content-addressed local research authorization,
single chronological shared-cash policy, provider market-rule/cost policy, and immutable risk
policy. `replay_policy_binding.market_rule_cost_policy_binding` SHALL contain exactly
`schema_version`, `policy_reference`, `market_rule_artifact_reference`, and `sha256`;
`schema_version` SHALL equal `stockdata-market-rule-cost-policy-binding/1`, and `sha256` SHALL
equal the lowercase SHA-256 over canonical JSON of every binding field except `sha256`.
The shared-cash policy SHALL bind one initial-capital identity, one common cash pool,
deterministic allocation and order priority, and no independent sleeves. The cost policy
SHALL bind the exact provider market-rule artifact and its dated lot-size, T+1, suspension,
price-limit, listing-age, commission, minimum-fee, transfer-fee, stamp-duty, slippage, and
order-lifecycle surface. The binding's `market_rule_artifact_reference` SHALL exactly equal
`component_references.market_rules.artifact_reference`, and the independent expected closure
SHALL exactly match the entire `market_rule_cost_policy_binding`. In the downstream RQGM plan,
`plan.research_authorization_reference == export.replay_policy_binding.research_authorization_reference`,
`plan.shared_cash_policy_reference == export.replay_policy_binding.shared_cash_policy_reference`,
`plan.risk_policy_reference == export.replay_policy_binding.risk_policy_reference`,
`plan.transaction_cost_policy_reference == export.replay_policy_binding.market_rule_cost_policy_binding.policy_reference`,
`plan.market_rule_policy_reference == export.component_references.market_rules.artifact_reference`,
and `plan.corporate_action_policy_reference == export.component_references.corporate_actions.artifact_reference`.
The provider export MUST NOT
bind the final RQGM replay-plan hash; RQGM SHALL construct and content-address that plan
afterward and bind the final provider export in one direction.

Eligibility MUST fail before export on registration, snapshot, receipt, panel, step,
component, adjustment, policy, or risk identity drift; historical backfill; stale or
ambiguous registration selection; outcome feedback; result-shaped input; or an RQGM plan
input supplied to provider eligibility. No missing identity may be inferred from a default
or current cache. An eligible export remains E1 research-only and MUST be rejected by
provider readiness, task 8.6b, E2+, Judge, release, production, and advice consumers. RQGM
separately MUST freeze its replay plan before any result access.

#### Scenario: Complete rows arrive through historical backfill
- **WHEN** a cell or component becomes complete outside its bound registered collector step and applicable phase window
- **THEN** research replay eligibility remains false even when current cache coverage is complete

#### Scenario: A result changes the plan or policy
- **WHEN** any panel outcome influences a provider eligibility input, shared-cash policy, cost policy, or risk policy
- **THEN** the export is ineligible and cannot be represented as strict OOS or authority evidence

#### Scenario: Research export enters an authority path
- **WHEN** a consumer presents an eligible research export as readiness, task 8.6b, E2+, Judge, release, production, or advice evidence
- **THEN** the consumer rejects it regardless of mechanical completeness or replay profitability

### Requirement: Research replay materialization resolves every reference immutably
After the research export and its independent expected closure verify, the provider SHALL be
able to emit `stockdata-rqgm-research-replay-materialization/1` from explicit canonical
inputs. The materialization SHALL bind the verified export reference, expected-closure
reference, canonical bytes for all nine component artifacts, shared-cash policy body, risk
policy body, and a canonical body SHA-256. Every component and policy body SHALL independently
reproduce the exact reference already present in the export; the aggregate materialization
hash MUST NOT mask a missing or detached item.

The top-level field set SHALL be exactly `schema_version`, `provider_export_reference`,
`provider_expected_bindings_reference`, `component_payloads`,
`shared_cash_policy_body`, `risk_policy_body`, and `materialization_sha256`.
`component_payloads` SHALL contain exactly the nine provider component keys.
`provider_expected_bindings_reference` SHALL use
`stockdata-rqgm-research-replay-expected-bindings/1` and the canonical SHA-256 of the exact
closure passed independently to export verification. `materialization_sha256` SHALL be the
canonical body hash excluding itself.

`shared_cash_policy_body` SHALL contain exactly `schema_version`, `initial_capital`,
`allocation_policy`, `order_priority`, `single_cash_pool`, and `per_symbol_sleeves`, with
schema `rqgm-trusted-local-shared-cash-policy/1`, finite positive capital,
`pro_rata_then_ticker`, `sells_then_buys_then_ticker`, true single cash, and false sleeves.
`risk_policy_body` SHALL contain exactly `schema_version`, `long_only`,
`leverage_allowed`, `target_weight_min`, `target_weight_max`, and
`gross_target_weight_limit`, with schema `rqgm-trusted-local-risk-policy/1`, true long-only,
false leverage, finite `0 <= min <= max <= 1`, and finite gross limit in `(0, 1]`.

The materializer and loader SHALL be read-only and SHALL NOT accept a mutable path, latest
alias, current-database fallback, callback, candidate result, downstream RQGM plan, readiness
override, or authority grant. Missing, extra, reordered, non-canonical, result-shaped, or
identity-drifted content SHALL fail before any replay consumer can read a market row or policy
value.

#### Scenario: Exact verified bytes are materialized
- **WHEN** every explicit component and policy body reproduces the export and independent closure
- **THEN** the provider returns one immutable research-only materialization without changing readiness or authority

#### Scenario: A reference resolves through mutable state
- **WHEN** materialization requires a current database, latest artifact, cache fallback, path alias, or caller default
- **THEN** materialization fails rather than claiming that the verified export is executable

### Requirement: A formal bundle resolves to explicit research inputs without authority upgrade
The provider SHALL expose a read-only resolver for one lexical-absolute formally published
`bundle.json` plus one mandatory outcome-blind replay-policy binding. The resolver SHALL
retain every no-follow locator descriptor through bundle, physical-identity, continuity,
semantic, and final ABA verification and SHALL fail if validation or descriptor cleanup
fails. It SHALL derive the independent expected closure from the verified provider bytes,
not from a candidate research export or mutable current state.

The resolver SHALL require `/5 trusted_local_mechanical`, the exact ordered 36-cell panel,
successful collector ordinals `0..11`, exact receipt closure, separate adjustments, and all
nine mechanically complete canonical component payloads. It SHALL compute mechanical
completeness with existing provider semantic verifiers and MUST NOT trust a caller boolean.
Its exact `stockdata-rqgm-research-replay-resolved-inputs/1` return object SHALL contain only
`schema_version`, `expected_bindings`, and `component_payloads`. It SHALL contain no path,
readiness upgrade, research export, materialization, policy body, downstream plan, result,
callback, signature, or authority grant.

#### Scenario: Verified bundle bytes close completely
- **WHEN** one formal `/5` bundle, its twelve terminal steps, all receipts, adjustments, nine components, and replay policy independently verify
- **THEN** the resolver returns explicit expected bindings and canonical component payloads that the existing export and materialization builders accept separately

#### Scenario: Candidate export is offered as the expected source
- **WHEN** a caller supplies a research export, current database, latest alias, result, readiness override, or claimed completeness boolean
- **THEN** resolution rejects without returning partial inputs or changing ordinary provider readiness

### Requirement: A completed retained collector snapshot materializes atomically
The provider SHALL expose one production materializer CLI that accepts a `/5` registration,
its collector database, and an output directory and SHALL require the exact registered twelve
terminal collector steps before any output or staging write. It SHALL retain the no-follow
registration, database, ledger, prerequisite, and derived-file identities through semantic
verification, final ABA verification, publication, and cleanup, and MUST NOT reopen a mutable
live path.

From that one retained snapshot, the bridge SHALL derive the exact panel, separate execution
and signal adjustment files, and all nine canonical component files consumed by
`materialize_provider_bundle`. Calendar, market rules, and exactly their two receipts SHALL
come from registration `prerequisite_files`; intrinsic and forward components SHALL reuse the
production reconstruction APIs; component availability SHALL be produced by a production
builder over the verified canonical components and bidirectionally closed receipt domains.
Fixtures, callbacks, caller-supplied semantic bytes, guessed adjustment or positive-event
semantics, and partial component substitution MUST fail closed.

The bridge SHALL stage the complete output privately, verify its canonical bundle closure,
and publish only by one final rename. Any failure SHALL leave no published bundle or partial
output. The ordinary `/5` provider export SHALL remain `ready=false`, and the bridge SHALL
grant no task 8.6b, readiness, Judge, release, production, or advice authority.

#### Scenario: One completed snapshot closes every materializer input
- **WHEN** the registration and retained collector snapshot contain twelve terminal steps, exact prerequisite bytes, unambiguous adjustments, and reconstructible intrinsic, forward, and availability closure
- **THEN** the CLI publishes one canonical provider bundle by final rename and the existing resolver, export builder, and materialization builder accept its bytes

#### Scenario: The collector is incomplete
- **WHEN** any registered terminal step is absent, failed, dangling, duplicated, or reordered
- **THEN** the CLI stops before staging or output creation and publishes nothing

#### Scenario: A mutable or caller-created semantic input is offered
- **WHEN** materialization would require reopening a live path, using a fixture or callback, accepting caller component bytes, or guessing adjustment or positive corporate-action semantics
- **THEN** materialization fails closed without substituting or partially publishing input files

#### Scenario: Final identity or cleanup fails
- **WHEN** any retained identity changes after reconstruction or validation, publication fails, or descriptor cleanup reports an error
- **THEN** no output directory becomes visible and ordinary readiness and authority remain unchanged

### Requirement: The provider owns one one-shot trusted-local research projection bridge
The provider SHALL expose one read-only bridge that accepts exactly one lexical-absolute
formally published `bundle.json` and one canonical
`stockdata-rqgm-trusted-local-research-replay-policy-request/1`. The policy request SHALL
contain exactly `schema_version`, `replay_policy_binding`, `shared_cash_policy_body`, and
`risk_policy_body`; no pre-resolved closure, component payload, candidate export,
materialization, RQGM plan, result, callback, or alternate implementation is accepted.

The bridge SHALL call `resolve_trusted_local_research_replay_inputs`,
`build_trusted_local_research_replay_export`, and
`build_trusted_local_research_replay_materialization` exactly once each and in that order.
It SHALL pass the resolver's expected closure unchanged through export and materialization
and SHALL pass only the resolver's component payloads and the request's verified policy
bodies into materialization. RQGM SHALL consume only the bridge envelope and MUST NOT call,
replace, or compose the underlying provider steps.

The exact `stockdata-rqgm-trusted-local-research-replay-envelope/1` output SHALL contain only
`schema_version`, `provider_export`, `provider_expected_bindings`, and
`provider_materialization`, using the already verified canonical outputs. Any input,
intermediate, or envelope failure SHALL return no partial output. The bridge SHALL perform no
replay and create no second resolver, export, materialization, market-data authority, writer,
readiness decision, or downstream authority.

#### Scenario: One formal bundle and canonical policy request close
- **WHEN** the formal bundle and policy request pass and all three fixed provider calls return mutually closed canonical outputs
- **THEN** the provider returns exactly one canonical bridge envelope for RQGM to consume as a unit

#### Scenario: A caller attempts to compose provider steps
- **WHEN** a caller supplies an intermediate output, alternate step, callback, result-shaped input, or detached policy body
- **THEN** the bridge rejects without a partial envelope, replay, write, readiness change, or authority grant
