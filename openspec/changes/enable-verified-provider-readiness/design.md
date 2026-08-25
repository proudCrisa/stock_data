## Context

The existing provider contract correctly fixes nine component identities and lets RQGM
reverify every exported byte. Its materializer, however, treats every component without a
signed envelope as unattested, while only five component types are eligible for signed
admission. `_blocked_report()` and the final materializer assertion also force aggregate
readiness to false. As a result, no input can produce a positive receipt by construction.

That fail-closed state was appropriate while provider admission was absent. The repository
now has signed-authority publication/admission, immutable price receipts, forward context,
corporate-action capture, availability verification, and a larger real price cache. The
next step is a positive path that proves facts instead of trusting a supplied readiness
claim. RQGM remains a read-only consumer and task 8.6 remains blocked until a real exact
panel passes the provider path.

## Goals / Non-Goals

**Goals:**

- Produce `ready=true` only from a complete, exact-panel, byte-verifiable nine-component
  bundle.
- Reconstruct locally owned components from immutable database rows and receipts.
- Require enrolled external signatures for independently published components.
- Bind availability to the actual component records and their signed, phase-specific
  calendar cutoffs.
- Represent every dated execution rule consumed by canonical A-share replay.
- Preserve deterministic export/reverification and explicit per-component blockers.

**Non-Goals:**

- Enroll a production trust root or commit private signing keys.
- Upgrade post-hoc Tonghuashun cache rows to PIT execution evidence.
- Repair the expired 2026-08-12 forward panel or backdate missing receipts.
- Relax RQGM's nine-component consumer gate or authorize release/economic claims.
- Treat a synthetic positive fixture as evidence that current local data is ready.

## Decisions

### Separate intrinsic reconstruction from external authority admission

Execution prices, signal prices, and decision context are intrinsic provider components.
The materializer SHALL reconstruct their canonical component payloads from the immutable
database, exact panel, adjustment identities, and bound source receipts, then byte-compare
the result with the component artifact. Caller JSON is only a claimed artifact locator.

Trading calendar, universe, instrument status, corporate actions, and market rules remain
externally published components. Their exact payloads SHALL pass enrolled signature,
source-receipt, effective-time, availability-time, and panel checks before they contribute
positive evidence.

Alternative: make all nine components signed. Rejected because it would let a signature
replace verification of database/receipt consistency and would introduce an unnecessary
self-attestation loop for locally derived facts.

### Make availability derived but independently verified

Availability records SHALL enumerate the actual records used by the other eight
components. Each row binds component, panel entry, canonical record hash, source receipt,
event/availability time, and the applicable signed calendar cutoff. Decision inputs use
the same-session decision cutoff. Finalized execution and signal price bars MUST become
available no earlier than the signed session close and no later than the signed next-session
decision cutoff. The verifier SHALL
recompute record hashes from component bytes and reject missing, duplicate, orphaned,
post-cutoff, or merely well-formed hashes.

Alternative: accept an availability artifact containing syntactically valid SHA-256
values. Rejected because arbitrary hashes do not establish that the evaluated records were
available for the decision.

### Compute aggregate readiness from verified evidence only

The positive aggregator SHALL build a fresh readiness report. For each component it records
the verifier identity, artifact identity, coverage count, and blockers. Aggregate
`ready=true` is exactly the conjunction of the fixed nine component `ready=true` values,
an empty aggregate blocker list, exact request/companion identities, and successful
byte-for-byte export reverification.

The materializer SHALL no longer assert that every result is false. It SHALL instead assert
that its export result equals the independently recomputed report. Missing admission or
reconstruction evidence continues to produce a valid blocked bundle.

Alternative: mutate the legacy full-readiness result in place. Rejected because its current
unavailable placeholders and activity proxies are useful diagnostics but not positive
authority.

### Freeze the complete dated execution-rule surface

The market-rule artifact SHALL cover every rule consumed by canonical replay: exchange and
board scope, effective interval, lot size, T+1, listing-age restrictions, suspension,
price-limit calculation and exceptional states, commission, minimum fee, transfer fee,
stamp duty, slippage policy, order validity/lifecycle, and policy/source identities. The
complete canonical payload bytes are content-addressed in the companion snapshot.

A payload missing a consumed field is blocked, even if it has a valid signature. A schema
version change is required if the existing market-rule payload cannot represent this
surface; RQGM compatibility is handled before a real READY receipt is consumed.

Alternative: bind a self-reported rulebook ID. Rejected because the ID cannot reproduce the
rules or detect a change in one fee or limit regime.

### Keep test trust separate from production trust

Tests MAY construct ephemeral roots and signers in temporary files to prove the positive
path. Production code SHALL load only the enrolled registry and SHALL never generate,
persist, or auto-enroll a key. A synthetic fixture proves implementation reachability, not
provider authority for task 8.6.

### Gate each new forward panel before capture

An explicit preparation command SHALL create a new empty collector database, install the
fixed context/action schemas, and bind the immutable price cohort before any pre-open
capture. The registration command SHALL then accept only a sorted future 12-symbol by
3-session exact product. It SHALL reload the pinned production trust registry, require independently
root-authorized signer coverage for all five external roles through the final signed
next-session cutoff, and directly admit the complete calendar and market-rule artifacts.
It SHALL also verify a clean, writable, structurally complete collector database through a
read-only connection before writing one canonical registration file without overwrite.

The registration binds the database path, panel identity, trust registry, admitted static
artifact/signature identities, their immutable input locators, role coverage, and
collector-schema fingerprint. Capture accepts only this version and only on the registered
session date; before every phase it reloads the production trust, re-admits the static
artifacts, checks current role coverage, and recomputes the database schema/cohort
fingerprint. Cleanliness includes price/context/action rows, receipts, and sync coverage.
These controls prevent
reuse of the expired panel but do not make the registration a new evidence authority:
materialization still independently verifies every nine-component byte and receipt.

## Risks / Trade-offs

- [Positive fixtures accidentally look like live evidence] -> Label them synthetic, keep
  them under temporary test roots, and assert their identities cannot load through the
  production registry.
- [Current schemas cannot bind full rules or record closure] -> Version the affected
  component payloads and keep old bundles blocked; do not add optional authority fields.
- [Post-hoc cache rows dominate coverage] -> Report them separately as E1 research data and
  require original timely receipts for execution-grade components.
- [Provider and RQGM schemas drift] -> Add cross-repository frozen fixtures and preserve
  fail-closed consumer behavior until the consumer recognizes the provider version.
- [Exact-panel collection takes future time] -> Finish code and static authority first,
  then register a new future panel; never repair the expired panel.

## Migration Plan

1. Freeze positive and negative acceptance fixtures before changing aggregation.
2. Implement intrinsic component reconstruction and strict signed-component admission.
3. Complete availability-record and market-rule schema verification.
4. Replace the forced-false report with verified conjunctive aggregation and re-export.
5. Run the full `stock_data` suite, OpenSpec strict validation, and RQGM consumer fixtures.
6. Audit current local artifacts; retain `ready=false` until external enrollment and a new
   timely future panel exist.
7. After capability/static authority readiness, register a new 12-symbol by 3-day panel,
   collect pre-open/post-close observations, and materialize the first real bundle.

Rollback disables the positive aggregator and returns to forced-blocked materialization.
Previously blocked bundles are immutable and require no rewrite.

## Open Questions

- Which independent custodian keys will be enrolled for the five signed components?
- Which official, freely redistributable rulebook bytes will be frozen for each dated A-share
  rule regime?
- Does the first real panel use Tencent raw prices with a separate Tonghuashun signal
  identity, or keep both price roles on one timely captured provider identity?
