from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from stockdata.cache import Cache
from stockdata.daily_bar_product import (
    DailyBarProductError,
    build_daily_bar_manifest,
    write_daily_bar_manifest,
)


def _receipt(rows, *, code="sh.600000", adjustflag="3",
             start_date=None, end_date=None):
    return {
        "observed_at": "2026-08-28T08:10:00+00:00",
        "source": "baostock",
        "request": {
            "code": code,
            "start_date": start_date or rows[0][0],
            "end_date": end_date or rows[-1][0],
            "adjustflag": adjustflag,
        },
        "response": {
            "fields": "date,open,high,low,close,volume",
            "rows": rows,
        },
    }


def _database(path, *, code="600000.SH", include_second=True,
              receipt_code="sh.600000", is_final=True,
              receipt_start=None, receipt_end=None):
    response_rows = [
        ["2026-08-27", "9.1", "9.2", "9.0", "9.15", "1000"],
        ["2026-08-28", "9.0", "9.1", "8.9", "9.0", "1200"],
    ]
    receipt = _receipt(
        response_rows, code=receipt_code,
        start_date=receipt_start, end_date=receipt_end,
    )
    bars = [{
        "date": day, "open": float(open_), "high": float(high),
        "low": float(low), "close": float(close), "volume": float(volume),
        "retrieved_at": receipt["observed_at"], "is_final": is_final,
        "_capture_receipt": receipt,
    } for day, open_, high, low, close, volume in response_rows]
    if not include_second:
        bars = bars[:1]
    cache = Cache(path)
    cache.upsert(
        code, bars, source="baostock", adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
        capture_receipts=[receipt],
    )
    cache.close()
    return path


def _build(database, *, created_at="2026-08-28T21:40:00+08:00"):
    return build_daily_bar_manifest(
        database,
        code="sh600000",
        start="2026-08-27",
        end="2026-08-28",
        source="baostock",
        adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
        universe_version="2026-08-28-5a98fcf55b75",
        trading_calendar_version="cn_sse_szse_sessions_v1",
        created_at=created_at,
    )


def test_builds_portable_shadow_manifest_with_receipt_replay(tmp_path):
    manifest = _build(_database(tmp_path / "cache.sqlite"))

    assert manifest["authority_grade"] == "shadow"
    assert manifest["decision_eligible"] is False
    assert manifest["source_authentication"] == "unverified"
    assert manifest["quality_status"] == \
        "self_consistent_current_observation"
    assert manifest["permitted_uses"] == ["offline_replay", "shadow_compare"]
    assert manifest["fallback_status"] == "not_used"
    assert manifest["trading_calendar_version"] == \
        "cn_sse_szse_sessions_v1"
    product = manifest["products"][0]
    assert product["pit_mode"] == "current_observation"
    assert product["trading_calendar_version"] == \
        manifest["trading_calendar_version"]
    assert product["price_identity"] == {
        "source": "baostock",
        "adjustment_mode": "raw",
        "adjustment_version": "baostock-adjustflag-3",
        "volume_unit": "share",
    }
    assert product["finality"] == {
        "status": "source_marked_final", "watermark": "2026-08-28"
    }
    assert len(product["rows"]) == 2
    assert len(product["source_receipt_ids"]) == 1
    assert product["rows"][0]["source_receipt_id"] == \
        product["source_receipt_ids"][0]
    assert manifest["receipt_ids"] == product["source_receipt_ids"]
    assert manifest["manifest_id"] == f"shadow-{manifest['manifest_sha256']}"


def test_content_addressed_write_is_idempotent(tmp_path):
    manifest = _build(_database(tmp_path / "cache.sqlite"))

    first = write_daily_bar_manifest(tmp_path / "products", manifest)
    second = write_daily_bar_manifest(tmp_path / "products", manifest)

    assert first == second
    assert first.name == f"{manifest['manifest_sha256']}.json"
    assert json.loads(first.read_text()) == manifest


def test_rejects_missing_exact_boundary(tmp_path):
    database = _database(tmp_path / "cache.sqlite", include_second=False)

    with pytest.raises(DailyBarProductError, match="exact requested boundaries"):
        _build(database)


def test_rejects_unfinalized_row(tmp_path):
    database = _database(tmp_path / "cache.sqlite", is_final=False)

    with pytest.raises(DailyBarProductError, match="not final and receipted"):
        _build(database)


def test_rejects_cross_security_receipt(tmp_path):
    database = _database(
        tmp_path / "cache.sqlite", receipt_code="sz.000001"
    )

    with pytest.raises(DailyBarProductError, match="does not replay row"):
        _build(database)


def test_rejects_receipt_request_range_that_excludes_row(tmp_path):
    database = _database(
        tmp_path / "cache.sqlite", receipt_start="2026-08-28"
    )

    with pytest.raises(DailyBarProductError, match="request range excludes row"):
        _build(database)


def test_rejects_tampered_receipt_response(tmp_path):
    database = _database(tmp_path / "cache.sqlite")
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER collection_receipts_no_update")
    response = json.dumps({
        "fields": "date,open,high,low,close,volume",
        "rows": [["2026-08-27", "9.1", "9.2", "9.0", "99", "1000"]],
    }, separators=(",", ":"))
    connection.execute(
        "UPDATE collection_receipts SET response_json=?",
        (response,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(DailyBarProductError, match="response hash mismatch"):
        _build(database)


def test_rejects_rehashed_response_that_does_not_cover_rows(tmp_path):
    database = _database(tmp_path / "cache.sqlite")
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER collection_receipts_no_update")
    response = json.dumps({
        "fields": "date,open,high,low,close,volume",
        "rows": [["2026-08-27", "9.1", "9.2", "9.0", "99", "1000"]],
    }, separators=(",", ":"))
    connection.execute(
        "UPDATE collection_receipts SET response_json=?,response_sha256=?",
        (response, hashlib.sha256(response.encode()).hexdigest()),
    )
    connection.commit()
    connection.close()

    with pytest.raises(DailyBarProductError, match="does not replay row"):
        _build(database)


def test_rejects_receipt_observed_after_decision_cutoff(tmp_path):
    database = _database(tmp_path / "cache.sqlite")

    with pytest.raises(DailyBarProductError, match="after decision cutoff"):
        _build(database, created_at="2026-08-28T08:09:59+00:00")


def test_write_rejects_invalid_outer_seal(tmp_path):
    manifest = _build(_database(tmp_path / "cache.sqlite"))
    manifest["quality_status"] = "forged"

    with pytest.raises(DailyBarProductError, match="not sealed"):
        write_daily_bar_manifest(tmp_path / "products", manifest)
