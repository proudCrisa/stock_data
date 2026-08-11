## ADDED Requirements

### Requirement: stock_data owns the provider authority
The `stock_data` repository SHALL be the sole provider-side owner of collection receipts,
database snapshots, component readiness, and companion snapshots for the RQGM PIT contract.
RQGM MUST consume these artifacts read-only and MUST NOT mint or repair provider authority.

#### Scenario: Consumer supplies reconstructed provider metadata
- **WHEN** RQGM supplies metadata not issued by the bound provider authority
- **THEN** the provider contract rejects it and readiness remains false

### Requirement: Provider contracts are versioned and content-addressed
Each provider contract SHALL bind explicit schema versions and SHA-256 content identities
for the provider checkout tree, immutable database, source receipts, adjustment policy,
exact panel, readiness report, and companion snapshot. Filesystem paths, mutable tags, and
timestamps MUST NOT serve as artifact identities.

#### Scenario: Database path is unchanged but bytes change
- **WHEN** the database content no longer matches the bound identity
- **THEN** contract verification fails before any readiness result is consumed

### Requirement: Full readiness is conjunctive over nine components
Full execution readiness SHALL require execution prices, signal prices, decision context,
signed trading calendar, historical universe, historical instrument status, corporate
actions, market rules, and availability records. Aggregate readiness SHALL be true only
when every component is independently ready and no component or aggregate blocker exists.

#### Scenario: Prices are complete but calendar is untrusted
- **WHEN** price readiness is true and the trading-calendar signer is absent or unenrolled
- **THEN** full readiness is false with a trading-calendar blocker

#### Scenario: Component set omits market rules
- **WHEN** a readiness report omits any required component
- **THEN** the report is contract-invalid rather than partially ready

### Requirement: Signed authorities bind trust and source receipts
Calendar, universe, status, corporate-action, and market-rule artifacts SHALL bind their
publisher key, enrolled trust-root identity, signature, effective time, availability time,
and immutable source receipts. Unknown, self-generated, modified, or post-cutoff evidence
MUST NOT satisfy readiness.

#### Scenario: Signed universe content is modified
- **WHEN** an attested historical-universe row changes after signing
- **THEN** signature or content verification fails and universe readiness is false

#### Scenario: Source receipt is missing
- **WHEN** a component row cannot be traced to its bound immutable source response
- **THEN** that component remains blocked for the affected panel entry

### Requirement: Exact panel and adjustment identities are immutable
The provider SHALL canonicalize the requested symbol-date panel, bind its size and SHA-256,
and bind separate execution-price and signal-price adjustment identities. Subsets,
supersets, mixed adjustment variants, or reordered aliases MUST NOT change or silently
satisfy the request.

#### Scenario: Readiness covers only a subset
- **WHEN** one symbol-date identity in the requested panel lacks evidence
- **THEN** readiness is false and the missing identity is reported

### Requirement: Companion snapshots bind all decision and execution authorities
The companion snapshot SHALL content-address every required component, exact panel,
checkout, database, source-receipt set, adjustment identity, coverage boundary, and
availability record. Verification SHALL fail when any bound artifact drifts.

#### Scenario: Market-rule artifact changes after snapshot creation
- **WHEN** the current market-rule bytes differ from the companion snapshot descriptor
- **THEN** snapshot verification fails before downstream evaluation

### Requirement: Missing historical authority is never fabricated
The provider SHALL keep current context, inferred membership, activity proxies,
undocumented rule constants, and post-hoc captures as explicit blockers when historical
authority is required.
The provider MUST NOT promote a lower-grade substitute by relabeling it.

#### Scenario: Current activity is used as historical listing status
- **WHEN** no point-in-time status authority exists for the requested date
- **THEN** instrument-status readiness remains false
