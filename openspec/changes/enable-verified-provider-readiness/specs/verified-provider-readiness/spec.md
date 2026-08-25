## ADDED Requirements

### Requirement: Intrinsic components are reconstructed from bound evidence
The provider SHALL reconstruct execution-price, signal-price, and decision-context
component payloads from the immutable database, exact panel, declared adjustment
identities, and bound source receipts. It MUST byte-compare each reconstructed payload with
the exported component artifact and MUST NOT accept caller-supplied readiness as evidence.

#### Scenario: Caller supplies a ready price artifact not supported by the database
- **WHEN** the claimed execution-price component contains a row absent from the bound database or receipt set
- **THEN** component verification fails and aggregate readiness remains false

#### Scenario: Raw and signal price identities are distinct and complete
- **WHEN** raw execution rows and declared signal-adjusted rows both reconstruct exactly for every panel entry
- **THEN** the two components retain separate identities and may contribute positive readiness

### Requirement: Externally published components require enrolled admission
Trading calendar, universe, instrument status, corporate actions, and market rules SHALL
contribute positive readiness only after their canonical artifacts pass an enrolled trust
root, authorized signer, signature, source-receipt, effective-time, availability-time, and
exact-panel verification. Local self-signing or an unknown signer MUST fail closed.

#### Scenario: All content is present but the signer is not enrolled
- **WHEN** a valid signature uses a key absent from the production trust registry
- **THEN** the component remains blocked and the materializer cannot emit a ready receipt

#### Scenario: Enrolled component is modified after signing
- **WHEN** any canonical component byte differs from the signed artifact reference
- **THEN** admission and bundle reverification fail

### Requirement: Availability records bind the actual evidence closure
Availability verification SHALL recompute and bind every record used by the other eight
components to its component identity, panel entry, canonical record hash, source receipt,
event time, availability time, and the signed-calendar cutoff applicable to the phase that
consumes it. Decision inputs MUST be available by the same-session decision cutoff.
Finalized execution-price and signal-price bars MUST become available no earlier than the
signed session close and no later than the signed next-session decision cutoff. Every
required record MUST appear exactly once, and no orphaned or out-of-window record may pass.

#### Scenario: Availability contains a valid but unrelated hash
- **WHEN** an availability row names a syntactically valid hash that is not the hash of the bound component record
- **THEN** availability readiness is false

#### Scenario: One component record is missing from availability
- **WHEN** any evaluated price, context, calendar, universe, status, action, or rule record lacks a matching availability row
- **THEN** the exact panel remains blocked

#### Scenario: Decision input was observed after the decision cutoff
- **WHEN** a non-price decision input's bound availability time is later than the signed same-session decision cutoff
- **THEN** the row cannot satisfy readiness even if its content is historically correct

#### Scenario: Final price bar claims pre-finalization availability
- **WHEN** an execution-price or signal-price record claims availability before the signed session close
- **THEN** the row cannot satisfy readiness

#### Scenario: Final price bar misses the next-session cutoff
- **WHEN** an execution-price or signal-price record is unavailable by the signed next-session decision cutoff
- **THEN** the row cannot satisfy readiness even if it is later present in the cache

### Requirement: Market-rule authority is execution complete
The market-rule component SHALL bind the complete dated rule surface consumed by canonical
A-share replay, including scope and validity, lot size, T+1, listing age, suspension,
price-limit behavior and exceptions, commission, minimum fee, transfer fee, stamp duty,
slippage, and order lifecycle. A signature over a partial or self-reported policy identifier
MUST NOT satisfy readiness.

#### Scenario: Signed rule artifact omits minimum commission
- **WHEN** an otherwise valid market-rule artifact lacks the minimum-fee rule used by replay
- **THEN** market-rule readiness and aggregate readiness are false

#### Scenario: Dated rule regimes cover the exact panel
- **WHEN** every panel entry resolves to exactly one signed, complete, effective rule regime
- **THEN** the market-rule component may contribute positive readiness

### Requirement: Positive readiness is a nine-component conjunction
The materializer SHALL generate a fresh readiness report whose aggregate `ready` value is
true if and only if all fixed nine components are independently ready, every component and
aggregate blocker list is empty, and the request, exact panel, database, receipt set,
adjustments, and companion snapshot identities match. A missing or unverified component
MUST produce a valid blocked bundle rather than a partial success.

#### Scenario: Nine verified components materialize successfully
- **WHEN** every component and identity passes its verifier with no blockers
- **THEN** materialization, read-only export, and a second byte-for-byte reverification all return `ready=true`

#### Scenario: Eight components pass
- **WHEN** exactly one required component is missing, blocked, or unverified
- **THEN** aggregate readiness is false and identifies that component

#### Scenario: Readiness report boolean is forged
- **WHEN** a caller changes only aggregate or component `ready` fields without changing the bound evidence
- **THEN** export reverification rejects the report

### Requirement: Positive fixtures cannot grant production authority
Positive end-to-end tests SHALL use isolated ephemeral trust and synthetic exact-panel
artifacts. Production trust loading MUST reject those identities, and test success MUST NOT
be represented as task 8.6, E2, elite, release, or profit evidence.

#### Scenario: Synthetic signer is presented to production admission
- **WHEN** a positive test fixture is loaded through the production trust registry
- **THEN** admission fails and no production-ready receipt is emitted

### Requirement: Post-hoc market caches remain research grade
Rows downloaded after their applicable historical consumption cutoff SHALL remain in retrospective
research scope unless an independently admitted source authority proves the original
event and availability times. Cache completeness alone MUST NOT upgrade evidence grade.

#### Scenario: Tonghuashun row is complete but has no timely receipt
- **WHEN** a historical adjusted-price row exists in the local cache without an original receipt inside its signed post-close to next-session-decision window
- **THEN** it may support retrospective research but cannot satisfy execution-grade readiness

### Requirement: Future panel registration is prerequisite-gated
The provider SHALL register a new capture panel only when it is a future, outcome-blind,
exact 12-symbol by 3-session product; the pinned production registry has independently
authorized signer coverage for all five external roles through the final collection
window; calendar passes full admission; and a signed generic market-rulebook passes
registration-prerequisite verification for every exact symbol/date, board/exchange,
listing-age interval, and explicit ST/non-ST branch with no overlap or gap. The generic
rulebook prerequisite MUST NOT be represented as `market_rules` readiness authority and
MUST NOT contain an execution wildcard. A clean collector database with a pre-bound
immutable cohort MUST pass read-only schema, identity, sync-coverage, and append-only
verification. Registration MUST bind those recomputed identities and MUST NOT trust caller
readiness booleans. Before each capture phase, the provider MUST re-admit the calendar and
generic rulebook prerequisite against current production trust and recompute the
collector/cohort fingerprint. Execution materialization and export MUST separately admit
the exact `instrument_status`, select exactly one explicit market rule, and bind the
selected rulebook, status artifact, and calendar cutoff before `market_rules` can be ready.

#### Scenario: Production trust enrollment is empty
- **WHEN** an otherwise complete synthetic panel is presented while the pinned production registry has no enrolled signers
- **THEN** registration is rejected and no registration file is written

#### Scenario: Generic rulebook cannot grant execution readiness
- **WHEN** a future registration contains a signed rulebook with generic ST/listing-age coverage
- **THEN** it may form only a registration prerequisite; execution readiness remains blocked
  until an exact admitted instrument status selects one explicit rule

#### Scenario: Collector append-only protection drifted
- **WHEN** any required collector trigger is missing or differs from the frozen definition
- **THEN** registration is rejected before the panel can be captured

#### Scenario: Registration-time database is replaced before capture
- **WHEN** the bound database schema, cohort, or append-only triggers differ at capture time
- **THEN** capture is rejected before any provider request runs

#### Scenario: Expired registration is presented for capture
- **WHEN** a registered session is invoked on a later calendar date
- **THEN** capture is rejected and historical rows cannot be backfilled through the forward path
