"""make_service / make_tonghuashun_service：按价格身份实例化服务。

非 baostock 身份默认只读本地缓存、不触发网络回补（避免跨源误标复权口径）；
全程离线，不打网络。
"""
from stockdata import make_service, make_tonghuashun_service
from stockdata.cache import Cache

BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 200},
]
BAOSTOCK_IDENTITY = ("baostock", "qfq", "baostock-adjustflag-2")
THS_IDENTITY = ("tonghuashun", "qfq", "ths-qfq-v1")


def _identity(svc):
    return (svc.source, svc.adjustment_mode, svc.adjustment_version)


def test_default_identity_is_baostock(tmp_path):
    svc = make_service(db_path=tmp_path / "x.sqlite")
    assert _identity(svc) == BAOSTOCK_IDENTITY
    svc.cache.close()


def test_raw_service_fetches_raw_and_preserves_receipt(tmp_path, monkeypatch):
    import stockdata.service as service_mod
    from stockdata.fetch_baostock import CapturedBars

    receipt = {
        "observed_at": "2024-01-02T08:00:00+00:00",
        "source": "baostock",
        "request": {
            "code": "sh.600000",
            "start_date": "2024-01-02",
            "end_date": "2024-01-02",
            "adjustflag": "3",
        },
        "response": {
            "fields": "date,open,high,low,close,volume",
            "rows": [["2024-01-02", "10", "10.2", "9.9", "10.1", "1000"]],
        },
    }
    calls = []

    def _fetch(code, start, end, *, adjustment_mode="qfq"):
        calls.append((code, start, end, adjustment_mode))
        bar = {
            "date": "2024-01-02", "open": 10.0, "high": 10.2,
            "low": 9.9, "close": 10.1, "volume": 1000.0,
            "source": "baostock", "adjustment_mode": "raw",
            "adjustment_version": "baostock-adjustflag-3",
            "is_final": True, "_capture_receipt": receipt,
        }
        return CapturedBars([bar], receipt)

    monkeypatch.setattr(service_mod, "_default_primary", _fetch)
    monkeypatch.setattr(
        service_mod, "_default_today",
        lambda code: (_ for _ in ()).throw(
            AssertionError("raw service must not merge a Tencent today bar")
        ),
    )
    svc = make_service(
        source="baostock", adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
        db_path=tmp_path / "raw.sqlite",
    )

    rows = svc.get_history(
        "sh600000", "2024-01-02", "2024-01-02", today="2024-01-02"
    )

    assert calls == [("600000.SH", "2024-01-02", "2024-01-02", "raw")]
    assert rows[0]["adjustment_mode"] == "raw"
    assert rows[0]["adjustment_version"] == "baostock-adjustflag-3"
    assert isinstance(rows[0]["receipt_id"], int)
    svc.cache.close()


def test_tonghuashun_identity(tmp_path):
    svc = make_tonghuashun_service(db_path=tmp_path / "x.sqlite")
    assert _identity(svc) == THS_IDENTITY
    svc.cache.close()


def test_tonghuashun_reads_cache_without_fetch(tmp_path, monkeypatch):
    import stockdata.service as service_mod

    def _boom(code, start, end):
        raise AssertionError("非 baostock 身份不应触发网络回补")

    monkeypatch.setattr(service_mod, "_default_primary", _boom)
    db = tmp_path / "x.sqlite"
    cache = Cache(db)
    cache.upsert("000063.SZ", BARS, source="tonghuashun", adjustment_mode="qfq",
                 adjustment_version="ths-qfq-v1")
    cache.close()

    svc = make_tonghuashun_service(db_path=db)
    # 请求区间超出缓存覆盖 → 触发缺口计算，但回补为 no-op，仅返回缓存已有部分
    rows = svc.get_history("000063.SZ", "2024-01-01", "2024-06-30", today="2024-07-01")
    assert [r["date"] for r in rows] == ["2024-01-02", "2024-01-03"]
    # 未写入任何 baostock 身份数据
    assert svc.cache.covered_range(
        "000063.SZ", source="baostock", adjustment_mode="qfq",
        adjustment_version="baostock-adjustflag-2") is None
    svc.cache.close()


def test_fetch_missing_override_rejected_for_non_baostock(tmp_path):
    # 非 baostock 身份 + fetch_missing=True 是复权混源路径
    # （默认 fetcher 是 baostock 取数，会误标身份），必须被拒绝。
    import pytest

    with pytest.raises(ValueError, match="复权混源"):
        make_service(source="tonghuashun", adjustment_mode="qfq",
                     adjustment_version="ths-qfq-v1",
                     db_path=tmp_path / "x.sqlite", fetch_missing=True)
