import hashlib
import json
import os
import sqlite3
import subprocess
import sys

from stockdata.cache import Cache
from stockdata.execution_readiness import check_execution_readiness, load_panel


IDENTITY = {
    "source": "baostock",
    "adjustment_mode": "raw",
    "adjustment_version": "baostock-adjustflag-3",
}


def _add_bar(cache, day="2025-07-01", observed_at=None):
    observed_at = observed_at or f"{day}T07:10:00+00:00"
    response = {
        "fields": "date,open,high,low,close,volume",
        "rows": [[day, "10", "10.2", "9.9", "10.1", "1000"]],
    }
    receipt = {
        "observed_at": observed_at,
        "source": "baostock",
        "request": {"code": "sz.000001", "start_date": day, "end_date": day},
        "response": response,
    }
    cache.upsert(
        "000001.SZ",
        [{
            "date": day, "open": 10.0, "high": 10.2, "low": 9.9,
            "close": 10.1, "volume": 1000.0, "retrieved_at": observed_at,
            "_capture_receipt": receipt,
        }],
        capture_receipts=[receipt],
        **IDENTITY,
    )


def _codes(report):
    return {item["code"] for item in report["blockers"]}


def test_migrated_legacy_rows_remain_blocked(tmp_path):
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE daily (code TEXT,date TEXT,open REAL,high REAL,low REAL,"
        "close REAL,volume REAL,PRIMARY KEY(code,date))"
    )
    connection.execute(
        "INSERT INTO daily VALUES (?,?,?,?,?,?,?)",
        ("000001.SZ", "2025-07-01", 10, 10.2, 9.9, 10.1, 1000),
    )
    connection.commit()
    connection.close()
    Cache(database).close()

    report = check_execution_readiness(database)

    assert report["ready"] is False
    assert report["schema_version"] == 4
    assert "missing_receipts" in _codes(report)


def test_receipt_linked_exact_panel_is_ready(tmp_path):
    database = tmp_path / "ready.sqlite"
    cache = Cache(database)
    _add_bar(cache)
    cache.close()

    report = check_execution_readiness(
        database, panel={("000001.SZ", "2025-07-01")}, **IDENTITY
    )

    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["counts"]["selected_rows"] == 1


def test_after_close_capture_is_ready_for_next_open(tmp_path):
    database = tmp_path / "after-close.sqlite"
    cache = Cache(database)
    _add_bar(cache, observed_at="2025-07-01T08:10:00+00:00")
    cache.close()

    report = check_execution_readiness(
        database, panel={("000001.SZ", "2025-07-01")}, **IDENTITY
    )

    assert report["ready"] is True


def test_capture_after_next_open_is_post_hoc(tmp_path):
    database = tmp_path / "after-next-open.sqlite"
    cache = Cache(database)
    _add_bar(cache, observed_at="2025-07-02T01:26:00+00:00")
    cache.close()

    report = check_execution_readiness(
        database, panel={("000001.SZ", "2025-07-01")}, **IDENTITY
    )

    assert report["ready"] is False
    assert "unknown_next_session" in _codes(report)


def test_claimed_final_bar_before_close_is_blocked(tmp_path):
    database = tmp_path / "before-close.sqlite"
    cache = Cache(database)
    _add_bar(cache, observed_at="2025-07-01T06:59:59+00:00")
    cache.close()

    report = check_execution_readiness(database, **IDENTITY)

    assert report["ready"] is False
    assert "availability_precedes_finalization" in _codes(report)


def test_partial_panel_reports_missing_dates(tmp_path):
    database = tmp_path / "partial.sqlite"
    cache = Cache(database)
    _add_bar(cache)
    cache.close()

    report = check_execution_readiness(
        database,
        panel={
            ("000001.SZ", "2025-07-01"),
            ("000001.SZ", "2025-07-02"),
        },
        **IDENTITY,
    )

    assert report["ready"] is False
    assert "missing_panel_rows" in _codes(report)


def test_receipt_hash_and_response_mismatches_are_blocked(tmp_path):
    database = tmp_path / "tampered.sqlite"
    cache = Cache(database)
    _add_bar(cache, "2025-07-01")
    _add_bar(cache, "2025-07-02")
    rows = cache._conn.execute(
        "SELECT receipt_id,response_json FROM collection_receipts ORDER BY receipt_id"
    ).fetchall()
    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute(
        "UPDATE collection_receipts SET response_sha256='bad' WHERE receipt_id=?",
        (rows[0]["receipt_id"],),
    )
    changed = json.loads(rows[1]["response_json"])
    changed["rows"][0][4] = "99.9"
    changed_json = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    cache._conn.execute(
        "UPDATE collection_receipts SET response_json=?,response_sha256=? WHERE receipt_id=?",
        (
            changed_json,
            hashlib.sha256(changed_json.encode()).hexdigest(),
            rows[1]["receipt_id"],
        ),
    )
    cache._conn.execute(
        "CREATE TRIGGER collection_receipts_no_update "
        "BEFORE UPDATE ON collection_receipts BEGIN "
        "SELECT RAISE(ABORT, 'collection receipts are append-only'); END"
    )
    cache._conn.commit()
    cache.close()

    report = check_execution_readiness(database, **IDENTITY)

    assert report["ready"] is False
    assert {"receipt_hash_mismatch", "receipt_response_mismatch"} <= _codes(report)


def test_same_name_noop_receipt_trigger_is_rejected(tmp_path):
    database = tmp_path / "noop-trigger.sqlite"
    cache = Cache(database)
    _add_bar(cache)
    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute(
        "CREATE TRIGGER collection_receipts_no_update "
        "BEFORE UPDATE ON collection_receipts BEGIN SELECT 1; END"
    )
    cache._conn.commit()
    cache.close()

    report = check_execution_readiness(database, **IDENTITY)

    assert report["ready"] is False
    assert "invalid_receipt_triggers" in _codes(report)


def test_receipt_observation_time_prevents_retrieval_backdating(tmp_path):
    database = tmp_path / "backdated.sqlite"
    cache = Cache(database)
    _add_bar(cache)
    cache._conn.execute(
        "UPDATE daily SET retrieved_at='2025-07-01T06:50:00+00:00'"
    )
    cache._conn.execute(
        "DROP TRIGGER collection_receipts_no_update"
    )
    cache._conn.execute(
        "UPDATE collection_receipts SET observed_at='2026-07-24T10:00:00+00:00'"
    )
    cache._conn.execute(
        "CREATE TRIGGER collection_receipts_no_update "
        "BEFORE UPDATE ON collection_receipts BEGIN "
        "SELECT RAISE(ABORT, 'collection receipts are append-only'); END"
    )
    cache._conn.commit()
    cache.close()

    report = check_execution_readiness(database, **IDENTITY)

    assert report["ready"] is False
    assert {"receipt_timestamp_mismatch", "unknown_next_session"} <= _codes(report)


def test_load_panel_accepts_existing_split_overlay_shape(tmp_path):
    path = tmp_path / "panel.json"
    path.write_text(json.dumps({
        "splits": {"search-validation": ["000001.SZ@2025-07-01"]}
    }))

    assert load_panel(path) == {("000001.SZ", "2025-07-01")}


def test_readiness_cli_does_not_migrate_or_modify_database(tmp_path):
    database = tmp_path / "legacy-readonly.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE daily (code TEXT,date TEXT,PRIMARY KEY(code,date))"
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()
    env = {**os.environ, "STOCKDATA_DB": str(database)}

    completed = subprocess.run(
        [sys.executable, "-m", "stockdata.cli", "execution-readiness"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout)["ready"] is False
    assert database.read_bytes() == before
