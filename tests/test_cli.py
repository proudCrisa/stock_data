"""Slice 9: 兼容 CLI —— 输出 findesk 格式 JSON 到 stdout。

供 super-trader-rqgm 那类"读 stdin 列式 JSON"的脚本零改造对接：
    stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30 | scripts/ffd_sync.py

纯逻辑 build_params(argv) 与网络/IO 分离。
"""
import pytest

from stockdata.cli import build_params


class TestBuildParams:
    def test_history_single(self):
        p = build_params(["history", "--code", "600519.SH",
                          "--start", "2024-01-01", "--end", "2024-06-30"])
        assert p == {
            "kind": "query",
            "function": "history",
            "code": "600519.SH",
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
        }

    def test_quote_history_multi(self):
        p = build_params(["quote_history", "--codes", "600519.SH,000001.SZ",
                          "--start", "2024-01-01", "--end", "2024-06-30"])
        assert p["kind"] == "quote_history"
        assert p["codes"] == ["600519.SH", "000001.SZ"]

    def test_missing_code_raises(self):
        with pytest.raises(SystemExit):
            build_params(["history", "--start", "2024-01-01"])

    def test_unknown_subcommand_raises(self):
        with pytest.raises(SystemExit):
            build_params(["financials", "--code", "600519.SH"])
