"""Research-only historical corporate-action observations.

Baostock's dividend endpoint is useful for historical research, but its
response is not a point-in-time, revision-complete corporate-action ledger.
This module preserves that limitation in the artifact itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import shutil
import tempfile
from typing import Any


SCHEMA_VERSION = "stockdata-research-corporate-actions/1"


class ResearchCorporateActionError(ValueError):
    """Raised when a historical corporate-action artifact is invalid."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ResearchCorporateActionError(
            "action value is not canonical JSON"
        ) from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchCorporateActionError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ResearchCorporateActionError(f"{field} must be an ISO date") from exc


def _iso_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchCorporateActionError(f"{field} must be timezone-aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCorporateActionError(f"{field} must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchCorporateActionError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(
    actions: Mapping[str, Iterable[Mapping[str, object]]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for symbol in sorted(actions):
        if not isinstance(symbol, str) or not symbol:
            raise ResearchCorporateActionError("action symbol must be non-empty")
        seen: set[bytes] = set()
        for index, action in enumerate(actions[symbol]):
            if not isinstance(action, Mapping) or not action:
                raise ResearchCorporateActionError(
                    f"{symbol} action {index} must be a non-empty mapping"
                )
            payload = {"symbol": symbol, "data": dict(action)}
            identity = _canonical(payload)
            if identity in seen:
                raise ResearchCorporateActionError(f"duplicate action for {symbol}")
            seen.add(identity)
            normalized.append(payload)
    if not normalized:
        raise ResearchCorporateActionError("corporate-action artifact is empty")
    normalized.sort(key=_canonical)
    return normalized


def build_corporate_action_artifact(
    actions: Mapping[str, Iterable[Mapping[str, object]]],
    *,
    observation_date: str,
    retrieved_at: str,
    source_receipt: Mapping[str, object],
    output_root: str | Path,
) -> Path:
    """Write a content-addressed historical observation artifact atomically."""

    observed = _iso_date(observation_date, "observation_date")
    retrieved = _iso_timestamp(retrieved_at, "retrieved_at")
    if set(source_receipt) < {"provider", "query"}:
        raise ResearchCorporateActionError(
            "source_receipt must identify provider and query"
        )
    rows = _rows(actions)
    raw = b"".join(_canonical(row) + b"\n" for row in rows)
    receipt = {**dict(source_receipt), "retrieved_at": retrieved}
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "observation_date": observed,
        "rows_sha256": _sha256(raw),
        "row_count": len(rows),
        "source_receipt": receipt,
    }
    identity = _sha256(_canonical(identity_payload))
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{observed}-{identity}"
    if target.exists():
        verify_corporate_action_artifact(target)
        return target

    temporary = Path(tempfile.mkdtemp(prefix=".corporate-actions-", dir=root))
    try:
        (temporary / "actions.jsonl").write_bytes(raw)
        manifest = {
            **identity_payload,
            "artifact_id": identity,
            "execution_grade": False,
            "authority_status": "research_vendor_only",
            "point_in_time_verified": False,
            "revision_complete": False,
        }
        (temporary / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def verify_corporate_action_artifact(root: str | Path) -> dict[str, Any]:
    """Verify an artifact and return its manifest without modifying it."""

    path = Path(root).expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise ResearchCorporateActionError("action artifact root must be a directory")
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="ascii"))
        raw = (path / "actions.jsonl").read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCorporateActionError("action artifact is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ResearchCorporateActionError("unsupported action artifact schema")
    if manifest.get("execution_grade") is not False:
        raise ResearchCorporateActionError(
            "action artifact cannot claim execution grade"
        )
    if manifest.get("authority_status") != "research_vendor_only":
        raise ResearchCorporateActionError("action authority status is invalid")
    if manifest.get("point_in_time_verified") is not False:
        raise ResearchCorporateActionError("action PIT status is invalid")
    if manifest.get("revision_complete") is not False:
        raise ResearchCorporateActionError("action revision status is invalid")
    lines = raw.decode("ascii").splitlines()
    rows = [json.loads(line) for line in lines]
    if rows != sorted(rows, key=_canonical):
        raise ResearchCorporateActionError("action rows are not canonical")
    canonical_raw = b"".join(_canonical(row) + b"\n" for row in rows)
    if canonical_raw != raw:
        raise ResearchCorporateActionError("action rows are not canonical")
    if manifest.get("rows_sha256") != _sha256(raw):
        raise ResearchCorporateActionError("action rows hash mismatch")
    if manifest.get("row_count") != len(rows):
        raise ResearchCorporateActionError("action row count mismatch")
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "observation_date": _iso_date(
            manifest.get("observation_date"), "observation_date"
        ),
        "rows_sha256": _sha256(raw),
        "row_count": len(rows),
        "source_receipt": manifest.get("source_receipt"),
    }
    if manifest.get("artifact_id") != _sha256(_canonical(identity_payload)):
        raise ResearchCorporateActionError("action artifact identity mismatch")
    return manifest


__all__ = [
    "SCHEMA_VERSION",
    "ResearchCorporateActionError",
    "build_corporate_action_artifact",
    "verify_corporate_action_artifact",
]
