from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from stockdata.rqgm_feature_snapshot import (
    export_sina_receipt_feature_snapshot,
    verify_feature_snapshot,
)


DAY = "2026-08-21"
OBSERVED_AT = f"{DAY}T15:00:00+08:00"
SYMBOLS = ("000001.SZ", "600000.SH")


def _valid_rows() -> list[dict[str, object]]:
    return [
        {
            "symbol": "sz000001",
            "pb": 1.2,
            "per": 10.5,
            "mktcap": 123456.0,
            "nmc": 100000.0,
            "turnoverratio": 2.5,
        },
        {
            "symbol": "sh600000",
            "pb": 0.9,
            "per": 8.25,
            "mktcap": 234567.0,
            "nmc": 200000.0,
            "turnoverratio": 1.75,
        },
    ]


def _response(rows: list[dict[str, object]]) -> str:
    return json.dumps(
        {"rows": rows},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=True,
    )


def _create_source_db(
    tmp_path: Path,
    *,
    rows: list[dict[str, object]] | None = None,
    observed_at: str = OBSERVED_AT,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "source.sqlite"
    response_json = _response(rows if rows is not None else _valid_rows())
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE collection_receipts (
            receipt_id INTEGER PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO collection_receipts
            (receipt_id, observed_at, source, request_json, response_json,
             response_sha256, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            7,
            observed_at,
            "sina-market-center-hs-a-v1",
            "{}",
            response_json,
            hashlib.sha256(response_json.encode("utf-8")).hexdigest(),
            observed_at,
        ),
    )
    connection.commit()
    connection.close()
    return database


def _export(
    database: Path,
    output_root: Path,
    *,
    symbols: tuple[str, ...] = SYMBOLS,
    available_at: str | None = OBSERVED_AT,
) -> Path:
    return export_sina_receipt_feature_snapshot(
        database,
        output_root,
        receipt_id=7,
        effective_date=DAY,
        symbols=symbols,
        available_at=available_at,
    )


def _make_writable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.iterdir():
        path.chmod(0o644)


def test_valid_export_verify_and_canonical_idempotency(tmp_path: Path) -> None:
    database = _create_source_db(tmp_path)
    output_root = tmp_path / "snapshots"

    first = _export(database, output_root)
    first_bytes = {
        name: (first / name).read_bytes()
        for name in ("manifest.json", "features.jsonl", "source.jsonl")
    }
    second = _export(database, output_root)

    assert second == first
    assert {
        path.name for path in first.iterdir()
    } == {"manifest.json", "features.jsonl", "source.jsonl"}
    assert {
        name: (second / name).read_bytes()
        for name in first_bytes
    } == first_bytes

    verified = verify_feature_snapshot(first)
    assert verified["evidence_grade"] == "FORWARD_PIT_RESEARCH_ONLY"
    assert verified["execution_grade"] is False
    assert verified["authoritative_for_execution"] is False


def test_export_does_not_change_source_database_bytes(tmp_path: Path) -> None:
    database = _create_source_db(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    _export(database, tmp_path / "snapshots")

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_export_rejects_missing_symbol_without_publishing_artifact(
    tmp_path: Path,
) -> None:
    database = _create_source_db(tmp_path, rows=[_valid_rows()[0]])
    output_root = tmp_path / "snapshots"

    with pytest.raises(ValueError):
        _export(database, output_root)

    assert not output_root.exists() or not any(output_root.iterdir())


def test_export_rejects_duplicate_normalized_symbols(tmp_path: Path) -> None:
    database = _create_source_db(tmp_path)
    with pytest.raises(ValueError):
        _export(
            database,
            tmp_path / "input-duplicates",
            symbols=("000001.SZ", "000001.SZ"),
        )

    duplicate_rows = _valid_rows()
    duplicate_rows.append({**duplicate_rows[0], "symbol": "sz000001"})
    database = _create_source_db(tmp_path / "source-duplicates", rows=duplicate_rows)
    with pytest.raises(ValueError):
        _export(
            database,
            tmp_path / "source-duplicates" / "snapshots",
            symbols=("000001.SZ",),
        )


def test_export_rejects_response_hash_tampering(tmp_path: Path) -> None:
    database = _create_source_db(tmp_path)
    connection = sqlite3.connect(database)
    response = json.loads(
        connection.execute(
            "SELECT response_json FROM collection_receipts WHERE receipt_id=7"
        ).fetchone()[0]
    )
    response["rows"][0]["pb"] = 9.9
    changed = _response(response["rows"])
    connection.execute(
        "UPDATE collection_receipts SET response_json=? WHERE receipt_id=7",
        (changed,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError):
        _export(database, tmp_path / "snapshots")


def test_export_rejects_availability_before_observation(tmp_path: Path) -> None:
    database = _create_source_db(tmp_path)

    with pytest.raises(ValueError):
        _export(
            database,
            tmp_path / "snapshots",
            available_at="2026-08-21T14:59:59+08:00",
        )


@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param(True, id="bool"),
        pytest.param(None, id="null"),
        pytest.param("1.2", id="text"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_export_rejects_invalid_mapped_values(
    tmp_path: Path, bad_value: object
) -> None:
    rows = _valid_rows()
    rows[0]["pb"] = bad_value
    database = _create_source_db(tmp_path, rows=rows)

    with pytest.raises(ValueError):
        _export(database, tmp_path / "snapshots")


@pytest.mark.parametrize("filename", ["features.jsonl", "source.jsonl", "manifest.json"])
def test_verify_rejects_feature_source_and_manifest_byte_drift(
    tmp_path: Path, filename: str
) -> None:
    snapshot = _export(_create_source_db(tmp_path), tmp_path / "snapshots")
    _make_writable(snapshot)
    path = snapshot / filename
    path.write_bytes(path.read_bytes() + (b"\n" if filename != "manifest.json" else b" "))

    with pytest.raises(ValueError):
        verify_feature_snapshot(snapshot)


def test_export_rejects_existing_content_id_directory_collision(
    tmp_path: Path,
) -> None:
    database = _create_source_db(tmp_path)
    output_root = tmp_path / "snapshots"
    snapshot = _export(database, output_root)
    _make_writable(snapshot)
    features = snapshot / "features.jsonl"
    features.write_bytes(features.read_bytes() + b"\n")

    with pytest.raises(ValueError):
        _export(database, output_root)


def test_verify_rejects_duplicate_manifest_json_key(tmp_path: Path) -> None:
    snapshot = _export(_create_source_db(tmp_path), tmp_path / "snapshots")
    _make_writable(snapshot)
    (snapshot / "manifest.json").write_bytes(
        b'{"schema_version":"rqgm-test","schema_version":"rqgm-test"}\n'
    )

    with pytest.raises(ValueError):
        verify_feature_snapshot(snapshot)
