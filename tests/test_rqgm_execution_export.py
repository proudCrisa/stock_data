import base64
import hashlib
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.historical_universe_attestation import (
    load_historical_universe_attestation,
    verify_historical_universe_attestation,
)
from stockdata.rqgm_execution_export import (
    _historical_universe_rows,
    _panel_from_overlay,
    _price_rows,
)


def test_panel_requires_complete_symbol_date_product(tmp_path):
    path = tmp_path / "split.json"
    path.write_text(
        json.dumps(
            {
                "splits": {
                    "search-validation": [
                        "000001.SZ@2025-07-01",
                        "000001.SZ@2025-07-02",
                        "600000.SH@2025-07-01",
                    ]
                }
            }
        )
    )
    with pytest.raises(ValueError, match="complete symbol-date product"):
        _panel_from_overlay(path, "2025-07-01", "2025-07-02")


def test_panel_can_bind_train_only_warmup_dates(tmp_path):
    path = tmp_path / "split.json"
    path.write_text(
        json.dumps(
            {
                "splits": {
                    "train": [
                        "000001.SZ@2025-06-27",
                        "600000.SH@2025-06-27",
                        "000001.SZ@2025-06-30",
                        "600000.SH@2025-06-30",
                    ],
                    "search-validation": [
                        "000001.SZ@2025-07-01",
                        "600000.SH@2025-07-01",
                    ],
                }
            }
        )
    )
    symbols, dates, panel = _panel_from_overlay(
        path, "2025-06-30", "2025-07-01", warmup_bars=1
    )
    assert symbols == ("000001.SZ", "600000.SH")
    assert dates == ("2025-06-30", "2025-07-01")
    assert len(panel) == 4


def test_price_rows_fail_closed_on_incomplete_panel(tmp_path):
    database = tmp_path / "prices.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE daily (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, retrieved_at TEXT, adjustment_mode TEXT,
            adjustment_version TEXT, source TEXT, is_final INTEGER, receipt_id INTEGER
        )
        """
    )
    response = json.dumps({
        "fields": "date,open,high,low,close,volume",
        "rows": [["2025-07-01", "10", "10.2", "9.9", "10.1", "1000"]],
    }, separators=(",", ":"))
    connection.execute(
        "CREATE TABLE collection_receipts "
        "(receipt_id INTEGER, source TEXT, observed_at TEXT, response_json TEXT, "
        "response_sha256 TEXT)"
    )
    connection.execute(
        "INSERT INTO collection_receipts VALUES (?,?,?,?,?)",
        (1, "baostock", "2026-07-24T10:00:00+00:00", response,
         hashlib.sha256(response.encode()).hexdigest()),
    )
    connection.execute(
        "INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "000001.SZ",
            "2025-07-01",
            10.0,
            10.2,
            9.9,
            10.1,
            1000.0,
            "2026-07-24T10:00:00+00:00",
            "raw",
            "baostock-adjustflag-3",
            "baostock",
            1,
            1,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="do not cover panel"):
        _price_rows(
            database,
            {
                ("000001.SZ", "2025-07-01"),
                ("000001.SZ", "2025-07-02"),
            },
            source="baostock",
            adjustment_mode="raw",
            adjustment_version="baostock-adjustflag-3",
        )


def test_price_rows_reject_post_hoc_retrieval_timestamp(tmp_path):
    database = tmp_path / "prices.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE daily (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, retrieved_at TEXT, adjustment_mode TEXT,
            adjustment_version TEXT, source TEXT, is_final INTEGER, receipt_id INTEGER
        )
        """
    )
    response = json.dumps({
        "fields": "date,open,high,low,close,volume",
        "rows": [["2025-07-01", "10", "10.2", "9.9", "10.1", "1000"]],
    }, separators=(",", ":"))
    connection.execute(
        "CREATE TABLE collection_receipts "
        "(receipt_id INTEGER, source TEXT, observed_at TEXT, response_json TEXT, "
        "response_sha256 TEXT)"
    )
    connection.execute(
        "INSERT INTO collection_receipts VALUES (?,?,?,?,?)",
        (1, "baostock", "2026-07-24T10:00:00+00:00", response,
         hashlib.sha256(response.encode()).hexdigest()),
    )
    connection.execute(
        "INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000001.SZ", "2025-07-01", 10.0, 10.2, 9.9, 10.1, 1000.0,
         "2026-07-24T10:00:00+00:00", "raw", "baostock-adjustflag-3",
         "baostock", 1, 1),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="point-in-time availability"):
        _price_rows(
            database,
            {("000001.SZ", "2025-07-01")},
            source="baostock",
            adjustment_mode="raw",
            adjustment_version="baostock-adjustflag-3",
        )


def _signed_universe(tmp_path):
    row = {
        "effective_date": "2025-07-01",
        "available_at": "2025-07-01T09:00:00+08:00",
        "symbol": "000001.SZ",
        "is_member": True,
    }
    raw = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    universe = tmp_path / "historical-universe.jsonl"
    universe.write_bytes(raw)
    payload = {
        "issuer": "fixture-exchange", "key_id": "2025-test",
        "published_snapshot_id": "exchange-2025-07-01-v1",
        "selection_policy_id": "all-listed-a-shares/v1", "membership_mode": "historical",
        "synthetic": False, "current_only": False, "coverage_start": "2025-07-01",
        "coverage_end": "2025-07-01", "content_sha256": hashlib.sha256(raw).hexdigest(),
        "full_record_count": 1, "daily_record_counts": {"2025-07-01": 1},
    }
    private = Ed25519PrivateKey.generate()
    signature = private.sign(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    manifest = tmp_path / "historical-universe-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "stockdata-historical-universe-attestation/1", "algorithm": "ed25519",
        "payload": payload, "signature_base64": base64.b64encode(signature).decode(),
    }), encoding="utf-8")
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return universe, manifest, base64.b64encode(public).decode()


def test_historical_universe_attestation_is_signed_and_exports_full_authority(tmp_path):
    universe, manifest, public = _signed_universe(tmp_path)
    rows, evidence = load_historical_universe_attestation(universe, manifest)
    verify_historical_universe_attestation(
        evidence, trusted_public_keys={("fixture-exchange", "2025-test"): public}
    )
    exported, exported_evidence = _historical_universe_rows(
        universe, manifest, {("000001.SZ", "2025-07-01")}
    )
    assert exported == rows
    assert exported_evidence == evidence


def test_historical_universe_attestation_rejects_untrusted_or_modified_content(tmp_path):
    universe, manifest, _ = _signed_universe(tmp_path)
    _, evidence = load_historical_universe_attestation(universe, manifest)
    with pytest.raises(ValueError, match="not enrolled"):
        verify_historical_universe_attestation(evidence, trusted_public_keys={})
    universe.write_text(universe.read_text().replace("true", "false"), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        load_historical_universe_attestation(universe, manifest)
