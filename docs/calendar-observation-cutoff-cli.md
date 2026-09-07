# Calendar Observation Cutoff CLI

`python -m stockdata.provider_authority_publisher publish-envelope` accepts
`--current-decision-observation-cutoff TIMESTAMP` for `trading_calendar`.
The CLI passes the supplied string unchanged to the existing publisher and
admission function. It does not derive a timestamp or change signed payloads.

Omitting the option preserves the existing publication rules. A calendar that
requires the current-observation path can therefore still fail when the option
is omitted. Supplying the option for another component is rejected by the
existing calendar-only check. Invalid timestamps, late source observations,
wrong registry pins and invalid signatures retain their existing rejection rules.

The option is distinct from the repeatable per-panel `--decision-cutoff` mapping.
The handler retains its existing empty mapping when no per-panel values are
supplied; calendar admission already derives its panel cutoffs internally.

## Operation Shape

The following is a command template, not an executed publication or permission to
use a real key. It requires already-qualified local artifacts, source receipts,
an independently fixed registry digest, and separately authorized signing.
Each placeholder must be replaced with the intended bound input; there are no
default times or example real credentials.

```sh
python -m stockdata.provider_authority_publisher publish-envelope \
  --component trading_calendar \
  --registry /ABS/PATH/registry.json \
  --registry-sha256 INDEPENDENT_REGISTRY_SHA256 \
  --artifact /ABS/PATH/calendar.json \
  --source-receipt /ABS/PATH/source-receipt.json \
  --signer-private-key-env AUTHORIZED_SIGNER_ENV_NAME \
  --output /ABS/PATH/new-calendar-envelope.json \
  --effective-at EFFECTIVE_TIMESTAMP \
  --available-at AVAILABLE_TIMESTAMP \
  --current-decision-observation-cutoff CURRENT_DECISION_TIMESTAMP
```

Repeat `--source-receipt` for multiple bound receipts. The new option does not
grant source, registration, decision, execution or historical evidence authority.
This increment uses only temporary test keys and synthetic fixtures. It does not
activate signing or change a running service's selected source revision.
