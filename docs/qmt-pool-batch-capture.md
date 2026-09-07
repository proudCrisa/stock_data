# Explicit RQGM Pool Batch Capture

This opt-in producer is paired with RQGM forward protocol `/7`. The existing
`capture_qmt_pool_replay.py` and `qmt_pool_replay.py` contracts are unchanged.
Historical sealed protocols must use their original implementation bytes.

The frozen command is one argv:

```text
ABS_PYTHON ABS_STOCK_DATA/scripts/capture_qmt_pool_replay_batch.py --base-url EXPLICIT_LOOPBACK_HTTP_URL --output-root ABS_EXISTING_EMPTY_STAGING SYMBOL_001 ... SYMBOL_154
```

Symbols must be unique and sorted. They are partitioned as seven groups of 20
and a final group of 14. `QMT_POOL_REPLAY_TOKEN` is resolved only when the command
is actually invoked. A real invocation requires separate authorization of its
exact benign service and scope; this implementation work authorizes no capture.

`capture_qmt_pool_replay_batch(client, symbols, output_root)` uses the existing
GET-only pool client to read `/` once and `/latest` once. The normalizer runs
once with its actual UTC observation time after both response bodies arrive.
All seals retain the complete common receipt and original source request with
309 symbols, count 1300, period `1d` and six existing fields. RQGM independently
checks exact source membership and the frozen cohort's full session panel.

All eight artifacts are built and verified in memory before writing. The
existing immutable, content-addressed per-file writer publishes each file.
Success has schema `qmt-pool-batch-capture-result/1`, status `COMPLETE`, eight
output paths and the common receipt digest, only after the exact directory
inventory and all bytes are rechecked. Every authority flag remains false.

This is not batch-atomic publication. An exception or crash may leave partial
files. CLI failures return code 2 and `INCOMPLETE`; a crash may produce no JSON.
Neither state is complete. A nonempty output directory is rejected before
acquisition, with no overwrite, automatic retry, timestamp copying or resume.
RQGM's unchanged exact-eight-files/full-receipt validator rejects partial,
mixed or extra products. A failed attempt requires explicit preparation of a
new compatible execution, never a silent change to a frozen protocol.

The offline tests use synthetic wire data and mocked client transport. They
provide no market observations, ordinary READY qualification, forward evidence,
research efficacy or decision authority.
