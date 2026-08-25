"""Export hash-verified local provider rows for RQGM feature research."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "stockdata-rqgm-feature-snapshot/1"
FEATURE_RECORD_SCHEMA = "stockdata-rqgm-feature-record/1"
SOURCE_RECORD_SCHEMA = "stockdata-rqgm-feature-source-record/1"
EVIDENCE_GRADE = "FORWARD_PIT_RESEARCH_ONLY"
PROVIDER = "sina-market-center-hs-a-v1"
FEATURES_FILE = "features.jsonl"
SOURCE_FILE = "source.jsonl"
MANIFEST_FILE = "manifest.json"

_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SZ|SH|BJ)$")
_PROVIDER_SYMBOL = re.compile(r"^(sz|sh|bj)([0-9]{6})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELD_MAPPING = (
    ("float_market_cap_10k_cny", "nmc"),
    ("market_cap_10k_cny", "mktcap"),
    ("price_to_book", "pb"),
    ("price_to_earnings", "per"),
    ("turnover_ratio_percent", "turnoverratio"),
)


class FeatureSnapshotError(ValueError):
    """Raised when source evidence or a feature snapshot is invalid."""


def _reject_constant(value: str) -> None:
    raise FeatureSnapshotError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeatureSnapshotError("duplicate JSON key is forbidden")
        result[key] = value
    return result


def _strict_json(raw: str | bytes, label: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeatureSnapshotError(f"{label} is not strict JSON") from exc


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FeatureSnapshotError("artifact contains non-canonical JSON") from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aware_timestamp(value: object, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value:
        raise FeatureSnapshotError(f"{label} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FeatureSnapshotError(f"{label} must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeatureSnapshotError(f"{label} must be an aware ISO timestamp")
    return value, parsed


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FeatureSnapshotError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FeatureSnapshotError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise FeatureSnapshotError(f"{label} must be a canonical ISO date")
    return value


def _symbols(values: object) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise FeatureSnapshotError("symbols must be a non-empty frozen panel")
    normalized = tuple(str(value) for value in values)
    if any(not _SYMBOL.fullmatch(value) for value in normalized):
        raise FeatureSnapshotError("symbols contain an invalid A-share identifier")
    if normalized != tuple(sorted(set(normalized))):
        raise FeatureSnapshotError("symbols must use unique canonical order")
    return normalized


def _normalized_provider_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise FeatureSnapshotError("provider row symbol is invalid")
    match = _PROVIDER_SYMBOL.fullmatch(value)
    if match is None:
        raise FeatureSnapshotError("provider row symbol is invalid")
    exchange, code = match.groups()
    return f"{code}.{exchange.upper()}"


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeatureSnapshotError(f"{label} must be a finite JSON number")
    result = float(value)
    if not math.isfinite(result):
        raise FeatureSnapshotError(f"{label} must be a finite JSON number")
    return result


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FeatureSnapshotError(f"{label} must be a regular file")
    return path.read_bytes()


def _jsonl(records: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join((_canonical(record) + "\n").encode("ascii") for record in records)


def _manifest_payload(
    *,
    source_database_sha256: str,
    receipt_id: int,
    observed_at: str,
    response_sha256: str,
    effective_date: str,
    available_at: str,
    symbols: tuple[str, ...],
    feature_sha256: str,
    source_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "source_database_sha256": source_database_sha256,
        "receipt": {
            "receipt_id": receipt_id,
            "observed_at": observed_at,
            "response_sha256": response_sha256,
        },
        "effective_date": effective_date,
        "available_at": available_at,
        "symbols": list(symbols),
        "field_mapping": [
            {"feature": feature, "source_key": source_key}
            for feature, source_key in _FIELD_MAPPING
        ],
        "features": {
            "path": FEATURES_FILE,
            "schema_version": FEATURE_RECORD_SCHEMA,
            "sha256": feature_sha256,
            "record_count": len(symbols),
        },
        "source": {
            "path": SOURCE_FILE,
            "schema_version": SOURCE_RECORD_SCHEMA,
            "sha256": source_sha256,
            "record_count": len(symbols),
        },
        "availability_semantics": "available_at_lte_decision_time",
        "revision_semantics": "receipt-response-and-source-row-sha256",
        "evidence_grade": EVIDENCE_GRADE,
        "execution_grade": False,
        "authoritative_for_execution": False,
    }


def _artifact_files(path: Path) -> tuple[bytes, bytes, bytes]:
    return tuple(
        _regular_file(path / name, name)
        for name in (MANIFEST_FILE, FEATURES_FILE, SOURCE_FILE)
    )  # type: ignore[return-value]


def _load_receipt(
    source_db: Path, receipt_id: int
) -> tuple[str, str, str, Mapping[str, object]]:
    try:
        connection = sqlite3.connect(
            f"file:{source_db}?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT observed_at,source,response_json,response_sha256
            FROM collection_receipts WHERE receipt_id = ?
            """,
            (receipt_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise FeatureSnapshotError("source receipt is not readable") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if row is None:
        raise FeatureSnapshotError("source receipt does not exist")
    source = str(row["source"])
    response_json = str(row["response_json"])
    response_sha256 = str(row["response_sha256"])
    if source != PROVIDER:
        raise FeatureSnapshotError("source receipt provider is unsupported")
    if not _SHA256.fullmatch(response_sha256) or (
        _sha256_bytes(response_json.encode("utf-8")) != response_sha256
    ):
        raise FeatureSnapshotError("source receipt response hash drifted")
    observed_at, _ = _aware_timestamp(row["observed_at"], "receipt observed_at")
    response = _strict_json(response_json, "source receipt response")
    if not isinstance(response, Mapping):
        raise FeatureSnapshotError("source receipt response must be an object")
    return observed_at, source, response_sha256, response


def _source_rows(
    response: Mapping[str, object], symbols: tuple[str, ...]
) -> dict[str, Mapping[str, object]]:
    rows = response.get("rows")
    if not isinstance(rows, list):
        raise FeatureSnapshotError("source receipt rows are missing")
    requested = set(symbols)
    selected: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FeatureSnapshotError("source receipt contains a malformed row")
        symbol = _normalized_provider_symbol(row.get("symbol"))
        if symbol not in requested:
            continue
        if symbol in selected:
            raise FeatureSnapshotError("source receipt repeats a requested symbol")
        selected[symbol] = dict(row)
    if set(selected) != requested:
        missing = sorted(requested - set(selected))
        raise FeatureSnapshotError(
            "source receipt lacks requested symbols: " + ",".join(missing)
        )
    return selected


def _records(
    *,
    rows: Mapping[str, Mapping[str, object]],
    effective_date: str,
    available_at: str,
    receipt_id: int,
    observed_at: str,
    response_sha256: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    features: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for symbol in sorted(rows):
        row = rows[symbol]
        values = {
            feature: _finite_number(row.get(source_key), source_key)
            for feature, source_key in _FIELD_MAPPING
        }
        revision_payload = {
            "provider": PROVIDER,
            "response_sha256": response_sha256,
            "source_row": row,
        }
        revision_id = _sha256_bytes(_canonical(revision_payload).encode("ascii"))
        features.append(
            {
                "schema_version": FEATURE_RECORD_SCHEMA,
                "symbol": symbol,
                "effective_date": effective_date,
                "available_at": available_at,
                "revision_id": revision_id,
                "values": values,
            }
        )
        sources.append(
            {
                "schema_version": SOURCE_RECORD_SCHEMA,
                "symbol": symbol,
                "effective_date": effective_date,
                "available_at": available_at,
                "revision_id": revision_id,
                "receipt": {
                    "receipt_id": receipt_id,
                    "source": PROVIDER,
                    "observed_at": observed_at,
                    "response_sha256": response_sha256,
                },
                "source_row": row,
            }
        )
    return features, sources


def _write_regular(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def export_sina_receipt_feature_snapshot(
    source_db: str | Path,
    output_root: str | Path,
    *,
    receipt_id: int,
    effective_date: str,
    symbols: tuple[str, ...],
    available_at: str | None = None,
) -> Path:
    """Export one receipt's frozen factor rows without mutating source evidence."""

    source_path = Path(source_db).expanduser().resolve(strict=True)
    if source_path.is_symlink() or not source_path.is_file():
        raise FeatureSnapshotError("source database must be a regular file")
    if isinstance(receipt_id, bool) or not isinstance(receipt_id, int) or receipt_id < 1:
        raise FeatureSnapshotError("receipt_id must be a positive integer")
    panel = _symbols(symbols)
    effective = _iso_date(effective_date, "effective_date")
    database_sha256 = _sha256_file(source_path)
    observed_at, _, response_sha256, response = _load_receipt(source_path, receipt_id)
    observed_text, observed_value = _aware_timestamp(observed_at, "receipt observed_at")
    available_text, available_value = _aware_timestamp(
        available_at or observed_text, "available_at"
    )
    if available_value < observed_value:
        raise FeatureSnapshotError("available_at cannot precede receipt observation")
    rows = _source_rows(response, panel)
    feature_records, source_records = _records(
        rows=rows,
        effective_date=effective,
        available_at=available_text,
        receipt_id=receipt_id,
        observed_at=observed_text,
        response_sha256=response_sha256,
    )
    feature_raw = _jsonl(feature_records)
    source_raw = _jsonl(source_records)
    payload = _manifest_payload(
        source_database_sha256=database_sha256,
        receipt_id=receipt_id,
        observed_at=observed_text,
        response_sha256=response_sha256,
        effective_date=effective,
        available_at=available_text,
        symbols=panel,
        feature_sha256=_sha256_bytes(feature_raw),
        source_sha256=_sha256_bytes(source_raw),
    )
    snapshot_id = _sha256_bytes(_canonical(payload).encode("ascii"))
    manifest_raw = (_canonical({"snapshot_id": snapshot_id, **payload}) + "\n").encode(
        "ascii"
    )

    root = Path(output_root).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise FeatureSnapshotError("output root must be a regular directory")
    target = root / snapshot_id
    expected = (manifest_raw, feature_raw, source_raw)
    if target.exists():
        verify_feature_snapshot(target)
        if _artifact_files(target) != expected:
            raise FeatureSnapshotError("existing snapshot identity has different bytes")
        return target

    temporary = Path(tempfile.mkdtemp(prefix=".feature-snapshot-", dir=root))
    try:
        _write_regular(temporary / MANIFEST_FILE, manifest_raw)
        _write_regular(temporary / FEATURES_FILE, feature_raw)
        _write_regular(temporary / SOURCE_FILE, source_raw)
        try:
            temporary.rename(target)
            target.chmod(0o555)
        except FileExistsError:
            verify_feature_snapshot(target)
            if _artifact_files(target) != expected:
                raise FeatureSnapshotError(
                    "existing snapshot identity has different bytes"
                )
        verify_feature_snapshot(target)
        if _sha256_file(source_path) != database_sha256:
            raise FeatureSnapshotError("source database changed during export")
        return target
    finally:
        if temporary.exists():
            temporary.chmod(0o755)
            shutil.rmtree(temporary)


def _strict_records(raw: bytes, label: str) -> list[Mapping[str, object]]:
    records: list[Mapping[str, object]] = []
    for line in raw.splitlines():
        value = _strict_json(line, label)
        if not isinstance(value, Mapping):
            raise FeatureSnapshotError(f"{label} must contain objects")
        records.append(value)
    return records


def verify_feature_snapshot(root: str | Path) -> Mapping[str, object]:
    """Verify a self-contained feature snapshot without trusting its source database."""

    path = Path(root).expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_dir():
        raise FeatureSnapshotError("feature snapshot root must be a regular directory")
    manifest_raw, feature_raw, source_raw = _artifact_files(path)
    manifest = _strict_json(manifest_raw, "feature snapshot manifest")
    if not isinstance(manifest, Mapping):
        raise FeatureSnapshotError("feature snapshot manifest must be an object")
    if manifest_raw != (_canonical(dict(manifest)) + "\n").encode("ascii"):
        raise FeatureSnapshotError("feature snapshot manifest bytes are not canonical")
    required = {
        "snapshot_id",
        "schema_version",
        "provider",
        "source_database_sha256",
        "receipt",
        "effective_date",
        "available_at",
        "symbols",
        "field_mapping",
        "features",
        "source",
        "availability_semantics",
        "revision_semantics",
        "evidence_grade",
        "execution_grade",
        "authoritative_for_execution",
    }
    if set(manifest) != required or manifest["schema_version"] != SCHEMA_VERSION:
        raise FeatureSnapshotError("feature snapshot manifest schema drifted")
    if (
        manifest["provider"] != PROVIDER
        or manifest["evidence_grade"] != EVIDENCE_GRADE
        or manifest["execution_grade"] is not False
        or manifest["authoritative_for_execution"] is not False
        or manifest["availability_semantics"]
        != "available_at_lte_decision_time"
        or manifest["revision_semantics"]
        != "receipt-response-and-source-row-sha256"
    ):
        raise FeatureSnapshotError("feature snapshot authority boundary drifted")
    database_sha256 = manifest["source_database_sha256"]
    snapshot_id = manifest["snapshot_id"]
    if (
        not isinstance(database_sha256, str)
        or not _SHA256.fullmatch(database_sha256)
        or not isinstance(snapshot_id, str)
        or not _SHA256.fullmatch(snapshot_id)
        or path.name != snapshot_id
    ):
        raise FeatureSnapshotError("feature snapshot identity is invalid")
    payload = dict(manifest)
    del payload["snapshot_id"]
    if _sha256_bytes(_canonical(payload).encode("ascii")) != snapshot_id:
        raise FeatureSnapshotError("feature snapshot identity drifted")

    symbols = _symbols(manifest["symbols"])
    effective = _iso_date(manifest["effective_date"], "effective_date")
    available, available_value = _aware_timestamp(
        manifest["available_at"], "available_at"
    )
    receipt = manifest["receipt"]
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "receipt_id",
        "observed_at",
        "response_sha256",
    }:
        raise FeatureSnapshotError("feature snapshot receipt is invalid")
    if (
        isinstance(receipt["receipt_id"], bool)
        or not isinstance(receipt["receipt_id"], int)
        or receipt["receipt_id"] < 1
        or not isinstance(receipt["response_sha256"], str)
        or not _SHA256.fullmatch(receipt["response_sha256"])
    ):
        raise FeatureSnapshotError("feature snapshot receipt is invalid")
    observed, observed_value = _aware_timestamp(
        receipt["observed_at"], "receipt observed_at"
    )
    if available_value < observed_value:
        raise FeatureSnapshotError("feature snapshot availability is backdated")
    expected_mapping = [
        {"feature": feature, "source_key": source_key}
        for feature, source_key in _FIELD_MAPPING
    ]
    if manifest["field_mapping"] != expected_mapping:
        raise FeatureSnapshotError("feature snapshot field mapping drifted")

    for descriptor, name, raw, schema in (
        (manifest["features"], FEATURES_FILE, feature_raw, FEATURE_RECORD_SCHEMA),
        (manifest["source"], SOURCE_FILE, source_raw, SOURCE_RECORD_SCHEMA),
    ):
        if not isinstance(descriptor, Mapping) or set(descriptor) != {
            "path",
            "schema_version",
            "sha256",
            "record_count",
        }:
            raise FeatureSnapshotError("feature snapshot descriptor is invalid")
        if (
            descriptor["path"] != name
            or descriptor["schema_version"] != schema
            or descriptor["sha256"] != _sha256_bytes(raw)
            or descriptor["record_count"] != len(symbols)
        ):
            raise FeatureSnapshotError("feature snapshot file identity drifted")

    features = _strict_records(feature_raw, "feature records")
    sources = _strict_records(source_raw, "source records")
    if len(features) != len(symbols) or len(sources) != len(symbols):
        raise FeatureSnapshotError("feature snapshot record count drifted")
    feature_symbols = tuple(str(record.get("symbol")) for record in features)
    source_symbols = tuple(str(record.get("symbol")) for record in sources)
    if feature_symbols != symbols or source_symbols != symbols:
        raise FeatureSnapshotError("feature snapshot records are not canonical")

    for feature_record, source_record, symbol in zip(
        features, sources, symbols, strict=True
    ):
        if set(feature_record) != {
            "schema_version",
            "symbol",
            "effective_date",
            "available_at",
            "revision_id",
            "values",
        } or set(source_record) != {
            "schema_version",
            "symbol",
            "effective_date",
            "available_at",
            "revision_id",
            "receipt",
            "source_row",
        }:
            raise FeatureSnapshotError("feature snapshot record schema drifted")
        if (
            feature_record["schema_version"] != FEATURE_RECORD_SCHEMA
            or source_record["schema_version"] != SOURCE_RECORD_SCHEMA
            or feature_record["symbol"] != symbol
            or source_record["symbol"] != symbol
            or feature_record["effective_date"] != effective
            or source_record["effective_date"] != effective
            or feature_record["available_at"] != available
            or source_record["available_at"] != available
            or feature_record["revision_id"] != source_record["revision_id"]
        ):
            raise FeatureSnapshotError("feature snapshot record binding drifted")
        source_receipt = source_record["receipt"]
        source_row = source_record["source_row"]
        if not isinstance(source_receipt, Mapping) or not isinstance(
            source_row, Mapping
        ):
            raise FeatureSnapshotError("feature source record is invalid")
        if source_receipt != {
            "receipt_id": receipt["receipt_id"],
            "source": PROVIDER,
            "observed_at": observed,
            "response_sha256": receipt["response_sha256"],
        }:
            raise FeatureSnapshotError("feature source receipt drifted")
        if _normalized_provider_symbol(source_row.get("symbol")) != symbol:
            raise FeatureSnapshotError("feature source symbol drifted")
        revision_payload = {
            "provider": PROVIDER,
            "response_sha256": receipt["response_sha256"],
            "source_row": source_row,
        }
        revision_id = _sha256_bytes(_canonical(revision_payload).encode("ascii"))
        if feature_record["revision_id"] != revision_id:
            raise FeatureSnapshotError("feature revision identity drifted")
        values = feature_record["values"]
        if not isinstance(values, Mapping) or tuple(sorted(values)) != tuple(
            feature for feature, _ in _FIELD_MAPPING
        ):
            raise FeatureSnapshotError("feature values schema drifted")
        for feature, source_key in _FIELD_MAPPING:
            expected = _finite_number(source_row.get(source_key), source_key)
            actual = _finite_number(values.get(feature), feature)
            if actual != expected:
                raise FeatureSnapshotError("feature value drifted from physical source")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockdata-rqgm-feature-snapshot")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-db", required=True)
    export.add_argument("--output-root", required=True)
    export.add_argument("--receipt-id", required=True, type=int)
    export.add_argument("--effective-date", required=True)
    export.add_argument("--available-at")
    export.add_argument("--symbols", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export":
        symbols = tuple(sorted(part.strip() for part in args.symbols.split(",")))
        path = export_sina_receipt_feature_snapshot(
            args.source_db,
            args.output_root,
            receipt_id=args.receipt_id,
            effective_date=args.effective_date,
            symbols=symbols,
            available_at=args.available_at,
        )
        manifest = verify_feature_snapshot(path)
    else:
        path = Path(args.snapshot).expanduser().resolve(strict=True)
        manifest = verify_feature_snapshot(path)
    print(
        _canonical(
            {
                "snapshot": str(path),
                "snapshot_id": manifest["snapshot_id"],
                "evidence_grade": manifest["evidence_grade"],
                "execution_grade": manifest["execution_grade"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
