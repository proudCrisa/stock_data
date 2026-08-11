"""Signed historical-universe evidence consumed by RQGM execution snapshots."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SCHEMA_VERSION = "stockdata-historical-universe-attestation/1"
ALGORITHM = "ed25519"


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("historical universe JSON contains duplicate keys")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("historical universe attestation is unreadable") from exc


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _timestamp(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")


def _date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date")
    return value


def _rows(path: Path) -> list[dict[str, object]]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ValueError("historical universe records are unreadable") from exc
    if not lines:
        raise ValueError("historical universe has no records")
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("historical universe record is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("historical universe record must be an object")
        required = {"effective_date", "available_at", "symbol", "is_member"}
        if set(row) != required:
            raise ValueError("historical universe records must use the canonical schema")
        _date(row["effective_date"], "historical universe effective_date")
        _timestamp(row["available_at"], "historical universe available_at")
        if not isinstance(row["symbol"], str) or not row["symbol"]:
            raise ValueError("historical universe symbol is invalid")
        if not isinstance(row["is_member"], bool):
            raise ValueError("historical universe is_member must be bool")
        rows.append(row)
    canonical = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows]
    if canonical != sorted(canonical) or len(canonical) != len(set(canonical)):
        raise ValueError("historical universe rows must be canonical, sorted, and unique")
    return rows


def load_historical_universe_attestation(
    records_path: Path, manifest_path: Path
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Parse and structurally validate an authority-signed full universe export."""
    manifest = _read_json(manifest_path)
    rows = _rows(records_path)
    if not isinstance(manifest, dict):
        raise ValueError("historical universe attestation must be an object")
    payload = manifest.get("payload")
    signature = manifest.get("signature_base64")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("algorithm") != ALGORITHM
        or not isinstance(payload, dict)
        or not isinstance(signature, str)
    ):
        raise ValueError("historical universe attestation schema is invalid")
    required = {
        "issuer", "key_id", "published_snapshot_id", "selection_policy_id",
        "membership_mode", "synthetic", "current_only", "coverage_start",
        "coverage_end", "content_sha256", "full_record_count", "daily_record_counts",
    }
    if set(payload) != required:
        raise ValueError("historical universe attestation payload is incomplete")
    if (
        not all(isinstance(payload[name], str) and payload[name] for name in ("issuer", "key_id", "published_snapshot_id", "selection_policy_id"))
        or payload["membership_mode"] != "historical"
        or payload["synthetic"] is not False
        or payload["current_only"] is not False
        or payload["full_record_count"] != len(rows)
    ):
        raise ValueError("historical universe attestation provenance is invalid")
    start = _date(payload["coverage_start"], "attestation coverage_start")
    end = _date(payload["coverage_end"], "attestation coverage_end")
    if start > end or any(not start <= str(row["effective_date"]) <= end for row in rows):
        raise ValueError("historical universe attestation coverage is invalid")
    content_sha256 = hashlib.sha256(records_path.read_bytes()).hexdigest()
    if payload["content_sha256"] != content_sha256:
        raise ValueError("historical universe attestation content hash mismatch")
    counts = payload["daily_record_counts"]
    actual_counts: dict[str, int] = {}
    for row in rows:
        day = str(row["effective_date"])
        actual_counts[day] = actual_counts.get(day, 0) + 1
    if counts != actual_counts:
        raise ValueError("historical universe attestation daily coverage is invalid")
    try:
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError
    except ValueError as exc:
        raise ValueError("historical universe signature is invalid") from exc
    return rows, manifest


def verify_historical_universe_attestation(
    manifest: Mapping[str, object], *, trusted_public_keys: Mapping[tuple[str, str], str]
) -> None:
    """Verify the signature against a caller-controlled-but-explicit trust registry."""
    payload = manifest.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("historical universe attestation payload is missing")
    key = (str(payload.get("issuer", "")), str(payload.get("key_id", "")))
    encoded_key = trusted_public_keys.get(key)
    if encoded_key is None:
        raise ValueError("historical universe issuer key is not enrolled as trusted")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded_key, validate=True))
        public_key.verify(base64.b64decode(str(manifest["signature_base64"]), validate=True), _canonical(dict(payload)))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("historical universe signature verification failed") from exc
