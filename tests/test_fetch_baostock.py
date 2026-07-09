"""Slice 3: baostock 拉取层。

历史主源（前复权）。纯解析逻辑与网络调用分离：
- _parse_rows: baostock 行 → 内部 bar（纯函数，可无网络测试）
- fetch_baostock: 登录 + 查询 + 解析（集成，需网络，标记 network）
"""
import pytest

from stockdata.fetch_baostock import _parse_rows


FIELDS = "date,open,high,low,close,volume"


class TestSuppressStdout:
    def test_suppress_stdout_context_silences_prints(self, capsys):
        from stockdata.fetch_baostock import _suppress_stdout

        print("before")
        with _suppress_stdout():
            print("login success!")   # 模拟 baostock 往 stdout 打印
        print("after")
        out = capsys.readouterr().out
        assert "login success!" not in out   # 被抑制
        assert "before" in out
        assert "after" in out


class TestParseRows:
    def test_basic_parse(self):
        rows = [
            ["2024-01-02", "1558.4788", "1561.3776", "1524.9465", "1531.2258", "3215644"],
            ["2024-01-03", "1531.0000", "1540.0000", "1520.0000", "1535.5000", "2000000"],
        ]
        bars = _parse_rows(FIELDS, rows)
        assert len(bars) == 2
        assert bars[0] == {
            "date": "2024-01-02",
            "open": 1558.4788,
            "high": 1561.3776,
            "low": 1524.9465,
            "close": 1531.2258,
            "volume": 3215644.0,
        }

    def test_types_are_numeric(self):
        rows = [["2024-01-02", "10.5", "11", "10", "10.8", "12345"]]
        bar = _parse_rows(FIELDS, rows)[0]
        assert isinstance(bar["open"], float)
        assert isinstance(bar["volume"], float)

    def test_empty_rows(self):
        assert _parse_rows(FIELDS, []) == []

    def test_blank_volume_becomes_zero(self):
        # baostock 停牌日可能返回空字符串字段
        rows = [["2024-01-02", "", "", "", "", ""]]
        bar = _parse_rows(FIELDS, rows)[0]
        assert bar["date"] == "2024-01-02"
        assert bar["open"] == 0.0
        assert bar["volume"] == 0.0

    def test_field_order_respected(self):
        # 若字段顺序不同，按列名对齐
        fields = "date,close,open,high,low,volume"
        rows = [["2024-01-02", "10.8", "10.5", "11", "10", "12345"]]
        bar = _parse_rows(fields, rows)[0]
        assert bar["close"] == 10.8
        assert bar["open"] == 10.5


@pytest.mark.network
class TestFetchBaostockIntegration:
    def test_real_600519_2024h1(self):
        from stockdata.fetch_baostock import fetch_baostock

        bars = fetch_baostock("600519.SH", "2024-01-01", "2024-06-30")
        assert len(bars) == 117  # 2024H1 交易日数（真实核对值）
        assert bars[0]["date"] == "2024-01-02"
        assert bars[-1]["date"] == "2024-06-28"
        # 前复权价均为正
        assert all(b["close"] > 0 for b in bars)
        # date 升序
        dates = [b["date"] for b in bars]
        assert dates == sorted(dates)
