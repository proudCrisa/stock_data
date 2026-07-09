"""Slice 4: mootdx 拉取层（近端补齐/兜底，不复权原始价）。

纯解析 _parse_bars_df 与网络（tdx_client / fetch_mootdx）分离。
mootdx 返回 DatetimeIndex 的 DataFrame，列含 open/close/high/low/vol/volume/datetime。
"""
import pandas as pd
import pytest

from stockdata.fetch_mootdx import _parse_bars_df


def _make_df(rows):
    """构造与 mootdx 真实返回同构的 DataFrame（DatetimeIndex + 列）。"""
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["datetime"])
    return df


class TestParseBarsDf:
    def test_basic(self):
        df = _make_df([
            {"datetime": "2024-01-02 15:00", "open": 1.0, "close": 1.1,
             "high": 1.2, "low": 0.9, "vol": 100.0, "volume": 100.0, "amount": 1e6},
            {"datetime": "2024-01-03 15:00", "open": 1.1, "close": 1.2,
             "high": 1.3, "low": 1.0, "vol": 200.0, "volume": 200.0, "amount": 2e6},
        ])
        bars = _parse_bars_df(df)
        assert len(bars) == 2
        assert bars[0] == {
            "date": "2024-01-02",
            "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100.0,
        }

    def test_date_stripped_from_datetime(self):
        df = _make_df([
            {"datetime": "2024-05-06 15:00", "open": 1, "close": 1,
             "high": 1, "low": 1, "vol": 1, "volume": 1, "amount": 1},
        ])
        assert _parse_bars_df(df)[0]["date"] == "2024-05-06"

    def test_empty_df(self):
        df = pd.DataFrame(columns=["datetime", "open", "close", "high", "low", "vol", "volume"])
        assert _parse_bars_df(df) == []

    def test_none_df(self):
        # mootdx 无数据时可能返回 None
        assert _parse_bars_df(None) == []

    def test_ascending_by_date(self):
        df = _make_df([
            {"datetime": "2024-01-03 15:00", "open": 1, "close": 1, "high": 1, "low": 1, "vol": 1, "volume": 1, "amount": 1},
            {"datetime": "2024-01-02 15:00", "open": 1, "close": 1, "high": 1, "low": 1, "vol": 1, "volume": 1, "amount": 1},
        ])
        dates = [b["date"] for b in _parse_bars_df(df)]
        assert dates == ["2024-01-02", "2024-01-03"]


@pytest.mark.network
class TestFetchMootdxIntegration:
    def test_real_600519(self):
        from stockdata.fetch_mootdx import fetch_mootdx

        bars = fetch_mootdx("600519.SH", offset=5)
        assert len(bars) >= 1
        assert all(set(b) == {"date", "open", "high", "low", "close", "volume"} for b in bars)
        assert all(b["close"] > 0 for b in bars)
