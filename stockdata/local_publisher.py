"""Explicit local publisher initialization and bounded native amount capture."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from .authority import AUTHORITY_COMPONENT_ROLES, _enrollment_payload
from .provider_authority_publisher import (
    _base64, _canonical, _key_id, _public_key, build_canonical_registry,
)


def _write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def initialize_local_publisher(directory: str | Path) -> dict:
    """Create a new real identity; never replace existing keys or enroll globally."""
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    now = datetime.now(timezone.utc)
    root, publisher = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    root_public, publisher_public = _public_key(root), _public_key(publisher)
    root_entry = {"trust_root_id": _key_id(root_public),
                  "public_key_base64": _base64(root_public)}
    enrollment = {
        "publisher_key_id": _key_id(publisher_public),
        "trust_root_id": root_entry["trust_root_id"],
        "public_key_base64": _base64(publisher_public),
        "component_roles": sorted(AUTHORITY_COMPONENT_ROLES),
        "valid_from": now.isoformat(timespec="seconds"),
        "valid_until": (now + timedelta(days=365)).isoformat(timespec="seconds"),
    }
    enrollment["authorization_signature_base64"] = _base64(root.sign(
        _canonical(_enrollment_payload(enrollment, registry_version=1)),
    ))
    for name, key in (("root", root), ("publisher", publisher)):
        _write_private(destination / f"{name}.key", key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption(),
        ))
    _write_private(destination / "root-public.json", _canonical(root_entry))
    _write_private(destination / "enrollment.json", _canonical(enrollment))
    registry = build_canonical_registry(
        root_public_key_file=destination / "root-public.json",
        enrollment_file=destination / "enrollment.json",
        output_file=destination / "registry.json",
    )
    anchor = {
        "schema_version": "stockdata-local-publisher-anchor/1",
        "registry_file": str(destination / "registry.json"),
        "registry_sha256": registry.registry_sha256,
        "trust_root_id": root_entry["trust_root_id"],
        "publisher_key_id": enrollment["publisher_key_id"],
        "valid_from": enrollment["valid_from"],
        "valid_until": enrollment["valid_until"],
    }
    _write_private(destination / "trust-anchor.json", _canonical(anchor))
    return anchor


def capture_liquidity(*, symbol: str, start: str, asof: str, output_dir: str | Path) -> dict:
    """Capture one exact ETF without touching shared caches or backdating receipts."""
    from .fetch_baostock import fetch_baostock
    from .liquidity_amount_product import ETF_SOURCES, build_liquidity_amounts_product
    from .ticker import normalize

    symbol = normalize(symbol)
    if symbol not in ETF_SOURCES:
        raise ValueError("ETF identity lacks an approved issuer source")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    captured = fetch_baostock(symbol, start, asof, adjustment_mode="raw")
    receipt = captured.capture_receipt
    _write_private(destination / "raw-capture.json", _canonical(receipt))
    cutoff = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    days = sorted({row["date"] for row in captured if row["date"] <= asof})[-20:]
    product = build_liquidity_amounts_product(
        [receipt], panel=[(symbol, day) for day in days],
        decision_cutoff=cutoff, expected_watermark=asof,
    )
    _write_private(destination / "liquidity-product.json", _canonical(product))
    return {"symbol": symbol, "asof": asof, "observed_at": receipt["observed_at"],
            "sessions": len(days), "product_sha256": product["product_sha256"],
            "product_file": str(destination / "liquidity-product.json")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--directory", required=True)
    capture = commands.add_parser("capture-liquidity")
    capture.add_argument("--symbol", required=True)
    capture.add_argument("--start", required=True)
    capture.add_argument("--asof", required=True)
    capture.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        result = initialize_local_publisher(args.directory)
    else:
        result = capture_liquidity(symbol=args.symbol, start=args.start,
                                   asof=args.asof, output_dir=args.output_dir)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
