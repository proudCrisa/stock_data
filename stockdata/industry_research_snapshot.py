"""Forward-only current industry observations derived from candidate captures."""

from __future__ import annotations

import argparse
import copy
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from . import candidate_admission_capture


SCHEMA_VERSION = "stockdata-current-industry-research-snapshot/1"
TAXONOMY_ID = "baostock.query_stock_industry"
TAXONOMY_VERSION = "unknown"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE = re.compile(r"^(?:sh|sz|bj)\d{6}$")
_RAW_FIELDS = {
    "updateDate", "code", "code_name", "industry", "industryClassification",
}


class IndustryResearchSnapshotError(ValueError):
    """A current industry observation cannot be replayed exactly."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise IndustryResearchSnapshotError(
            "industry snapshot is not canonical JSON") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise IndustryResearchSnapshotError(f"{field} must be timezone-aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndustryResearchSnapshotError(
            f"{field} must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndustryResearchSnapshotError(f"{field} must be timezone-aware")
    return parsed


def _day(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IndustryResearchSnapshotError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise IndustryResearchSnapshotError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise IndustryResearchSnapshotError(f"{field} must be an ISO date")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise IndustryResearchSnapshotError(f"{field} must be non-empty text")
    return value


def _mapping(record: dict, *, asof: str, observed_at: str) -> dict:
    code = record.get("code")
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        raise IndustryResearchSnapshotError("industry record code is invalid")
    rows = record.get("industry_rows")
    if not isinstance(rows, list) or not rows:
        raise IndustryResearchSnapshotError(f"industry mapping is missing for {code}")
    if len(rows) != 1:
        identities = {
            _canonical(row) for row in rows if isinstance(row, dict)
        }
        kind = "duplicate" if len(identities) == 1 else "conflicting"
        raise IndustryResearchSnapshotError(
            f"industry mapping is {kind} for {code}")
    row = rows[0]
    if not isinstance(row, dict) or set(row) != {
            "update_date", "code", "industry", "classification", "raw"}:
        raise IndustryResearchSnapshotError("industry mapping row schema is invalid")
    raw = row["raw"]
    if not isinstance(raw, dict) or set(raw) != _RAW_FIELDS:
        raise IndustryResearchSnapshotError("industry raw row schema is invalid")
    provider_code = f"{code[:2]}.{code[2:]}"
    update_date = _day(row["update_date"], "provider update_date")
    if update_date > asof:
        raise IndustryResearchSnapshotError("provider update_date is after snapshot asof")
    if row["code"] != code or raw["code"] != provider_code \
            or raw["updateDate"] != update_date \
            or raw["industry"] != row["industry"] \
            or raw["industryClassification"] != row["classification"]:
        raise IndustryResearchSnapshotError(
            "industry normalized row differs from provider raw row")
    _text(raw["code_name"], "provider code_name")
    industry = _text(row["industry"], "industry")
    classification = _text(row["classification"], "industry classification")
    return {
        "instrument_code": code,
        "industry": industry,
        "classification": classification,
        "provider_update_date": update_date,
        "observed_at": observed_at,
        "available_at": observed_at,
        "raw": copy.deepcopy(raw),
    }


def _unsigned_snapshot(capture: object, *, decision_cutoff: str) -> dict:
    try:
        verified = candidate_admission_capture.verify_capture(capture)
    except (candidate_admission_capture.CandidateAdmissionCaptureError,
            TypeError, KeyError, AttributeError) as exc:
        raise IndustryResearchSnapshotError(
            "candidate admission capture is invalid") from exc
    asof = _day(verified["asof"], "capture asof")
    observed_at = verified["generated_at"]
    observed = _timestamp(observed_at, "capture observed_at")
    cutoff = _timestamp(decision_cutoff, "decision_cutoff")
    if cutoff.astimezone(SHANGHAI).date().isoformat() != asof:
        raise IndustryResearchSnapshotError(
            "industry snapshot cannot backfill another asof")
    if verified["blockers"]:
        raise IndustryResearchSnapshotError(
            "industry snapshot source coverage is incomplete")
    records = [
        _mapping(record, asof=asof, observed_at=observed_at)
        for record in verified["records"]
    ]
    requested = sorted(verified["requested_codes"])
    if sorted(row["instrument_code"] for row in records) != requested:
        raise IndustryResearchSnapshotError(
            "industry snapshot does not cover the requested panel")
    records.sort(key=lambda row: row["instrument_code"])
    available = cutoff >= observed
    excluded = [] if available else [
        {**copy.deepcopy(record), "reason": "available_after_cutoff"}
        for record in records
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "current-industry-research-only",
        "pit_mode": "forward_only_current_observation",
        "asof": asof,
        "decision_cutoff": decision_cutoff,
        "status": "available" if available else "unavailable",
        "reason": ("current_mapping_available" if available else
                   "mapping_observed_after_cutoff"),
        "observed_at": observed_at,
        "available_at": observed_at,
        "availability_basis": "capture_observed_at",
        "taxonomy": {"id": TAXONOMY_ID, "version": TAXONOMY_VERSION},
        "source": {
            "provider": "baostock",
            "sdk_package_version": verified["source_receipt"][
                "sdk_package_version"],
            "capture_schema_version": verified["schema_version"],
            "capture_sha256": verified["artifact_sha256"],
        },
        "requested_codes": requested,
        "records": records if available else [],
        "excluded": excluded,
        "authority": copy.deepcopy(verified["authority"]),
        "actions": [],
        "source_capture": copy.deepcopy(verified),
    }


def build_industry_research_snapshot(
    capture: object, *, decision_cutoff: str,
) -> dict:
    """Build a current-day research snapshot from one verified capture."""
    unsigned = _unsigned_snapshot(capture, decision_cutoff=decision_cutoff)
    return {**unsigned, "snapshot_sha256": _hash(unsigned)}


def verify_industry_research_snapshot(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "scope", "pit_mode", "asof", "decision_cutoff",
            "status", "reason",
            "observed_at", "available_at", "availability_basis", "taxonomy", "source",
            "requested_codes", "records", "excluded", "authority", "actions",
            "source_capture", "snapshot_sha256"}:
        raise IndustryResearchSnapshotError("industry snapshot schema is incomplete")
    expected = build_industry_research_snapshot(
        payload["source_capture"], decision_cutoff=payload["decision_cutoff"])
    if payload != expected:
        raise IndustryResearchSnapshotError(
            "industry snapshot does not replay from its source capture")
    return copy.deepcopy(expected)


def load_industry_research_snapshot(path: str | Path) -> dict:
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise IndustryResearchSnapshotError(
            "industry snapshot path must be a regular file")
    try:
        raw = source.read_bytes()
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndustryResearchSnapshotError("industry snapshot is unreadable") from exc
    if raw != _canonical(value):
        raise IndustryResearchSnapshotError(
            "industry snapshot bytes are not canonical")
    return verify_industry_research_snapshot(value)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise IndustryResearchSnapshotError(
                "industry snapshot has duplicate JSON keys")
        result[key] = value
    return result


def write_industry_research_snapshot(
    output_root: str | Path, snapshot: dict,
) -> Path:
    verified = verify_industry_research_snapshot(snapshot)
    raw = _canonical(verified)
    root = Path(output_root).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{verified['snapshot_sha256']}.json"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
            raise IndustryResearchSnapshotError(
                "content-addressed industry snapshot conflicts")
        return target
    descriptor, temporary_name = tempfile.mkstemp(prefix=".industry-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, target)
        temporary.unlink()
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
            raise IndustryResearchSnapshotError(
                "content-addressed industry snapshot conflicts")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--decision-cutoff", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    capture = candidate_admission_capture.load_capture(args.capture)
    snapshot = build_industry_research_snapshot(
        capture, decision_cutoff=args.decision_cutoff)
    path = write_industry_research_snapshot(args.output_root, snapshot)
    print(json.dumps({
        "status": "completed",
        "snapshot_path": str(path),
        "snapshot_sha256": snapshot["snapshot_sha256"],
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IndustryResearchSnapshotError", "SCHEMA_VERSION", "TAXONOMY_ID",
    "TAXONOMY_VERSION", "build_industry_research_snapshot",
    "load_industry_research_snapshot", "verify_industry_research_snapshot",
    "write_industry_research_snapshot",
]
