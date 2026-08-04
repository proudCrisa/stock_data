from stockdata.cache import Cache
from stockdata.fetch_baostock import CapturedBars
from stockdata.forward_capture import capture_forward_evidence


def _fetch(code, start, end):
    close = "10.1" if code == "000001.SZ" else "20.1"
    row = [start, close, close, close, close, "1000"]
    observed_at = f"{start}T08:10:00+00:00"
    receipt = {
        "observed_at": observed_at,
        "source": "baostock",
        "request": {"code": code, "start_date": start, "end_date": end},
        "response": {
            "fields": "date,open,high,low,close,volume",
            "rows": [row],
        },
    }
    bar = {
        "date": start,
        "open": float(close),
        "high": float(close),
        "low": float(close),
        "close": float(close),
        "volume": 1000.0,
        "source": "baostock",
        "adjustment_mode": "raw",
        "adjustment_version": "baostock-adjustflag-3",
        "retrieved_at": observed_at,
        "is_final": True,
        "_capture_receipt": receipt,
    }
    return CapturedBars([bar], receipt)


def test_forward_capture_is_ready_and_idempotent(tmp_path):
    cache = Cache(tmp_path / "evidence.sqlite")

    first = capture_forward_evidence(
        cache, ["000001.SZ", "600000.SH"], "2025-07-01", "2025-07-01",
        fetcher=_fetch,
    )
    second = capture_forward_evidence(
        cache, ["000001.SZ", "600000.SH"], "2025-07-01", "2025-07-01",
        fetcher=_fetch,
    )

    assert first["ready"] is True
    assert first["panel_size"] == 2
    assert second["ready"] is True
    assert second["sync"]["up_to_date"] == 2
    assert cache._conn.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == 2


def test_forward_capture_blocks_partial_symbol_date_product(tmp_path):
    cache = Cache(tmp_path / "partial.sqlite")

    def partial_fetch(code, start, end):
        if code == "600000.SH":
            receipt = {
                "observed_at": f"{start}T08:10:00+00:00",
                "source": "baostock",
                "request": {"code": code},
                "response": {"fields": "date,open,high,low,close,volume", "rows": []},
            }
            return CapturedBars([], receipt)
        return _fetch(code, start, end)

    result = capture_forward_evidence(
        cache, ["000001.SZ", "600000.SH"], "2025-07-01", "2025-07-01",
        fetcher=partial_fetch,
    )

    assert result["ready"] is False
    assert result["readiness"]["blockers"][0]["code"] == "missing_panel_rows"


def test_forward_capture_rejects_database_outside_fixed_cohort(tmp_path):
    cache = Cache(tmp_path / "mixed.sqlite")
    cache.upsert(
        "600519.SH",
        [{"date": "2025-07-01", "open": 1, "high": 1, "low": 1,
          "close": 1, "volume": 1}],
        adjustment_mode="raw",
    )

    try:
        capture_forward_evidence(
            cache, ["000001.SZ"], "2025-07-01", "2025-07-01", fetcher=_fetch
        )
    except ValueError as exc:
        assert "outside the cohort" in str(exc)
    else:
        raise AssertionError("mixed cohort database must fail closed")


def test_forward_capture_binds_immutable_cohort_identity(tmp_path):
    cache = Cache(tmp_path / "cohort.sqlite")
    first = capture_forward_evidence(
        cache, ["000001.SZ"], "2025-07-01", "2025-07-01", fetcher=_fetch
    )

    try:
        capture_forward_evidence(
            cache,
            ["000001.SZ", "600000.SH"],
            "2025-07-01",
            "2025-07-01",
            fetcher=_fetch,
        )
    except ValueError as exc:
        assert "identity drift" in str(exc)
    else:
        raise AssertionError("cohort identity drift must fail closed")

    row = cache._conn.execute(
        "SELECT spec_sha256 FROM forward_capture_cohort WHERE singleton=1"
    ).fetchone()
    assert row[0] == first["cohort_sha256"]
