"""stockdata CLI realtime 子命令 —— 供 RQGM 适配层取盘中价(T1)。

build_params 纯逻辑直测;main 的 service 调用 monkeypatch api.get_realtime。
"""
import io
import json
import pytest

from stockdata.cli import build_params, main


class TestRealtimeBuildParams:
    def test_realtime_single(self):
        p = build_params(["realtime", "--code", "600519.SH"])
        assert p == {"kind": "realtime", "code": "600519.SH"}

    def test_realtime_missing_code_raises(self):
        with pytest.raises(SystemExit):
            build_params(["realtime"])

    def test_history_still_works(self):
        # 不破坏原有 history
        p = build_params(["history", "--code", "600519.SH", "--start", "2024-01-01"])
        assert p["function"] == "history"


class TestRealtimeMain:
    def test_realtime_main_outputs_bar(self, monkeypatch, capsys):
        import stockdata.api as api
        bar = {"date": "2026-07-08", "open": 26.53, "high": 27.11,
               "low": 23.87, "close": 23.88, "volume": 3255178.0}
        monkeypatch.setattr(api, "get_realtime", lambda code: bar)
        rc = main(["realtime", "--code", "600667.SH"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["close"] == 23.88 and out["date"] == "2026-07-08"

    def test_realtime_main_null_when_unavailable(self, monkeypatch, capsys):
        import stockdata.api as api
        monkeypatch.setattr(api, "get_realtime", lambda code: None)
        rc = main(["realtime", "--code", "600667.SH"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) is None
