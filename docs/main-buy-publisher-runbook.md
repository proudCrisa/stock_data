# Local publisher preparation (inactive)

This runbook prepares externally authorized inputs. It does not create a trust
root, sign enrollment with test keys, modify the bundled registry or activate a
production publisher. The native amount probe and synthetic tests are not a
complete operational BUY input set.

## Trust identity and enrollment

`stockdata.authority.PROVIDER_TRUST_REGISTRY_SHA256` currently pins the bundled
empty registry to `69b94b1d01cb8dd299db799fac657b78ce77a548d35753ab9dca1c9bf94aeec6`.
Changing only `stockdata/enrolled_trust_registry.json` fails that pin check.
There is no separate hard-coded administrator public key. The external registry
SHA-256 is the trust anchor; it authenticates its root public keys. Each root
must then authorize a publisher enrollment with an Ed25519 signature. Root and
publisher IDs are SHA-256 of their raw Ed25519 public-key bytes.

For an independent supplement, an externally fixed request may select a registry
through `load_enrolled_trust_registry(path, expected_sha256=pin)` or its bytes
equivalent. The supplement cannot choose its own trusted pin. Reusing the price
snapshot's registry requires the same registry bytes and roles. Changing that
anchor is a separate reviewed deployment action.

Required enrollment inputs:

- `root-public.json`: exactly `trust_root_id`, `public_key_base64`.
- `enrollment.json`: exactly `publisher_key_id`, `trust_root_id`,
  `public_key_base64`, sorted `component_roles`, `valid_from`, `valid_until`,
  `authorization_signature_base64`.
- A root-authorized enrollment signature over the canonical
  `stockdata-signer-enrollment/1` payload, including registry schema/version,
  `publisher_public_key_base64`, IDs, roles and validity bounds. The exact
  payload is defined by `stockdata.authority._enrollment_payload`.
- An existing publisher private key exposed only through the explicitly named
  environment variable; the publishing API never creates it.

The public CLI verifies a supplied root-signed enrollment; it does not issue one:

```bash
python -m stockdata.provider_authority_publisher build-registry \
  --root-public-key /absolute/staging/root-public.json \
  --enrollment /absolute/staging/enrollment.json \
  --registry-version 1 \
  --output /absolute/staging/registry.json
```

The administrator must supply the real enrollment. The existing all-zero role
registry means no producer is enrolled; fixture keys cannot fill this gap.

## Exact input files

An operational supplement needs all of these independently evidenced inputs:

| Input | Source and required state |
| --- | --- |
| Original v1 price provider manifest and bundle | Existing price authority; externally fixed manifest hash |
| Registry | Canonical registry JSON, externally fixed SHA-256 and root-signed enrollments |
| Native amount product | At least 20 complete raw BaoStock ETF session receipts through price session T |
| Global snapshot | Trading `build_snapshot` output plus observed-at facts and signed publisher receipt |
| Calendar | Signed session facts for every amount session, with next-session links and closing times |
| Instrument status | Exact ETF and T; current listing/suspension state |
| Universe | Exact ETF and T; signed membership identity |
| Corporate actions | Exact ETF and T; signed events or explicit source-supported no-event result |
| Market rules | Exact ETF, dated official classification/rule sources and the existing fee/slippage policy |
| Component envelopes | One valid enrolled-publisher envelope for each component |

Each component has `artifact.json`, one or more canonical `source-receipt.json`
files, and `envelope.json`. All source-receipt IDs must equal their canonical
SHA-256 and must bind the artifact's exact records. Real dated source assertions
cannot be replaced by the test fixtures in this checkout.

## Publishing API

Native amount product construction and conversion:

```python
from stockdata.liquidity_amount_product import (
    build_liquidity_amounts_product, build_liquidity_authority_inputs,
)
product = build_liquidity_amounts_product(
    raw_captures, panel=exact_symbol_session_pairs,
    decision_cutoff=cutoff, expected_watermark=asof,
)
liquidity = build_liquidity_authority_inputs(product)
```

Global conversion is
`build_global_signals_authority_inputs(snapshot, panel=[symbol + '@' + asof],
available_at=observed_at)` in `stockdata.main_buy_supplement`. It preserves the
Trading snapshot and does not compute strategy decisions.

The same publisher API signs either component after its canonical artifact and
binding receipts have been materialized into staging files:

```python
from stockdata.provider_authority_publisher import publish_authority_envelope
published = publish_authority_envelope(
    component="liquidity_amounts",
    registry_file=registry_path, registry_sha256=externally_fixed_pin,
    artifact_file=artifact_path, source_receipt_files=receipt_paths,
    signer_private_key_env="STOCKDATA_PUBLISHER_KEY_B64", output_file=envelope_path,
    effective_at=effective_at, available_at=available_at,
    decision_cutoff_by_panel={entry: cutoff for entry in liquidity["artifact"]["panel"]},
)
liquidity["authority_envelope"] = dict(published.envelope)
```

For exact market rules, first publish status; pass its `published.admitted` object
as `instrument_status_authority` to the market-rule publisher. Without that
argument the existing generic rulebook prerequisite flow remains in force.

The CLI equivalent is `publish-envelope --component ... --registry ...
--registry-sha256 ... --artifact ... --source-receipt ...
--signer-private-key-env ... --output ... --effective-at ... --available-at ...
--decision-cutoff 'symbol@YYYY-MM-DD=timestamp'`. Repeat `--source-receipt` and
`--decision-cutoff` for every required receipt/panel entry. The exact status-bound
market-rule branch is currently exposed through the Python API.

## Composition and offline verification

The final composition is ordinary JSON assembly of already-published inputs:

```python
from stockdata.main_buy_supplement import verify_main_buy_supplement
supplement = {
    "schema_version": "stockdata-main-buy-supplement/1",
    "provider_manifest_sha256": manifest_sha256,
    "asof": asof, "decision_cutoff": cutoff, "symbols": symbols,
    "registry": registry_json,
    "liquidity": liquidity, "global_signals": global_inputs,
    "references": signed_reference_inputs,
}
verify_main_buy_supplement(
    supplement, expected_registry_sha256=externally_fixed_pin,
    provider_manifest_sha256=manifest_sha256, asof=asof, decision_cutoff=cutoff,
)
```

The production artifact builder is the API above; the test helper is only a
synthetic fixture. A convenience CLI for file-descriptor composition is not yet
provided and is not a requirement for using these callable APIs. All raw signed
inputs must be retained inside the supplement so Trading can freeze and replay
the same decision bundle without external data reads.
