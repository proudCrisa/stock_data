"""Slice 5: 腾讯实时今日 bar（qt.gtimg.cn，免 key）。

产出"今日 bar" {date, open, high, low, close, volume}，供 end_date 含当日时
覆盖/追加最后一根。纯解析 parse_tencent_bar 与网络 fetch_today_bar 分离。

腾讯字段: [1]名称 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [30]时间戳 [33]最高 [34]最低
"""
import pytest

from stockdata.fetch_tencent import parse_tencent_bar


# 真实格式样本：逐索引精确构造（腾讯字段布局真实）
def _build_sample(price="1199.30", prev="1188.80", open_="1188.77",
                  vol="25776", ts="20260708161439", high="1200.98", low="1177.00"):
    parts = [""] * 45
    parts[0] = "1"
    parts[1] = "贵州茅台"
    parts[2] = "600519"
    parts[3] = price
    parts[4] = prev
    parts[5] = open_
    parts[6] = vol
    parts[30] = ts
    parts[33] = high
    parts[34] = low
    return 'v_sh600519="' + "~".join(parts) + '";'


_SAMPLE = _build_sample()


class TestParseTencentBar:
    def test_extracts_ohlcv(self):
        bar = parse_tencent_bar(_SAMPLE, "600519.SH")
        assert bar["open"] == 1188.77
        assert bar["high"] == 1200.98
        assert bar["low"] == 1177.00
        assert bar["close"] == 1199.30   # 现价作为今日收
        assert bar["volume"] == 25776.0

    def test_date_from_timestamp(self):
        bar = parse_tencent_bar(_SAMPLE, "600519.SH")
        assert bar["date"] == "2026-07-08"   # 从 [30] 时间戳前 8 位解析

    def test_zero_price_rejected(self):
        raw = _build_sample(price="0.00")
        assert parse_tencent_bar(raw, "600519.SH") is None

    def test_zero_volume_rejected(self):
        # 停牌/无成交 → 占位，拒绝
        raw = _build_sample(vol="0")
        assert parse_tencent_bar(raw, "600519.SH") is None

    def test_malformed_returns_none(self):
        assert parse_tencent_bar("garbage", "600519.SH") is None

    def test_empty_returns_none(self):
        assert parse_tencent_bar("", "600519.SH") is None


@pytest.mark.network
class TestFetchTodayBarIntegration:
    def test_real_600519(self):
        from stockdata.fetch_tencent import fetch_today_bar

        bar = fetch_today_bar("600519.SH")
        # 交易时段外可能返回 None（无当日成交占位），仅在拿到时校验结构
        if bar is not None:
            assert set(bar) == {"date", "open", "high", "low", "close", "volume"}
            assert bar["close"] > 0
