## ADDED Requirements

### Requirement: A prospective collector has a pre-registration genesis and stable file identity
The provider SHALL create a new collector database and canonical continuity ledger exclusively before registration. It SHALL bind no-follow regular-file identities, parent-directory identities, a unique immutable database UUID, the exact cohort, and a cross-bound genesis hash. Normal SQLite writes MUST preserve the bound device, inode, and UUID; mutable byte hashes, timestamps, sizes, and platform birth time MUST NOT define live database identity.

#### Scenario: Normal collector write preserves identity
- **WHEN** a valid registered capture appends evidence to the original SQLite file
- **THEN** the database device, inode, UUID, and genesis remain equal to registration while the logical state advances

#### Scenario: Same-path database is replaced
- **WHEN** another regular SQLite file with the same schema and cohort replaces the registered path
- **THEN** capture is rejected before any provider request because its physical identity or database UUID differs

#### Scenario: Database or ledger path is indirect or non-regular
- **WHEN** any bound path resolves through a substituted parent, symlink, FIFO, socket, directory, or other non-regular final entry
- **THEN** preparation, registration, capture, materialization, and export fail closed

#### Scenario: Same-inode database is rolled back
- **WHEN** the registered inode contains an older logical collector state while the ledger retains a later head
- **THEN** continuity verification rejects the state-head mismatch before capture or export

### Requirement: Finalized collector evidence is append-only
The `/4` collector SHALL reject update or deletion of genesis, cohort, source receipts, finalized daily prices, context, universe, status, and corporate-action evidence. Every inserted collector `daily` row SHALL have `NEW.is_final=1`; a non-final insert MUST be rejected. A repeated finalized price key MAY be treated as an idempotent no-op only when every market value, provenance value, finality value, and receipt binding is identical. Sync coverage MAY only widen monotonically for the same fixed identity. An update with the exact existing identity, `start_date`, and `end_date` SHALL use `RAISE(IGNORE)` and preserve the old `retrieved_at`; only real monotonic widening MAY update `retrieved_at`.

#### Scenario: Identical finalized price is encountered on retry
- **WHEN** a retry encounters a finalized row whose complete persisted payload and receipt binding equal the existing row
- **THEN** the collector performs no semantic write and preserves the original evidence

#### Scenario: Finalized price differs on retry
- **WHEN** a retry attempts to change any price, volume, provenance, finality, timestamp, or receipt binding of an existing finalized row
- **THEN** the write is rejected and no existing evidence is modified

#### Scenario: Coverage is rolled back or changes identity
- **WHEN** a writer tries to shrink sync coverage or change its source, adjustment mode, adjustment version, or cohort symbol
- **THEN** the collector rejects the update

#### Scenario: Coverage retry is already exact
- **WHEN** a retry presents the existing fixed identity and the same start and end dates
- **THEN** the update is ignored and the existing retrieved timestamp remains byte-identical

#### Scenario: Non-final collector price is inserted
- **WHEN** a collector writer attempts to insert a daily row whose finality is not one
- **THEN** the insert is rejected and no receipt, price, or coverage evidence from that transaction is admitted

### Requirement: Collector continuity is an append-only hash-chained audit record
The provider SHALL maintain a canonical JSONL ledger whose events have an exact schema, monotonic sequence, previous-event hash, and self hash. It SHALL contain one genesis, one registration binding, and for every step attempt exactly one durable start followed by exactly one durable completion or failure. The exact unreleased ledger detail schema `/1` SHALL require `lease_nonce_sha256` and the complete exact `step_state_before` in `ATTEMPT_STARTED`, with `state_before_sha256` equal to its aggregate hash. `ATTEMPT_COMPLETED` and `ATTEMPT_FAILED` SHALL require the complete exact `step_state_after`, with `state_after_sha256` equal to its aggregate hash and the pre-state identity equal to the matching start. These are mandatory frozen corrections to the unreleased `/1` schema; no optional or compatibility form without them exists. Existing events MUST NOT be edited, deleted, reordered, truncated, or compacted.

#### Scenario: Attempt starts before the child process
- **WHEN** a registered step is ready to execute
- **THEN** the parent generates a fresh 256-bit lease nonce, appends and fsyncs `ATTEMPT_STARTED` with its mandatory `lease_nonce_sha256` and complete consistent `step_state_before`, and transfers the raw nonce once through an inherited anonymous pipe before the provider child process can write

#### Scenario: Ledger line or chain is modified
- **WHEN** an event field, ordering, sequence, previous hash, event hash, registration binding, or tail is inconsistent
- **THEN** capture, materialization, and export reject the ledger

#### Scenario: Child output claims success
- **WHEN** stdout reports ready or success but the child return code or independent postcondition fails
- **THEN** the attempt is durably failed and cannot be reported complete

### Requirement: Collector capture is single-writer and totally ordered
The registered orchestrator SHALL hold a non-blocking exclusive ledger lock through a complete phase and SHALL duplicate its lock descriptor for each child writer. The primitive lock check proves only the bound ledger identity and current held-lock state, not inheritance provenance. A collector-marked database MUST reject a direct writer unless the child also proves the matching active attempt by reading exactly 32 nonce bytes followed by EOF from the inherited one-use pipe and matching their SHA-256 to the ledger tail. The raw nonce MUST NOT appear in the ledger, argv, environment, stdout, stderr, or diagnostics; the child MUST NOT call `LOCK_UN`; parent pipe ends and descriptor duplicates MUST be closed immediately after process creation. Every sorted session SHALL use stable IDs `pre_open_context`, `pre_open_corporate_actions`, `post_close_context`, and `post_close_prices` in that order. `step_ordinal` SHALL be the global zero-based `session_index * 4 + local_step_index`, exactly `0..11` for the registered three-session panel. The normalized command hash SHALL cover canonical `stockdata-forward-collector-command/1` JSON containing the exact argv tuple with resolved absolute Python executable, bound canonical database, sorted normalized symbols, ISO date bounds, and registered source/raw adjustment identity. Reordered, aliased, unknown, or extra argv MUST be rejected. All earlier sessions MUST be complete.

#### Scenario: Two processes invoke the same collector
- **WHEN** another capture or materialization process holds the collector lock
- **THEN** the competing invocation fails before appending an attempt or calling a provider

#### Scenario: Phase is duplicate or out of order
- **WHEN** a completed phase is invoked again or a phase/step/date is invoked before any required predecessor
- **THEN** the invocation is rejected before a provider request and no ledger event falsely records an attempt

#### Scenario: Direct collector subcommand bypasses registration
- **WHEN** a forward capture subcommand targets a collector-genesis database with no active attempt, a separately opened self-locked descriptor, a missing or wrong nonce, a truncated or overlong nonce pipe, or a nonce from a terminal or different attempt
- **THEN** it fails before opening a market-evidence write transaction

#### Scenario: Registration changes after binding
- **WHEN** the registration bytes, prerequisite hash, panel, sessions, or database binding differ from `REGISTRATION_BOUND`
- **THEN** capture and materialization reject the registration

### Requirement: Step completion is established by raw postconditions
For every attempt, the provider SHALL compute one exact `stockdata-forward-collector-step-state/1` object in the same read-only SQLite snapshot. It SHALL contain exactly the aggregate collector hash, exact ten-table counts, exact ten-table `table_sha256`, `outside_scope_sha256` with exactly the current step's allowed-table names, and a non-negative `receipt_id_high_water`. Table and selector-complement digests SHALL reuse task 2.2 typed-cell encoding, canonical framing, frozen columns, and primary-key ordering with distinct domains. Every disallowed whole-table digest and every allowed-table selector-complement digest MUST remain unchanged.

Context steps SHALL allow only receipts, context, universe, and status: the phase observation and one attributable receipt are exact, universe equals the full raw response and contains the cohort, and status equals the cohort. Corporate actions SHALL allow only receipts, action coverage, and action events: every cohort symbol has exact coverage including zero events and events reconstruct exactly from the response. Prices SHALL allow only receipts, daily, and sync coverage: the command range is `cohort_start..session`, newly persisted daily rows are restricted to the current session and exact cohort/raw identity, the complete post-state has one finalized row per cohort symbol, and coverage is exactly `cohort_start..session`. Receipts above the pre-state high-water mark MUST be fully attributable, referenced, timely, and non-orphaned; earlier receipts remain immutable. Exact price or coverage retries are no-ops.

After each child exits, the provider SHALL reverify file identity, database UUID, schema, ledger head, complete step state, raw receipt request/response hashes and bindings, and Shanghai phase timing. It SHALL append completion only when the child returns zero and the independent postcondition proves the exact selector and timely cohort coverage. Return code, stdout, child readiness, or aggregate table counts MUST NOT independently establish completion.

#### Scenario: Context child returns zero without complete rows
- **WHEN** the context child exits zero but exact phase observation, universe, status, or timely receipt coverage is missing
- **THEN** the attempt is failed and the next step does not run

#### Scenario: Corporate actions include zero-event symbols
- **WHEN** the pre-open action step has timely exact-cohort coverage including explicit zero-event records
- **THEN** its postcondition can complete without inventing corporate-action events

#### Scenario: Price child commits a foreign identity
- **WHEN** price capture writes another symbol, date, source, adjustment identity, unfinished row, or unbound receipt
- **THEN** the attempt becomes non-retryable and the registration is quarantined

#### Scenario: Exact step postcondition passes
- **WHEN** the child returns zero and raw rows, receipts, timing, cohort, and allowed logical delta all verify
- **THEN** exactly one fsynced `ATTEMPT_COMPLETED` event records the complete consistent `step_state_after` and resulting aggregate logical state

#### Scenario: Same allowed table contains foreign evidence
- **WHEN** an allowed table changes outside the current step's exact date, phase, cohort, source, adjustment, or receipt selector
- **THEN** its selector-complement digest changes and the attempt is non-retryable even if aggregate counts appear plausible

### Requirement: Interrupted attempts recover without self-reporting success
The provider SHALL classify a dangling started attempt only after acquiring the lock, completing any controlled rollback-journal recovery, and comparing the persisted complete `step_state_before`, selector complements, receipt high-water mark, and current raw state. It SHALL preserve the interrupted attempt, append a terminal recovery classification with a complete consistent `step_state_after`, and use a new attempt ID for every retry.

#### Scenario: Interrupted child committed nothing
- **WHEN** a dangling attempt's current logical state equals its recorded pre-state
- **THEN** the provider appends a retryable interrupted failure and permits a new attempt

#### Scenario: Interrupted atomic step is independently complete
- **WHEN** a dangling context or corporate-action attempt has an exact complete raw postcondition
- **THEN** the provider may append recovered completion with the verifier identity and MUST NOT infer success from missing process status

#### Scenario: Interrupted price step committed a valid subset
- **WHEN** only a valid monotonic subset of registered price rows and receipts was committed
- **THEN** the provider appends a retryable partial-commit failure and a later attempt collects only the missing subset

#### Scenario: Interrupted step contains forbidden drift
- **WHEN** recovery finds overwrite, deletion, rollback, foreign data, malformed receipt, or an unclassifiable delta
- **THEN** it appends a non-retryable failure and all later collection for that registration is blocked

### Requirement: Registration `/4` binds continuity and legacy registration remains blocked
The provider SHALL emit and accept `rqgm-forward-panel-registration/4` only after recomputing static prerequisites, collector capability, physical identities, UUID, genesis, cohort, and ledger state. It SHALL bind the exact canonical registration hash into the ledger. A byte-identical unbound registration MAY finish a missing crash-time bind without overwrite. Registration `/3` and earlier MUST NOT enter the `/4` capture, materialization, or execution-grade path.

#### Scenario: New future `/4` registration is bound
- **WHEN** a clean future collector and every existing static prerequisite pass
- **THEN** one canonical registration is written without overwrite and exactly one matching registration-bound event is fsynced

#### Scenario: Crash leaves byte-identical registration unbound
- **WHEN** registration bytes were written but the process stopped before the ledger binding
- **THEN** recomputation may append the missing binding only if the bytes and all prerequisites are identical

#### Scenario: Legacy `/3` registration is presented
- **WHEN** a `/3` or earlier registration or database without pre-registration genesis is presented
- **THEN** it is rejected and cannot be migrated, replayed, or repaired into point-in-time evidence

### Requirement: Provider snapshots preserve continuity without granting readiness
Provider materialization SHALL hold the continuity lock, verify the complete registered schedule and logical tail, and use a consistent SQLite snapshot mechanism. Provider bundle input `/2` SHALL bind the registration, ledger snapshot, continuity closure, and database snapshot. Read-only export SHALL reverify those artifacts. Continuity MAY reject materialization or export but MUST NOT supply component records, signed authority, availability evidence, or a positive readiness decision.

#### Scenario: Materialization races with capture
- **WHEN** capture holds the collector lock or the database changes during snapshot preparation
- **THEN** materialization fails without producing a provider bundle

#### Scenario: Bundle has valid components but broken continuity
- **WHEN** nine component artifacts appear ready but registration, ledger, closure, schedule, UUID, or logical state fails continuity verification
- **THEN** provider materialization or export rejects the bundle and does not expose ready

#### Scenario: Continuity is valid but evidence is incomplete
- **WHEN** the continuity closure verifies but any original component, receipt, signed authority, phase cutoff, or availability requirement is missing
- **THEN** the existing readiness computation remains blocked

#### Scenario: Complete bundle is independently reverified
- **WHEN** continuity and all existing nine-component requirements independently pass from the frozen snapshot
- **THEN** the provider may preserve the recomputed readiness result without using continuity as evidence for any component

### Requirement: RQGM compatibility remains fail closed
The stock-data provider SHALL keep the verified RQGM export envelope compatible while advancing only its local bundle input to `/2`. Cross-repository fixtures SHALL prove that RQGM rejects legacy, missing, malformed, drifted, or unverifiable continuity inputs and accepts no caller-supplied continuity token. External signer enrollment, signed authority, future collection, immutable Judge evidence, and release authorization SHALL remain separate blockers.

#### Scenario: RQGM receives a legacy or forged continuity path
- **WHEN** a provider bundle omits `/2` continuity artifacts or a caller supplies a fabricated continuity claim
- **THEN** stock-data export fails closed before RQGM can receive an authoritative ready envelope

#### Scenario: RQGM receives verified blocked evidence
- **WHEN** provider continuity passes but current external authority or future-data blockers remain
- **THEN** the RQGM consumer preserves the blocked result and grants no release or trading authority

#### Scenario: Continuity implementation is complete without external enrollment
- **WHEN** all local continuity code and fixtures pass but production trust roots, signers, or future panel observations are absent
- **THEN** the project remains externally data/authority blocked and MUST NOT claim task 8.6, economic evidence, or release completion
