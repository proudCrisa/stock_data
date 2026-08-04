"""Slice 9: 兼容 CLI —— 输出 findesk 格式 JSON 到 stdout。

供 super-trader-rqgm 那类"读 stdin 列式 JSON"的脚本零改造对接：
    stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30 | scripts/ffd_sync.py

纯逻辑 build_params(argv) 与网络/IO 分离。
"""
import pytest

from stockdata.cli import build_params


class TestBuildParams:
    def test_rqgm_provider_export_params(self):
        assert build_params(
            ["rqgm-provider-export", "--bundle-file", "/tmp/bundle.json"]
        ) == {
            "kind": "rqgm_provider_export",
            "bundle_file": "/tmp/bundle.json",
        }

    def test_forward_corporate_actions_capture_params(self):
        assert build_params(
            [
                "forward-corporate-actions-capture",
                "--database",
                "/tmp/evidence.sqlite",
                "--date",
                "2026-07-28",
            ]
        ) == {
            "kind": "forward_corporate_actions_capture",
            "database": "/tmp/evidence.sqlite",
            "observation_date": "2026-07-28",
        }

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

    def test_update_is_bounded_by_codes_and_start(self):
        p = build_params([
            "update", "--codes", "600519.SH,000001.SZ",
            "--start", "2024-01-01", "--end", "2024-12-31",
        ])
        assert p["kind"] == "update"
        assert p["codes"] == "600519.SH,000001.SZ"
        assert p["start_date"] == "2024-01-01"

    def test_snapshot_create_params(self):
        p = build_params([
            "snapshot", "create", "--output", "/tmp/snapshots",
            "--as-of", "2024-12-31", "--codes", "600519.SH",
        ])
        assert p["kind"] == "snapshot_create"
        assert p["codes"] == ["600519.SH"]

    def test_snapshot_create_accepts_explicit_source(self):
        p = build_params([
            "snapshot", "create", "--output", "/tmp/snapshots",
            "--as-of", "2024-12-31", "--source", "baostock",
        ])
        assert p["source"] == "baostock"

    def test_execution_readiness_params(self):
        p = build_params([
            "execution-readiness", "--source", "baostock",
            "--adjustment-mode", "raw",
            "--adjustment-version", "baostock-adjustflag-3",
            "--panel-file", "/tmp/panel.json",
            "--database", "/tmp/cache.sqlite",
        ])
        assert p == {
            "kind": "execution_readiness",
            "source": "baostock",
            "adjustment_mode": "raw",
            "adjustment_version": "baostock-adjustflag-3",
            "panel_file": "/tmp/panel.json",
            "database": "/tmp/cache.sqlite",
        }

    def test_forward_capture_requires_explicit_database_and_cohort(self):
        p = build_params([
            "forward-capture", "--database", "/tmp/evidence.sqlite",
            "--codes-file", "/tmp/cohort.txt", "--start", "2026-07-27",
        ])
        assert p == {
            "kind": "forward_capture",
            "database": "/tmp/evidence.sqlite",
            "codes": None,
            "codes_file": "/tmp/cohort.txt",
            "start_date": "2026-07-27",
            "end_date": "",
            "source": "baostock",
            "adjustment_version": None,
        }

    def test_forward_context_capture_params(self):
        p = build_params([
            "forward-context-capture", "--database", "/tmp/evidence.sqlite",
            "--date", "2026-07-27",
        ])
        assert p == {
            "kind": "forward_context_capture",
            "database": "/tmp/evidence.sqlite",
            "effective_date": "2026-07-27",
        }

    def test_full_execution_readiness_params(self):
        p = build_params([
            "full-execution-readiness",
            "--database", "/tmp/evidence.sqlite",
            "--source", "tencent",
            "--adjustment-mode", "raw",
            "--adjustment-version", "tencent-qt-daily-v1",
            "--panel-file", "/tmp/panel.json",
        ])
        assert p == {
            "kind": "full_execution_readiness",
            "database": "/tmp/evidence.sqlite",
            "source": "tencent",
            "adjustment_mode": "raw",
            "adjustment_version": "tencent-qt-daily-v1",
            "signal_adjustment_mode": None,
            "signal_adjustment_version": None,
            "panel_file": "/tmp/panel.json",
        }
