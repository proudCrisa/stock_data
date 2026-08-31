"""Slice 9: 兼容 CLI —— 输出 findesk 格式 JSON 到 stdout。

供 super-trader-rqgm 那类"读 stdin 列式 JSON"的脚本零改造对接：
    stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30 | scripts/ffd_sync.py

纯逻辑 build_params(argv) 与网络/IO 分离。
"""
import json
import sys
import types

import pytest

from stockdata.cli import build_params, main


class TestBuildParams:
    def test_jqdata_bootstrap_has_no_credential_arguments(self):
        params = build_params([
            "jqdata-bootstrap",
            "--panel-file", "panel.json",
            "--max-rows", "20000",
        ])
        assert params == {
            "kind": "jqdata_bootstrap",
            "panel_file": "panel.json",
            "max_rows": 20000,
        }
        assert "account" not in params
        assert "secret" not in params

    def test_jqdata_bootstrap_prompts_and_closes_session(
        self, monkeypatch, tmp_path, capsys
    ):
        panel_file = tmp_path / "panel.json"
        panel_file.write_text(json.dumps(["000001.SZ@2026-07-22"]))
        sdk = types.ModuleType("jqdatasdk")
        sdk.closed = False
        sdk.logout = lambda: setattr(sdk, "closed", True)
        monkeypatch.setitem(sys.modules, "jqdatasdk", sdk)

        values = iter(["runtime-account", "runtime-secret"])
        monkeypatch.setattr("stockdata.cli.getpass.getpass", lambda _: next(values))
        received = {}

        def fake_authenticate(provider, account, secret):
            received.update(provider=provider, account=account, secret=secret)

        def fake_build(provider, *, panel, observed_at, max_rows):
            assert provider is sdk
            assert panel == {("000001.SZ", "2026-07-22")}
            assert observed_at.endswith("+08:00")
            assert max_rows == 20_000
            return {"authoritative": False, "evidence_grade": "VENDOR_BOOTSTRAP_ONLY"}

        monkeypatch.setattr("stockdata.jqdata_bootstrap.authenticate", fake_authenticate)
        monkeypatch.setattr("stockdata.jqdata_bootstrap.build_bootstrap_artifact", fake_build)

        assert main([
            "jqdata-bootstrap",
            "--panel-file", str(panel_file),
            "--max-rows", "20000",
        ]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["authoritative"] is False
        assert "runtime-account" not in captured.out + captured.err
        assert "runtime-secret" not in captured.out + captured.err
        assert received == {
            "provider": sdk,
            "account": "runtime-account",
            "secret": "runtime-secret",
        }
        assert sdk.closed is True

    def test_rqgm_provider_export_params(self):
        assert build_params(
            ["rqgm-provider-export", "--bundle-file", "/tmp/bundle.json"]
        ) == {
            "kind": "rqgm_provider_export",
            "bundle_file": "/tmp/bundle.json",
        }

    def test_rqgm_provider_export_rejects_unpublished_manifest(
        self, tmp_path, capsys
    ):
        from test_provider_export import _bundle

        bundle_file, _ = _bundle(tmp_path)
        unpublished = tmp_path / ".bundle-interrupted.json"
        bundle_file.rename(unpublished)

        with pytest.raises(ValueError, match=r"bundle\.json|published|basename"):
            main(["rqgm-provider-export", "--bundle-file", str(unpublished)])
        assert capsys.readouterr().out == ""

    def test_rqgm_provider_materialize_params(self):
        components = (
            "execution_prices",
            "signal_prices",
            "decision_context",
            "trading_calendar",
            "universe",
            "instrument_status",
            "corporate_actions",
            "market_rules",
            "availability_records",
        )
        args = [
            "rqgm-provider-materialize",
            "--output-dir",
            "/tmp/closure",
            "--database",
            "/tmp/cache.sqlite",
            "--registration-file",
            "/tmp/registration.json",
            "--snapshot-staging-directory",
            "/tmp/snapshots",
            "--panel-file",
            "/tmp/panel.json",
            "--source-receipt",
            "/tmp/receipt.json",
            "--execution-adjustment-file",
            "/tmp/execution.json",
            "--signal-adjustment-file",
            "/tmp/signal.json",
            "--source",
            "baostock",
        ]
        for component in components:
            args.extend(("--component-file", f"{component}=/tmp/{component}.json"))
        params = build_params(args)
        assert params["registration_file"] == "/tmp/registration.json"
        assert params["snapshot_staging_directory"] == "/tmp/snapshots"
        assert params["component_files"] == {
            component: f"/tmp/{component}.json" for component in components
        }

    @pytest.mark.parametrize(
        ("missing_flag", "missing_value"),
        [
            ("--registration-file", "/tmp/registration.json"),
            ("--snapshot-staging-directory", "/tmp/snapshots"),
        ],
    )
    def test_rqgm_provider_materialize_requires_continuity_arguments(
        self, missing_flag, missing_value
    ):
        args = [
            "rqgm-provider-materialize",
            "--output-dir", "/tmp/closure",
            "--database", "/tmp/cache.sqlite",
            "--registration-file", "/tmp/registration.json",
            "--snapshot-staging-directory", "/tmp/snapshots",
            "--panel-file", "/tmp/panel.json",
            "--source-receipt", "/tmp/receipt.json",
            "--execution-adjustment-file", "/tmp/execution.json",
            "--signal-adjustment-file", "/tmp/signal.json",
            "--source", "fixture",
            "--component-file", "execution_prices=/tmp/execution-prices.json",
        ]
        index = args.index(missing_flag)
        assert args[index + 1] == missing_value
        del args[index : index + 2]

        with pytest.raises(SystemExit):
            build_params(args)

    def test_rqgm_provider_materialize_dispatches_continuity_arguments(
        self, monkeypatch, capsys
    ):
        from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS

        received = {}

        def materialize(**kwargs):
            received.update(kwargs)
            return {"bundle_file": "/tmp/closure/bundle.json", "receipt": {}}

        monkeypatch.setattr(
            "stockdata.provider_materializer.materialize_provider_bundle", materialize
        )
        args = [
            "rqgm-provider-materialize",
            "--output-dir", "/tmp/closure",
            "--database", "/tmp/cache.sqlite",
            "--registration-file", "/tmp/registration.json",
            "--snapshot-staging-directory", "/tmp/snapshots",
            "--panel-file", "/tmp/panel.json",
            "--source-receipt", "/tmp/receipt.json",
            "--execution-adjustment-file", "/tmp/execution.json",
            "--signal-adjustment-file", "/tmp/signal.json",
            "--source", "fixture",
        ]
        for component in REQUIRED_COMPONENTS:
            args.extend(("--component-file", f"{component}=/tmp/{component}.json"))

        assert main(args) == 0

        assert received["registration_file"] == "/tmp/registration.json"
        assert received["snapshot_staging_directory"] == "/tmp/snapshots"
        assert json.loads(capsys.readouterr().out)["bundle_file"] == (
            "/tmp/closure/bundle.json"
        )

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

    def test_update_calendar_params(self):
        p = build_params([
            "update-calendar", "--database", "/tmp/cache.sqlite",
            "--start", "2024-01-01", "--end", "2024-12-31",
        ])
        assert p == {
            "kind": "update_calendar",
            "database": "/tmp/cache.sqlite",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        }

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


class TestWriteCommandExitCode:
    def test_update_returns_nonzero_when_sync_reports_errors(
        self, monkeypatch, tmp_path, capsys
    ):
        from stockdata import cli

        def fake_sync(cache, codes, start, end, *, adjustment_mode="qfq"):
            return {
                "start": start,
                "end": end,
                "source": "baostock",
                "adjustment_mode": adjustment_mode,
                "adjustment_version": "baostock-adjustflag-2",
                "symbols": [
                    {
                        "code": "600519.SH",
                        "status": "error",
                        "start": start,
                        "end": end,
                        "written": 0,
                        "error": "boom",
                    }
                ],
                "synced": 0,
                "up_to_date": 0,
                "errors": 1,
            }

        monkeypatch.setattr("stockdata.sync.sync_symbols", fake_sync)
        monkeypatch.setenv("STOCKDATA_DB", str(tmp_path / "cli.sqlite"))

        rc = cli.main([
            "update", "--codes", "600519.SH",
            "--start", "2024-01-01", "--end", "2024-01-07",
        ])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["errors"] == 1

    def test_update_returns_zero_when_sync_succeeds(
        self, monkeypatch, tmp_path, capsys
    ):
        from stockdata import cli

        def fake_sync(cache, codes, start, end, *, adjustment_mode="qfq"):
            return {
                "start": start,
                "end": end,
                "source": "baostock",
                "adjustment_mode": adjustment_mode,
                "adjustment_version": "baostock-adjustflag-2",
                "symbols": [
                    {
                        "code": "600519.SH",
                        "status": "synced",
                        "start": start,
                        "end": end,
                        "written": 2,
                    }
                ],
                "synced": 1,
                "up_to_date": 0,
                "errors": 0,
            }

        monkeypatch.setattr("stockdata.sync.sync_symbols", fake_sync)
        monkeypatch.setenv("STOCKDATA_DB", str(tmp_path / "cli.sqlite"))

        rc = cli.main([
            "update", "--codes", "600519.SH",
            "--start", "2024-01-01", "--end", "2024-01-07",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["errors"] == 0
