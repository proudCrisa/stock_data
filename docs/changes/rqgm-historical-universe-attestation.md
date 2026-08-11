# RQGM Historical Universe Attestation

RQGM continuous replay accepts a stock universe only when the complete daily
membership artifact is bound to an Ed25519-signed attestation. The signed payload
contains issuer/key identity, selection policy, publication snapshot identity,
coverage, daily counts, and the exact JSONL SHA-256. A fixed-panel projection is
not an authority and is never used as the `universe` artifact.

`stock_data` validates structure, canonical bytes, and the supplied signature shape;
RQGM owns the pinned issuer/key registry and verifies the signature before replay.
The production registry is deliberately empty until a real publisher's public key and
historical artifact are reviewed and enrolled in source control. Existing post-hoc
SQLite bars therefore remain research-only and cannot be upgraded by this change.

This contract also supports future capture, but only a key pre-enrolled in RQGM can
make an artifact execution-grade. A local timestamp, file permissions, or a caller
provided key are not substitutes for that enrollment.
