"""Slice 8: MCP 工具处理逻辑（与 FastMCP 注册分离，便于测试）。

handle_ffd_query / handle_ffd_quote_history 是纯逻辑：解析入参 → service → 列式组装。
FastMCP 工具（server.py）只是薄壳，注入真实 service 与 today。
"""
import pytest

from stockdata.cache import Cache
from stockdata.service import HistoryService
from stockdata.mcp_handlers import handle_ffd_query, handle_ffd_quote_history


BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 200},
]


def _fake_primary(bars):
    def f(code, start, end):
        return [b for b in bars if start <= b["date"] <= end]
    return f


@pytest.fixture
def svc(tmp_path):
    cache = Cache(tmp_path / "mcp.sqlite")
    return HistoryService(cache, primary_fetch=_fake_primary(BARS),
                          fallback_fetch=None, today_fetch=lambda c: None)


class TestFfdQuery:
    def test_history_single_code(self, svc):
        out = handle_ffd_query(
            {"function": "history", "code": "600519.SH",
             "start_date": "2024-01-01", "end_date": "2024-01-31"},
            svc, today="2024-02-01")
        d = out["data"]
        assert d["time"] == ["2024-01-02", "2024-01-03"]
        assert d["code"] == ["600519.SH", "600519.SH"]
        assert d["open"][0] == 1.0        # 含 open

    def test_columnar_contract(self, svc):
        out = handle_ffd_query(
            {"function": "history", "code": "600519.SH",
             "start_date": "2024-01-01", "end_date": "2024-01-31"},
            svc, today="2024-02-01")
        assert set(out["data"]) == {"time", "code", "open", "high", "low", "close", "volume"}
        assert isinstance(out["data"]["close"], list)

    def test_unknown_function_raises(self, svc):
        with pytest.raises(ValueError):
            handle_ffd_query({"function": "financials", "code": "600519.SH"},
                             svc, today="2024-02-01")

    def test_missing_code_raises(self, svc):
        with pytest.raises(ValueError):
            handle_ffd_query({"function": "history", "start_date": "2024-01-01"},
                             svc, today="2024-02-01")


class TestFfdQuoteHistory:
    def test_multi_code(self, svc):
        out = handle_ffd_quote_history(
            {"codes": ["600519.SH", "000001.SZ"],
             "start_date": "2024-01-01", "end_date": "2024-01-31"},
            svc, today="2024-02-01")
        # 两个标的都用同一 fake 源 → 各 2 天，共 4 行平铺
        assert len(out["data"]["time"]) == 4
        assert set(out["data"]["code"]) == {"600519.SH", "000001.SZ"}

    def test_single_code_string_accepted(self, svc):
        # codes 传单个字符串也应接受
        out = handle_ffd_quote_history(
            {"codes": "600519.SH", "start_date": "2024-01-01", "end_date": "2024-01-31"},
            svc, today="2024-02-01")
        assert len(out["data"]["time"]) == 2

    def test_missing_codes_raises(self, svc):
        with pytest.raises(ValueError):
            handle_ffd_quote_history({"start_date": "2024-01-01"}, svc, today="2024-02-01")
