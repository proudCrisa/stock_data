## ADDED Requirements

### Requirement: Export uses one hash-verified local receipt
The exporter SHALL open the selected evidence database read-only, require one explicit
receipt id, recompute the receipt response SHA-256, and reject missing, duplicated,
malformed, non-finite, or panel-incomplete source rows.

#### Scenario: Receipt response bytes drift
- **WHEN** the stored response no longer matches its bound SHA-256
- **THEN** export fails before any feature snapshot becomes visible

#### Scenario: Requested symbol is absent
- **WHEN** the selected receipt does not contain exactly one source row for every requested symbol
- **THEN** export fails closed and reports no successful artifact

### Requirement: Feature semantics and PIT availability are explicit
Each exported record SHALL bind symbol, effective date, aware availability time, revision
identity, and finite values for a versioned field mapping. Availability SHALL be no earlier
than the receipt observation time, and the exporter SHALL NOT infer earlier historical
availability.

#### Scenario: Caller backdates availability
- **WHEN** requested availability precedes the selected receipt observation time
- **THEN** export rejects the request instead of relabeling the observations as earlier PIT data

#### Scenario: Provider value is not finite
- **WHEN** a mapped source value is boolean, null, text, NaN, or infinite
- **THEN** the corresponding snapshot is rejected

### Requirement: Snapshot is content-addressed and self-verifying
The exporter SHALL emit canonical `manifest.json`, `features.jsonl`, and `source.jsonl`
files. The manifest SHALL bind the source database SHA-256, receipt and response identities,
field mapping, ordered panel, file hashes, record counts, and a snapshot id derived from
the complete manifest payload. Verification SHALL reject any byte or identity drift.

#### Scenario: Feature bytes change after export
- **WHEN** a feature value is modified without rebuilding the snapshot
- **THEN** verification rejects the records hash before returning the artifact

#### Scenario: Existing snapshot id has different content
- **WHEN** export encounters a directory with the requested content identity but different bytes
- **THEN** it rejects the collision and does not overwrite the existing artifact

### Requirement: Export cannot grant execution authority
Every version-1 snapshot SHALL declare `FORWARD_PIT_RESEARCH_ONLY`,
`execution_grade=false`, and `authoritative_for_execution=false`. These values SHALL be
fixed by the schema and SHALL NOT be caller-configurable.

#### Scenario: Consumer requests execution-grade output
- **WHEN** a caller attempts to treat exporter arguments or provider values as execution authorization
- **THEN** the artifact remains explicitly non-execution-grade and cannot satisfy full readiness

### Requirement: Export performs no collection or source mutation
The module SHALL have no network collection path and SHALL NOT insert, update, delete, or
migrate source database records. It SHALL write only beneath the caller-selected output
root.

#### Scenario: Export runs against a read-only evidence database
- **WHEN** a valid local receipt and output root are provided
- **THEN** export succeeds without changing the source database hash
