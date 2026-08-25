## Context

The append-only forward evidence database stores provider response receipts and derived
context rows. Receipt `13` is one concrete example: a Sina market-center response was
observed at a fixed aware timestamp and its canonical parsed rows contain PB, PE, market
capitalization, free-float capitalization, and turnover ratio for a broad A-share panel.
RQGM must not query this provider-specific schema directly, and the existing full-execution
readiness gate must remain authoritative for execution-grade claims.

## Goals / Non-Goals

**Goals:**

- Produce one immutable, provider-neutral snapshot from an explicitly selected local
  receipt and symbol panel.
- Prove every exported numeric value is present in the hash-verified receipt response.
- Preserve event/effective date, aware availability time, receipt identity, source row,
  field semantics, and deterministic revision identity.
- Make byte-for-byte verification possible without reopening the source database.
- Label the artifact as PIT research-only and non-execution-grade.

**Non-Goals:**

- Fetch or refresh provider data.
- Admit a provider, sign evidence, complete the nine-component readiness closure, or
  authorize E2+ evidence.
- Activate an RQGM capability or decide whether an exported feature is economically useful.
- Provide a generic expression engine or silently infer field units.

## Decisions

### 1. Export from one explicit receipt

The caller supplies a local database path, receipt id, effective date, requested symbols,
and output root. The exporter opens SQLite with `mode=ro&immutable=1`, verifies the stored
response hash, parses the receipt's canonical `rows`, and rejects missing or duplicate
symbols. It never selects "latest" implicitly. This makes availability and revision
semantics reproducible.

Alternative: query the derived forward tables. Rejected because those tables do not retain
all requested factor values and could obscure the exact provider response used.

### 2. Freeze a narrow, documented field projection

Version 1 exports `price_to_book`, `price_to_earnings`, `market_cap_10k_cny`,
`float_market_cap_10k_cny`, and `turnover_ratio_percent` from the provider row keys `pb`,
`per`, `mktcap`, `nmc`, and `turnoverratio`. Every value must be a finite JSON number.
Field names and source keys are stored in the manifest.

Alternative: accept arbitrary field expressions. Rejected because a trusted generic
expression evaluator would add unnecessary execution and semantic risk to this narrow
bridge.

### 3. Make the artifact self-verifying

The output directory contains `manifest.json`, `features.jsonl`, and `source.jsonl`.
`source.jsonl` contains the exact canonical source rows used plus receipt metadata;
`features.jsonl` contains provider-neutral feature records. The manifest binds both byte
hashes, record counts, field mapping, ordered symbols, source database hash, receipt
response hash, observation/availability time, and evidence boundary. Its snapshot id is a
SHA-256 content identity over the manifest payload.

Alternative: store only the provider-neutral records. Rejected because a consumer could
verify internal consistency but not the physical source values.

### 4. Keep authority explicitly limited

The manifest always records `evidence_grade=FORWARD_PIT_RESEARCH_ONLY`,
`execution_grade=false`, and `authoritative_for_execution=false`. The exporter rejects an
availability time earlier than the receipt observation time. Consumers may use values only
for decision cutoffs at or after that time.

## Risks / Trade-offs

- [One receipt provides a stale cross-sectional snapshot] -> Preserve the exact availability
  time and require downstream `LAST_AVAILABLE` semantics; make no freshness claim.
- [Provider field units could be misunderstood] -> Freeze explicit output names containing
  units and bind the source-key mapping in the manifest.
- [The source database can later drift] -> Bind its byte hash and copy the exact used rows
  into the immutable artifact; verification does not trust the later database state.
- [A valid feature snapshot is mistaken for execution readiness] -> Hard-code the
  research-only/non-execution-grade boundary and test that callers cannot override it.
- [Concurrent writers alter the database during export] -> Open the source in SQLite
  immutable read-only mode and bind the database bytes observed by the export.

## Migration Plan

1. Add the standalone exporter and verifier with fixture tests.
2. Export one fresh artifact from the already captured receipt without network access.
3. Verify the artifact independently from its own bytes.
4. Let RQGM consume the artifact under its separate activation and replay contracts.
5. Roll back by removing the new module and generated artifact; no source database or
   existing readiness state is modified.

## Open Questions

None for version 1. Additional provider schemas or fields require a new schema version and
their own explicit semantics.
