## MODIFIED Requirements

### Requirement: Missing historical authority is never fabricated

The provider SHALL keep current context, inferred membership, activity proxies,
undocumented rule constants, post-hoc captures, and vendor bootstrap data as explicit
non-authoritative evidence when historical publisher authority is required. Provider
credentials SHALL be accepted only through non-persistent runtime input and SHALL NOT
appear in logs, errors, receipts, artifacts, command arguments, or repository files.

#### Scenario: JQData history is collected successfully
- **WHEN** a bounded exact-date JQData bootstrap returns historical membership or status
- **THEN** the rows are labeled vendor bootstrap evidence and signed authority readiness remains unchanged

#### Scenario: Provider returns a neighboring date
- **WHEN** a JQData response date differs from the requested historical date
- **THEN** collection fails closed rather than accepting a nearest-date result

#### Scenario: Trial credentials are entered
- **WHEN** the CLI authenticates to JQData
- **THEN** the account and secret exist only in process memory and are absent from persisted or emitted data

#### Scenario: Free quota is insufficient
- **WHEN** the remaining trial quota cannot cover the bounded request
- **THEN** no history call is made and collection fails with a redacted quota error
