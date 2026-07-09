"""多源共识交叉验证（纯逻辑，无网络）。

输入多源 {source: {date: close}}，输出每交易日共识收盘价 + 一致性标记 + 离群源，
并与实时价比对最新日。可靠来自多源交叉，而非信任单一源。
"""
import pytest

from stockdata.consensus import cross_validate


class TestAllAgree:
    def test_full_agreement(self):
        data = {
            "baostock": {"2026-07-08": 1199.3},
            "mootdx":   {"2026-07-08": 1199.3},
            "pytdx":    {"2026-07-08": 1199.3},
        }
        r = cross_validate(data)["2026-07-08"]
        assert r.consensus == 1199.3
        assert r.n_sources == 3
        assert r.agreement is True
        assert r.outliers == []


class TestOutlierDetection:
    def test_one_source_off(self):
        data = {
            "baostock": {"2026-07-08": 1199.3},
            "mootdx":   {"2026-07-08": 1199.3},
            "pytdx":    {"2026-07-08": 1199.3},
            "weird":    {"2026-07-08": 1250.0},
        }
        r = cross_validate(data)["2026-07-08"]
        assert r.consensus == 1199.3
        assert r.agreement is False
        assert "weird" in r.outliers

    def test_tolerance_small_diff_ok(self):
        data = {"a": {"2026-07-08": 10.60}, "b": {"2026-07-08": 10.6001}}
        assert cross_validate(data, tol=0.001)["2026-07-08"].agreement is True


class TestRealtimeCheck:
    def test_matches_realtime(self):
        data = {"baostock": {"2026-07-08": 1199.3}, "mootdx": {"2026-07-08": 1199.3}}
        r = cross_validate(data, realtime={"date": "2026-07-08", "price": 1199.3})["2026-07-08"]
        assert r.realtime_match is True

    def test_realtime_mismatch_flagged(self):
        # 1250 vs 共识 1199.3 相对差 ~4.2% > 1% 容差 → 判不匹配
        data = {"baostock": {"2026-07-08": 1199.3}, "mootdx": {"2026-07-08": 1199.3}}
        r = cross_validate(data, realtime={"date": "2026-07-08", "price": 1250.0})["2026-07-08"]
        assert r.realtime_match is False


class TestSparseSources:
    def test_missing_dates_handled(self):
        data = {
            "baostock": {"2026-07-07": 10.4, "2026-07-08": 10.6},
            "efinance": {"2026-07-08": 10.6},
        }
        out = cross_validate(data)
        assert out["2026-07-07"].consensus == 10.4
        assert out["2026-07-07"].n_sources == 1
        assert out["2026-07-08"].n_sources == 2

    def test_empty_input(self):
        assert cross_validate({}) == {}

    def test_failed_source_strings_ignored(self):
        data = {"baostock": {"2026-07-08": 10.6}, "akshare": "ERR ConnectionError"}
        r = cross_validate(data)["2026-07-08"]
        assert r.n_sources == 1
        assert r.consensus == 10.6
