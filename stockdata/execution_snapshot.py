"""Immutable execution-history artifact export for downstream replay."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "stockdata-execution-snapshot/1"
ARTIFACT_SCHEMAS = {
    "execution_prices": frozenset(
        {"effective_date", "available_at", "symbol", "open", "high", "low", "close", "volume"}
    ),
    "signal_prices": frozenset(
        {"effective_date", "available_at", "symbol", "open", "high", "low", "close", "volume"}
    ),
    "corporate_actions": frozenset(
        {"effective_date", "available_at", "symbol", "action_type", "payload"}
    ),
    "instrument_status": frozenset(
        {
            "effective_date",
            "available_at",
            "symbol",
            "listing_status",
            "board",
            "is_st",
            "is_suspended",
        }
    ),
    "universe": frozenset(
        {"effective_date", "available_at", "symbol", "is_member"}
    ),
    "market_rules": frozenset(
        {"effective_date", "available_at", "rule_id", "rule_type", "parameters"}
    ),
}


def _canonical_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return value


def _available_by_execution_cutoff(
    value: object, effective_date: str, kind: str, name: str
) -> None:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    cutoff_time = time(15, 5) if kind in {"execution_prices", "signal_prices"} else time(9, 25)
    cutoff = datetime.combine(
        date.fromisoformat(effective_date), cutoff_time, ZoneInfo("Asia/Shanghai")
    )
    if local > cutoff:
        raise ValueError(f"{name} is not point-in-time available by its execution cutoff")


def _canonical_artifact(
    kind: str,
    rows: Sequence[Mapping[str, object]],
    coverage_start: str,
    coverage_end: str,
) -> tuple[bytes, tuple[str, ...]]:
    required = ARTIFACT_SCHEMAS[kind]
    if not rows:
        raise ValueError(f"{kind} must contain authoritative records")
    normalized: list[dict[str, object]] = []
    schema: tuple[str, ...] | None = None
    for index, source in enumerate(rows):
        row = dict(source)
        missing = required - set(row)
        if missing:
            raise ValueError(f"{kind}[{index}] missing fields: {', '.join(sorted(missing))}")
        current_schema = tuple(sorted(row))
        if schema is None:
            schema = current_schema
        elif current_schema != schema:
            raise ValueError(f"{kind} rows must use one stable schema")
        effective_date = _canonical_date(row["effective_date"], f"{kind}.effective_date")
        available_at = _timestamp(row["available_at"], f"{kind}.available_at")
        if not coverage_start <= effective_date <= coverage_end:
            raise ValueError(f"{kind} record falls outside declared coverage")
        _available_by_execution_cutoff(
            available_at, effective_date, kind, f"{kind}.available_at"
        )
        normalized.append(row)
    lines = sorted(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in normalized
    )
    if len(lines) != len(set(lines)):
        raise ValueError(f"{kind} contains duplicate records")
    return ("\n".join(lines) + "\n").encode("utf-8"), schema or ()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _snapshot_identity(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_execution_snapshot(
    output_root: str | Path,
    *,
    coverage_start: str,
    coverage_end: str,
    artifacts: Mapping[str, Sequence[Mapping[str, object]]],
    authorities: Mapping[str, str],
    execution_price_basis: str = "raw",
    signal_price_basis: str = "qfq",
    selection_policy_id: str,
    rulebook_id: str,
    universe_attestation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write six content-addressed artifacts without inventing missing history."""
    _canonical_date(coverage_start, "coverage_start")
    _canonical_date(coverage_end, "coverage_end")
    if coverage_start > coverage_end:
        raise ValueError("coverage_start must not exceed coverage_end")
    if execution_price_basis != "raw":
        raise ValueError("execution prices must use raw basis")
    if signal_price_basis not in {"raw", "qfq", "hfq"}:
        raise ValueError("unsupported signal price basis")
    if set(artifacts) != set(ARTIFACT_SCHEMAS):
        raise ValueError("all six execution artifacts are required")
    if set(authorities) != set(ARTIFACT_SCHEMAS):
        raise ValueError("every artifact requires an explicit authority")
    if any(not isinstance(value, str) or not value.strip() for value in authorities.values()):
        raise ValueError("artifact authorities must be non-empty")
    if not selection_policy_id or not rulebook_id:
        raise ValueError("selection policy and rulebook identities are required")

    prepared: dict[str, tuple[bytes, tuple[str, ...]]] = {
        kind: _canonical_artifact(kind, artifacts[kind], coverage_start, coverage_end)
        for kind in sorted(ARTIFACT_SCHEMAS)
    }
    descriptors = {
        kind: {
            "path": f"{kind}.jsonl",
            "authority": authorities[kind],
            "content_sha256": _digest(raw),
            "record_count": len(raw.splitlines()),
            "schema": list(schema),
        }
        for kind, (raw, schema) in prepared.items()
    }
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "execution_price_basis": execution_price_basis,
        "signal_price_basis": signal_price_basis,
        "membership_mode": "historical",
        "synthetic_membership": False,
        "current_only_membership": False,
        "selection_policy_id": selection_policy_id,
        "rulebook_id": rulebook_id,
        "artifacts": descriptors,
        "universe_attestation": (
            dict(universe_attestation) if isinstance(universe_attestation, Mapping) else None
        ),
    }
    snapshot_id = f"{coverage_end}-{_snapshot_identity(identity_payload)}"
    manifest = {
        **identity_payload,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / snapshot_id
    if target.exists():
        verified = verify_execution_snapshot(target)
        if verified["snapshot_id"] != snapshot_id:
            raise ValueError("existing execution snapshot failed identity verification")
        return verified

    temporary = Path(tempfile.mkdtemp(prefix=".execution-snapshot-", dir=root))
    try:
        for kind, (raw, _) in prepared.items():
            (temporary / f"{kind}.jsonl").write_bytes(raw)
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for path in temporary.iterdir():
            os.chmod(path, 0o444)
        os.replace(temporary, target)
        os.chmod(target, 0o555)
    except Exception:
        if temporary.exists():
            os.chmod(temporary, 0o755)
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def verify_execution_snapshot(snapshot_dir: str | Path) -> dict[str, object]:
    root = Path(snapshot_dir).expanduser().resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported execution snapshot schema")
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "coverage_start",
            "coverage_end",
            "execution_price_basis",
            "signal_price_basis",
            "membership_mode",
            "synthetic_membership",
            "current_only_membership",
            "selection_policy_id",
            "rulebook_id",
            "artifacts",
            "universe_attestation",
        )
    }
    expected_id = f"{manifest['coverage_end']}-{_snapshot_identity(identity_payload)}"
    if manifest.get("snapshot_id") != expected_id or root.name != expected_id:
        raise ValueError("execution snapshot identity mismatch")
    for kind in ARTIFACT_SCHEMAS:
        descriptor = manifest["artifacts"][kind]
        path = root / descriptor["path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{kind} artifact must be a regular file")
        raw = path.read_bytes()
        if _digest(raw) != descriptor["content_sha256"]:
            raise ValueError(f"{kind} artifact hash mismatch")
        if len(raw.splitlines()) != descriptor["record_count"]:
            raise ValueError(f"{kind} artifact record count mismatch")
    return manifest
