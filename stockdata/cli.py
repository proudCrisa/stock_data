"""兼容 CLI —— 输出 findesk 格式列式 JSON 到 stdout。

供"读 stdin 列式 JSON"的下游脚本零改造对接。
build_params 为纯逻辑（argv→params），main 负责真实 service 调用与 IO。

用法:
    stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30
    stockdata-cli quote_history --codes 600519.SH,000001.SZ --start 2024-01-01 --end 2024-06-30
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def build_params(argv: list) -> dict:
    parser = argparse.ArgumentParser(prog="stockdata-cli")
    sub = parser.add_subparsers(dest="kind", required=True)

    q = sub.add_parser("history")
    q.add_argument("--code", required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", default="")
    q.add_argument("--finalized-only", action="store_true")

    m = sub.add_parser("quote_history")
    m.add_argument("--codes", required=True, help="逗号分隔多标的")
    m.add_argument("--start", required=True)
    m.add_argument("--end", default="")

    r = sub.add_parser("realtime")
    r.add_argument("--code", required=True)

    u = sub.add_parser("update")
    codes = u.add_mutually_exclusive_group(required=True)
    codes.add_argument("--codes", help="comma-separated symbols")
    codes.add_argument("--codes-file")
    u.add_argument("--start", required=True)
    u.add_argument("--end", default="")
    u.add_argument("--adjustment-mode", choices=("qfq", "raw", "hfq"), default="qfq")

    snapshot = sub.add_parser("snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_action", required=True)
    create = snapshot_sub.add_parser("create")
    create.add_argument("--output", required=True)
    create.add_argument("--as-of", required=True)
    create.add_argument("--codes", default="")
    create.add_argument("--adjustment-mode", choices=("qfq", "raw", "hfq"), default="qfq")
    create.add_argument("--adjustment-version", default="")
    create.add_argument("--source", default="")
    verify = snapshot_sub.add_parser("verify")
    verify.add_argument("--snapshot-dir", required=True)

    readiness = sub.add_parser("execution-readiness")
    readiness.add_argument("--source", default="")
    readiness.add_argument("--adjustment-mode", default="")
    readiness.add_argument("--adjustment-version", default="")
    readiness.add_argument("--panel-file", default="")
    readiness.add_argument("--database", default="")

    capture = sub.add_parser("forward-capture")
    capture.add_argument("--database", required=True)
    capture_codes = capture.add_mutually_exclusive_group(required=True)
    capture_codes.add_argument("--codes")
    capture_codes.add_argument("--codes-file")
    capture.add_argument("--start", required=True)
    capture.add_argument("--end", default="")
    capture.add_argument("--source", choices=("baostock", "tencent"), default="baostock")
    capture.add_argument("--adjustment-version", default="")

    context_capture = sub.add_parser("forward-context-capture")
    context_capture.add_argument("--database", required=True)
    context_capture.add_argument("--date", required=True)

    corporate_action_capture = sub.add_parser("forward-corporate-actions-capture")
    corporate_action_capture.add_argument("--database", required=True)
    corporate_action_capture.add_argument("--date", required=True)

    full_readiness = sub.add_parser("full-execution-readiness")
    full_readiness.add_argument("--database", required=True)
    full_readiness.add_argument("--source", required=True)
    full_readiness.add_argument("--adjustment-mode", required=True)
    full_readiness.add_argument("--adjustment-version", required=True)
    full_readiness.add_argument("--signal-adjustment-mode", default="")
    full_readiness.add_argument("--signal-adjustment-version", default="")
    full_readiness.add_argument("--panel-file", required=True)

    provider_export = sub.add_parser("rqgm-provider-export")
    provider_export.add_argument("--bundle-file", required=True)

    jqdata_bootstrap = sub.add_parser("jqdata-bootstrap")
    jqdata_bootstrap.add_argument("--panel-file", required=True)
    jqdata_bootstrap.add_argument("--max-rows", required=True, type=int)

    args = parser.parse_args(argv)
    if args.kind == "history":
        params = {"kind": "query", "function": "history", "code": args.code,
                  "start_date": args.start, "end_date": args.end}
        if args.finalized_only:
            params["finalized_only"] = True
        return params
    if args.kind == "realtime":
        return {"kind": "realtime", "code": args.code}
    if args.kind == "update":
        return {
            "kind": "update",
            "codes": args.codes,
            "codes_file": args.codes_file,
            "start_date": args.start,
            "end_date": args.end,
            "adjustment_mode": args.adjustment_mode,
        }
    if args.kind == "snapshot":
        if args.snapshot_action == "verify":
            return {"kind": "snapshot_verify", "snapshot_dir": args.snapshot_dir}
        return {
            "kind": "snapshot_create",
            "output": args.output,
            "as_of": args.as_of,
            "codes": [c.strip() for c in args.codes.split(",") if c.strip()],
            "adjustment_mode": args.adjustment_mode,
            "adjustment_version": args.adjustment_version or None,
            "source": args.source or None,
        }
    if args.kind == "execution-readiness":
        return {
            "kind": "execution_readiness",
            "source": args.source or None,
            "adjustment_mode": args.adjustment_mode or None,
            "adjustment_version": args.adjustment_version or None,
            "panel_file": args.panel_file or None,
            "database": args.database or None,
        }
    if args.kind == "forward-capture":
        return {
            "kind": "forward_capture",
            "database": args.database,
            "codes": args.codes,
            "codes_file": args.codes_file,
            "start_date": args.start,
            "end_date": args.end,
            "source": args.source,
            "adjustment_version": args.adjustment_version or None,
        }
    if args.kind == "forward-context-capture":
        return {
            "kind": "forward_context_capture",
            "database": args.database,
            "effective_date": args.date,
        }
    if args.kind == "forward-corporate-actions-capture":
        return {
            "kind": "forward_corporate_actions_capture",
            "database": args.database,
            "observation_date": args.date,
        }
    if args.kind == "full-execution-readiness":
        return {
            "kind": "full_execution_readiness",
            "database": args.database,
            "source": args.source,
            "adjustment_mode": args.adjustment_mode,
            "adjustment_version": args.adjustment_version,
            "signal_adjustment_mode": args.signal_adjustment_mode or None,
            "signal_adjustment_version": args.signal_adjustment_version or None,
            "panel_file": args.panel_file,
        }
    if args.kind == "rqgm-provider-export":
        return {
            "kind": "rqgm_provider_export",
            "bundle_file": args.bundle_file,
        }
    if args.kind == "jqdata-bootstrap":
        return {
            "kind": "jqdata_bootstrap",
            "panel_file": args.panel_file,
            "max_rows": args.max_rows,
        }
    return {"kind": "quote_history",
            "codes": [c.strip() for c in args.codes.split(",") if c.strip()],
            "start_date": args.start, "end_date": args.end}


def main(argv=None):
    from .cache import Cache
    from .mcp_handlers import handle_ffd_query, handle_ffd_quote_history
    from .service import HistoryService

    params = build_params(sys.argv[1:] if argv is None else argv)
    db = Path(
        params.get("database")
        or os.environ.get("STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite"))
    )
    if params["kind"] == "execution_readiness":
        from .execution_readiness import check_execution_readiness, load_panel
        panel = load_panel(params["panel_file"]) if params["panel_file"] else None
        out = check_execution_readiness(
            db,
            source=params["source"],
            adjustment_mode=params["adjustment_mode"],
            adjustment_version=params["adjustment_version"],
            panel=panel,
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "full_execution_readiness":
        from .execution_readiness import load_panel
        from .full_execution_readiness import check_full_execution_readiness
        out = check_full_execution_readiness(
            db,
            source=params["source"],
            adjustment_mode=params["adjustment_mode"],
            adjustment_version=params["adjustment_version"],
            signal_adjustment_mode=params["signal_adjustment_mode"],
            signal_adjustment_version=params["signal_adjustment_version"],
            panel=load_panel(params["panel_file"]),
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "rqgm_provider_export":
        from .provider_export import export_verified_provider_receipt
        out = export_verified_provider_receipt(params["bundle_file"])
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "jqdata_bootstrap":
        from .execution_readiness import load_panel
        from .jqdata_bootstrap import (
            authenticate,
            build_bootstrap_artifact,
            close_session,
        )

        try:
            import jqdatasdk
        except ImportError:
            raise RuntimeError(
                "JQData support is not installed; install stockdata[jqdata]"
            ) from None
        account = secret = ""
        auth_attempted = False
        try:
            account = getpass.getpass("JQData account: ")
            secret = getpass.getpass("JQData secret: ")
            auth_attempted = True
            authenticate(jqdatasdk, account, secret)
            out = build_bootstrap_artifact(
                jqdatasdk,
                panel=load_panel(params["panel_file"]),
                observed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
                    timespec="seconds"
                ),
                max_rows=params["max_rows"],
            )
        finally:
            if auth_attempted:
                close_session(jqdatasdk)
            account = secret = ""
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    cache = Cache(db)
    svc = HistoryService(cache)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    if params["kind"] == "forward_corporate_actions_capture":
        from .forward_corporate_actions import capture_forward_corporate_actions
        out = capture_forward_corporate_actions(cache, params["observation_date"])
    elif params["kind"] == "forward_context_capture":
        from .forward_context import capture_forward_context
        out = capture_forward_context(cache, params["effective_date"])
    elif params["kind"] == "forward_capture":
        from .forward_capture import capture_forward_evidence
        if params["codes_file"]:
            codes = [
                line.strip() for line in Path(params["codes_file"]).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        else:
            codes = [c.strip() for c in params["codes"].split(",") if c.strip()]
        out = capture_forward_evidence(
            cache,
            codes,
            params["start_date"],
            params["end_date"] or None,
            source=params["source"],
            adjustment_version=params["adjustment_version"],
        )
    elif params["kind"] == "query":
        if params.get("finalized_only"):
            from .columnar import to_columnar
            from .ticker import normalize
            end = params["end_date"] or today
            rows = svc.get_history(
                params["code"], params["start_date"], end, today=today,
                finalized_only=True,
            )
            out = to_columnar({normalize(params["code"]): rows})
        else:
            out = handle_ffd_query(params, svc, today=today)
    elif params["kind"] == "realtime":
        from . import api
        out = api.get_realtime(params["code"])
    elif params["kind"] == "update":
        from .sync import default_final_date, sync_symbols
        if params["codes_file"]:
            codes = [
                line.strip() for line in Path(params["codes_file"]).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        else:
            codes = [c.strip() for c in params["codes"].split(",") if c.strip()]
        out = sync_symbols(
            cache,
            codes,
            params["start_date"],
            params["end_date"] or default_final_date(),
            adjustment_mode=params["adjustment_mode"],
        )
    elif params["kind"] == "snapshot_create":
        from .snapshot import create_snapshot
        out = create_snapshot(
            cache,
            params["output"],
            params["as_of"],
            codes=params["codes"] or None,
            source=params["source"],
            adjustment_mode=params["adjustment_mode"],
            adjustment_version=params["adjustment_version"],
        )
    elif params["kind"] == "snapshot_verify":
        from .snapshot import verify_snapshot
        out = verify_snapshot(params["snapshot_dir"])
    else:
        out = handle_ffd_quote_history(params, svc, today=today)
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
