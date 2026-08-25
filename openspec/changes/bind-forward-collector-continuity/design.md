## Context

`future-panel-prepare` currently creates a new SQLite file, installs the forward schemas, and binds the fixed cohort. `future-panel-register` records `rqgm-forward-panel-registration/3`, including the canonical database path and a schema/cohort/trigger fingerprint. Before capture, `registered-panel-capture` recomputes that fingerprint, then `forward_panel_capture.capture_phase` launches two external CLI steps and treats a zero return code as success.

This protects the shape of the collector but not its continuity. A database can be replaced by another file at the same path with the same schema and cohort. A same-inode rollback can restore older rows and the same cohort. The parent process also has no durable record distinguishing an unstarted step, a running step, a child that committed before the parent crashed, a partial per-symbol price commit, or a completed step.

The collector is deliberately local. The design must detect replacement, rollback, process crashes, uncoordinated writers, duplicate phases, and ordering drift without treating local process metadata as market evidence. It must preserve the existing nine-component readiness authority and the external signer, trust-root, future observation, Judge, and release boundaries.

## Goals / Non-Goals

**Goals:**

- Give every newly prepared prospective collector an immutable genesis and stable physical identity that survives normal SQLite writes but rejects path replacement.
- Make finalized raw collector prices append-only and retries idempotent without permitting semantic overwrite.
- Persist every registered capture attempt in an append-only, hash-chained ledger before and after each external subprocess.
- Enforce one total order over all registered dates, phases, and steps; reject concurrent, duplicate, stale, or drifted invocations before a provider request.
- Recover deterministically after parent or child crashes without inferring success from an exit code or stdout.
- Bind registration `/4`, live capture, consistent provider snapshotting, and provider bundle input `/2` to the same verified continuity closure.
- Keep continuity incapable of granting component or aggregate `READY`.

**Non-Goals:**

- The ledger is not a source receipt, signed market fact, component artifact, Judge record, or release authorization.
- This change does not enroll production signers, create authority keys, collect future data, produce economic replay evidence, or release an RQGM agent.
- This change does not make an expired or legacy `/3` registration prospective.
- This change does not defend against a privileged or same-user adversary that can coherently rewrite the database, ledger, registration, and their containing directories while preserving every bound identity. That stronger threat model requires an external signature, WORM store, or separately administered anchor.
- General research databases that do not contain collector genesis remain outside the collector lease protocol.

## Decisions

### 1. Bind physical identity and an immutable database UUID, not mutable file metadata

Preparation SHALL create a private control ledger and the SQLite database with exclusive creation. File access SHALL traverse the canonical parent path without following symlinks, open the final entry with `O_NOFOLLOW`, and verify with `fstat` that it is a regular file. The bound identity contains:

- canonical path;
- canonical parent-directory `st_dev` and `st_ino`;
- file `st_dev` and `st_ino`;
- a locally generated 256-bit `database_uuid` stored in `forward_collector_genesis`;
- the canonical genesis payload hash.

The genesis table has exactly one row and immutable update/delete protection. The database identity does not include bytes, size, mtime, ctime, or platform birth time because those values change during normal SQLite operation or are not portable. Standard writes and checkpoints keep the main file device, inode, and UUID stable. A move, copy, atomic replacement, symlink substitution, or newly prepared same-schema database fails identity verification.

The sidecar ledger receives the same treatment and has its own device/inode identity. Its first `GENESIS` event cross-binds the database UUID, database identity, cohort hash, ledger identity, and collector schema versions. Registration later binds the exact genesis event hash.

Alternative considered: hash the complete database at registration. Rejected because a collector is intentionally writable and every valid capture changes the bytes. Alternative considered: rely only on device/inode. Rejected because an in-place rollback preserves them.

### 2. Freeze the collector SQLite mode and finalized-row semantics

Collector connections SHALL use `journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`, a bounded busy timeout, and exact schema verification without automatic migration. Entry and exit verification reject WAL mode and any `-wal` or `-shm` sidecar. A hot rollback journal is handled only while the continuity lock is held and only after a durable recovery-start event; a controlled SQLite connection performs recovery before continuity is classified.

The `/4` collector schema adds finalized-price insert/update/delete guards. Every inserted `daily` row must have `NEW.is_final=1`; a non-final insert fails before evidence is written. A finalized `daily` row can be inserted once. Encountering the same primary key is an idempotent no-op only when every persisted market value, provenance field, finality flag, and receipt binding is identical; otherwise the write fails. `collection_receipts`, cohort, genesis, context, status, universe, and corporate-action rows remain append-only. `sync_coverage` may only widen monotonically for the same fixed identity; it cannot shrink coverage or change source identity. An update whose identity, `start_date`, and `end_date` exactly equal the existing row uses `RAISE(IGNORE)` so the old `retrieved_at` is preserved; only a real monotonic widening may update `retrieved_at`.

Alternative considered: support WAL immediately. Rejected because provider materialization currently copies one file, WAL introduces multi-file snapshot identity, and the small 12-by-3 collector does not need WAL concurrency.

### 3. Use a separate canonical JSONL ledger as the continuity anchor

The ledger stays outside the mutable evidence database so a database rollback cannot also roll back its latest process history. Each line is canonical ASCII JSON with an exact schema. Each event contains `seq`, `event_type`, `previous_event_sha256`, and `event_sha256`, where the event hash is computed over the canonical event excluding `event_sha256`.

Event types are:

1. `GENESIS`;
2. exactly one `REGISTRATION_BOUND`;
3. `ATTEMPT_STARTED` for each step attempt;
4. exactly one terminal `ATTEMPT_COMPLETED` or `ATTEMPT_FAILED` for that attempt;
5. `SQLITE_RECOVERY_STARTED` and a terminal recovery event when hot-journal recovery is required.

Attempt events bind the registration hash, database UUID, date, phase, stable step ID and ordinal, attempt ID, normalized command hash, timestamps, logical database state before and after, return code when known, stdout/stderr hashes and byte counts when known, recovery classification, and retryability. The exact `ATTEMPT_STARTED` detail schema additionally requires `lease_nonce_sha256`, the lowercase SHA-256 of a fresh 256-bit random nonce generated for that attempt, and the complete exact `step_state_before` object. Its existing `state_before_sha256` must equal `step_state_before.collector_state_sha256`. The exact `ATTEMPT_COMPLETED` and `ATTEMPT_FAILED` detail schemas require the complete exact `step_state_after` object, and `state_after_sha256` must equal `step_state_after.collector_state_sha256`; terminal events retain the same recorded pre-state identity as their matching start. Ledger event schema `/1` is not yet released, so these fields are mandatory frozen corrections rather than optional extensions, and there is no compatibility form without them. The writer holds the lock, performs one bounded append, and calls `fsync` before continuing. Existing lines are never rewritten, deleted, or compacted.

Alternative considered: store the ledger in the same SQLite database. Rejected because a restored database copy would restore both evidence and continuity history to the same older head.

### 4. Define a deterministic logical collector state

`collector_state_sha256` is computed in one read-only consistent SQLite transaction. It hashes canonical, primary-key-ordered rows and table counts for the collector-owned tables: genesis, cohort, daily, collection receipts, sync coverage, context, universe, status, corporate-action coverage, and corporate-action events. SQLite page layout, freelists, journal files, and the external ledger are excluded.

Every attempt also freezes one exact `stockdata-forward-collector-step-state/1` object containing exactly `schema_version`, `collector_state_sha256`, `table_counts`, `table_sha256`, `outside_scope_sha256`, and `receipt_id_high_water`. `table_counts` and `table_sha256` have exactly the ten collector-owned table names. `outside_scope_sha256` has exactly the current step's allowed-table names and hashes the complement of that step's selector. `receipt_id_high_water` is the non-negative maximum receipt ID, or zero when the receipt table is empty. Aggregate state, every table digest, every selector-complement digest, all counts, and the high-water mark are computed in the same read-only SQLite snapshot. Table and complement digests reuse the task 2.2 typed-cell encoding, canonical record framing, frozen column order, and primary-key order with separate domain labels; they do not hash `SELECT *`, rowid, page layout, or caller conversion policy.

The four allowed-table and selector contracts are exact:

- `pre_open_context` and `post_close_context` allow only `collection_receipts`, `forward_context_observations`, `forward_universe_observations`, and `forward_status_observations`. Evidence selectors are the registered session, the respective phase, and the fixed context source. Universe rows may contain the full-market symbol set reconstructed from the one bound response, but status rows are exactly the registered cohort and that full-market set must contain the cohort.
- `pre_open_corporate_actions` allows only `collection_receipts`, `forward_corporate_action_coverage`, and `forward_corporate_actions`. Evidence selectors are the registered session, exact cohort, and fixed corporate-action source; every cohort symbol has one coverage row, including explicit zero-event coverage.
- `post_close_prices` allows only `collection_receipts`, `daily`, and `sync_coverage`. Its command requests `cohort_start..session`, but newly persisted `daily` rows are restricted to the current registered session, exact cohort, raw source/adjustment identity, finality one, and bound source responses. Coverage rows are restricted to the exact cohort and identity and finish at `cohort_start..session`.

For every allowed table, the complement digest before and after must be identical; every disallowed whole-table digest must be identical. Receipts with IDs at or below `receipt_id_high_water` are pre-existing and immutable. Every later receipt must be attributable to the current selector, must be referenced by the exact step rows, and must not be orphaned or reused across a foreign step. The current aggregate state must equal the ledger tail's recorded state before any new attempt, after crash recovery, and before provider snapshotting. These checks catch same-inode rollback, unledgered direct writes, truncation of only the ledger, same-table foreign evidence, and cross-step mutation.

### 5. Serialize the phase and require an inherited collector lease

The orchestrator opens the ledger no-follow and takes a non-blocking exclusive `flock` for the complete phase. The lease primitive verifies only that the descriptor has the bound ledger identity and currently refers to the held lock; it does not claim to prove how that descriptor was acquired or inherited. For normal orchestration, the parent duplicates the locked descriptor for each child so a new orchestrator cannot acquire the lock while an orphaned child is still running. The child closes its duplicate but never calls `LOCK_UN`, because duplicated and inherited descriptors refer to the same lock.

A collector-marked database can be written by a forward capture subcommand only when the child receives the active attempt ID, inherited lock descriptor, and one-use lease nonce. Before appending `ATTEMPT_STARTED`, the parent generates a new 256-bit random nonce and records only `lease_nonce_sha256` in the exact event detail. The raw nonce never enters the ledger, stdout, stderr, command arguments, environment, or diagnostics. The parent writes the exact 32 raw bytes to a one-use anonymous pipe, closes its write end, and passes only the pipe read descriptor and a duplicate of the lock descriptor with `shell=False`, `close_fds=True`, and exact `pass_fds`; all parent-side pipe ends and descriptor duplicates are closed immediately after process creation.

The child first verifies the locked descriptor identity and current lock state, then parses the complete ledger and requires its tail to be the exact matching `ATTEMPT_STARTED` for the attempt ID, database UUID, registration hash, date, phase, step ID and ordinal, and normalized command hash. It reads exactly 32 bytes from the inherited pipe, requires immediate EOF with no truncation or trailing byte, hashes those bytes, and requires equality with the tail's `lease_nonce_sha256`. A separately opened and self-locked ledger descriptor is insufficient because it lacks the nonce preimage. The nonce expires when that attempt receives a terminal event and is never reusable; every retry receives a new attempt ID and nonce. Direct invocation without the complete match fails before a provider call or write transaction. Non-collector research databases retain their current behavior.

### 6. Make completion depend on independent postconditions

The registered 12-step schedule uses four stable IDs per sorted session:

1. `pre_open_context` in phase `pre_open`;
2. `pre_open_corporate_actions` in phase `pre_open`;
3. `post_close_context` in phase `post_close`;
4. `post_close_prices` in phase `post_close`.

`step_ordinal` is a global zero-based ordinal across the complete registration, equal to `session_index * 4 + local_step_index`; for three sorted sessions it is exactly `0..11`, never a session-local ordinal. The exact command tuple uses the resolved absolute `sys.executable`, `-m stockdata.cli`, the bound canonical database path, ISO session/cohort dates, sorted normalized cohort symbols, and the registered source/raw adjustment version. `command_sha256` hashes canonical ASCII JSON `{"schema_version":"stockdata-forward-collector-command/1","argv":[...]}`. The actual argv must equal the frozen tuple exactly; reordered, aliased, unknown, or extra argv is rejected rather than normalized into equivalence.

All steps of every earlier session must already be complete. A completed phase is not rerunnable. Wrong date, phase window, step order, registration hash, static prerequisite, file identity, chain head, logical state, or lock ownership is rejected before `ATTEMPT_STARTED` and before a provider request.

For each step, the parent generates the fresh lease nonce, appends and fsyncs `ATTEMPT_STARTED` with its mandatory hash, transfers the raw nonce once through the anonymous pipe while passing the duplicated lock descriptor, launches the child, reopens and reverifies the database path and identity, and runs a step-specific postcondition from raw rows and receipts:

- context requires exactly one phase observation and one newly attributable receipt; universe equals the complete raw full-market response, status equals the cohort, all rows share the receipt, and observation/creation times fall within the attempt and the exact Shanghai phase window;
- corporate actions require exactly one newly attributable receipt, exact cohort coverage including explicit zero-event rows, and event rows reconstructed byte-for-byte from the canonical response within the pre-open window;
- prices require exactly one finalized raw row for every cohort symbol on the current session after the complete step, exact response/request binding, no orphan receipt, no foreign date/symbol/source/adjustment identity, and exact `cohort_start..session` coverage; a partial retry may retain previously verified rows but may add receipts only for newly added rows, while exact rows and exact coverage are no-ops;
- every step independently recomputes raw receipt request/response hashes and bindings, compares the complete before/after step-state objects, and requires unchanged disallowed tables and unchanged allowed-table selector complements.

Only return code zero plus a passing independent postcondition can produce `ATTEMPT_COMPLETED`. A zero return code, stdout claiming readiness, or an idempotent child response alone is insufficient. Any other outcome appends `ATTEMPT_FAILED` and stops the phase.

### 7. Recover dangling attempts without rewriting history

After acquiring the lock, the next invocation handles a ledger tail with `ATTEMPT_STARTED` but no terminal event by comparing the persisted complete `step_state_before` with a newly computed step state and raw selector rows:

- If the current state equals `state_before`, append a retryable interrupted/no-commit failure and permit a new attempt.
- If an atomic context or corporate-action postcondition is exactly complete, independently verify it and append recovered completion.
- If the price step contains only a valid monotonic subset attributable to its registered cohort/date and all present rows and receipts verify, append a retryable partial-commit failure; a new attempt may collect only the missing subset.
- If the delta contains a deletion, overwrite, rollback, foreign table/date/symbol/source, malformed receipt, or an otherwise unclassifiable change, append a non-retryable failure and quarantine the registration.

Recovered completion records `recovered=true` and the independent verifier identity. It is not inferred from the missing child exit status. The old attempt is never edited, its nonce cannot authorize another child after terminal classification, and every retry receives a new attempt ID and fresh nonce.

### 8. Advance registration to `/4` and bind it into the ledger

Preparation advances the collector capability schema and returns both verified identities. Registration `/4` includes those identities and genesis hash in `prerequisites.collector`, while retaining the exact future panel, static authority prerequisites, outcome blindness, and exclusive canonical registration file.

After the registration bytes exist, registration appends one `REGISTRATION_BOUND` event containing the registration SHA-256, panel hash, sessions, and prerequisite hash. If a process crashes after writing the file but before binding it, a repeat with byte-identical recomputed input may finish the missing bind without overwriting the file. Any byte difference or an existing different binding fails closed.

Registration `/3` and earlier lack a pre-observation genesis and durable attempt history. Capture SHALL reject them; there is no migration or compatibility mode that can manufacture prospective evidence.

### 9. Snapshot and export continuity without making it readiness authority

Materialization acquires the same exclusive continuity lock, verifies the complete expected ledger schedule through the requested panel, and checks that the tail state equals the live database. It then uses the SQLite backup API to create a consistent immutable database snapshot while no capture can run. It snapshots the canonical registration, complete ledger, and a `stockdata-forward-collector-continuity-closure/1` report that binds the original live physical identity, database UUID, registration hash, ledger head, logical state, and snapshot database reference.

Provider bundle input advances to `/2` and requires locators for the registration, ledger snapshot, and continuity closure. Read-only export reruns the chain, registration, UUID, logical-state, schedule, and closure verification against the bundled database. Because the bundled snapshot has a new inode, frozen verification compares its UUID and logical state to the closure rather than requiring the live inode.

The verified RQGM-facing export envelope remains `stockdata-rqgm-provider-export/1`. Continuity is evaluated before exposing the existing report. A broken or missing closure rejects materialization/export; a valid closure contributes no component records, no signed authority, and no positive readiness evidence. The existing nine components must still independently recompute ready, and a valid continuity closure with any missing component remains blocked.

## Risks / Trade-offs

- [Coherent same-user rewrite can defeat unsigned local anchors] -> State this boundary explicitly and preserve external signer/WORM work as a separate blocker; do not claim cryptographic tamper proof.
- [Physical identity rejects legitimate moves or restores] -> Treat this as intentional for prospective evidence; prepare and register a new future collector after relocation.
- [DELETE journal reduces writer concurrency] -> The collector is intentionally single-writer and tiny; the simpler consistent-snapshot boundary is preferable.
- [Crash occurs between registration file creation and ledger binding] -> Permit only byte-identical completion of the missing bind; never overwrite either artifact.
- [Crash occurs after child commit but before terminal ledger append] -> Use independent state and step postconditions; never trust absent process status.
- [Price capture commits per symbol] -> Permit only verified monotonic partial recovery and record the interrupted attempt as failed before retry.
- [Logical hashing costs grow with data] -> The fixed 12-by-3 panel is bounded; stream ordered rows through the hash instead of loading the full database into memory.
- [Lease nonce or descriptor leaks through process plumbing] -> Pass only the lock duplicate and one-use pipe read descriptor through exact `pass_fds`; never place the nonce in argv, environment, ledger, stdout, stderr, or diagnostics; close every parent duplicate and pipe end immediately after process creation, require exact 32-byte read plus EOF, and reject reuse after a terminal event.
- [Provider bundle `/2` breaks old bundle fixtures] -> Fail closed on `/1` bundle input, keep the verified export envelope stable, and update frozen cross-repository fixtures together.

## Migration Plan

1. Introduce collector continuity schemas and verifiers without accepting legacy collectors.
2. Add `/4` preparation and registration; keep `/3` capture rejection explicit.
3. Move registered capture onto the lease, ledger, postcondition, and recovery state machine; guard direct collector writers.
4. Add consistent materialization and provider bundle input `/2`, then update RQGM fail-closed compatibility fixtures while preserving export envelope `/1`.
5. Run focused crash/tamper/concurrency suites, both repositories' full regressions, strict OpenSpec validation, and diff checks.
6. Only after production trust enrollment and signed static authority exist, prepare and register a new prospective `/4` panel. Never retrofit the expired `/3` panel.

Rollback consists of disabling `/4` collection and leaving its files untouched for audit. It must not reactivate `/3`, delete ledger events, or reinterpret partial `/4` data as execution-grade evidence.

## Open Questions

No design question blocks implementation. Concrete schema constants, event field names, and bounded diagnostic limits must be frozen in tests before Terra writes the production implementation; changing them later requires an explicit schema version rather than optional fields.
