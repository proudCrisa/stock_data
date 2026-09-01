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


class _StoreOnce(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may only be provided once")
        setattr(namespace, self.dest, values)


def build_params(argv: list) -> dict:
    parser = argparse.ArgumentParser(prog="stockdata-cli", allow_abbrev=False)
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

    cal = sub.add_parser("update-calendar")
    cal.add_argument("--database", default="")
    cal.add_argument("--start", required=True)
    cal.add_argument("--end", required=True)

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

    provider_materialize = sub.add_parser("rqgm-provider-materialize")
    provider_materialize.add_argument("--output-dir", required=True)
    provider_materialize.add_argument("--database", required=True)
    provider_materialize.add_argument("--registration-file", required=True)
    provider_materialize.add_argument(
        "--snapshot-staging-directory", required=True
    )
    provider_materialize.add_argument("--panel-file", required=True)
    provider_materialize.add_argument("--source-receipt", action="append", required=True)
    provider_materialize.add_argument("--execution-adjustment-file", required=True)
    provider_materialize.add_argument("--signal-adjustment-file", required=True)
    provider_materialize.add_argument("--component-file", action="append", required=True)
    provider_materialize.add_argument("--component-authority", action="append")
    provider_materialize.add_argument("--source", required=True)

    registered_provider_materialize = sub.add_parser(
        "rqgm-provider-materialize-registered"
    )
    registered_provider_materialize.add_argument("--registration-file", required=True)
    registered_provider_materialize.add_argument("--database", required=True)
    registered_provider_materialize.add_argument("--output-dir", required=True)

    research_replay = sub.add_parser(
        "rqgm-provider-research-replay", allow_abbrev=False
    )
    research_replay.add_argument("--bundle-file", required=True, action=_StoreOnce)
    research_replay.add_argument(
        "--policy-request-file", required=True, action=_StoreOnce
    )

    future_prepare = sub.add_parser("future-panel-prepare")
    future_prepare.add_argument("--database", required=True)
    future_prepare.add_argument("--panel-file", required=True)

    local_prerequisites = sub.add_parser("future-panel-local-prerequisites")
    local_prerequisites.add_argument("--panel-file", required=True)
    local_prerequisites.add_argument("--output-dir", required=True)
    local_prerequisites.add_argument("--calendar-facts-file", required=True)
    local_prerequisites.add_argument("--market-rules-facts-file", required=True)

    future_registration = sub.add_parser("future-panel-register")
    future_registration.add_argument("--output", required=True)
    future_registration.add_argument("--database", required=True)
    future_registration.add_argument("--panel-file", required=True)
    future_registration.add_argument("--source-receipt", action="append", required=True)
    future_registration.add_argument("--calendar-file", required=True)
    future_registration.add_argument("--calendar-authority")
    future_registration.add_argument("--market-rules-file", required=True)
    future_registration.add_argument("--market-rules-authority")
    future_registration.add_argument(
        "--authority-mode",
        choices=("signed", "trusted_local_mechanical"),
        default="signed",
    )

    registered_capture = sub.add_parser("registered-panel-capture")
    registered_capture.add_argument("--registration-file", required=True)
    registered_capture.add_argument("--database", required=True)
    registered_capture.add_argument("--date", required=True)
    registered_capture.add_argument("--phase", choices=("pre_open", "post_close"), required=True)

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
    if args.kind == "update-calendar":
        return {
            "kind": "update_calendar",
            "database": args.database or None,
            "start_date": args.start,
            "end_date": args.end,
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
    if args.kind == "rqgm-provider-materialize":
        component_files = {}
        for value in args.component_file:
            component, separator, path = value.partition("=")
            if not separator or not component or not path or component in component_files:
                parser.error("--component-file must be COMPONENT=PATH and each component appears once")
            component_files[component] = path
        component_authority_files = {}
        for value in args.component_authority or []:
            component, separator, path = value.partition("=")
            if not separator or not component or not path or component in component_authority_files:
                parser.error("--component-authority COMPONENT=PATH must be unique")
            component_authority_files[component] = path
        return {
            "kind": "rqgm_provider_materialize",
            "output_dir": args.output_dir,
            "database": args.database,
            "registration_file": args.registration_file,
            "snapshot_staging_directory": args.snapshot_staging_directory,
            "panel_file": args.panel_file,
            "source_receipts": args.source_receipt,
            "execution_adjustment_file": args.execution_adjustment_file,
            "signal_adjustment_file": args.signal_adjustment_file,
            "component_files": component_files,
            "component_authority_files": component_authority_files,
            "source": args.source,
        }
    if args.kind == "rqgm-provider-materialize-registered":
        return {
            "kind": "rqgm_provider_materialize_registered",
            "registration_file": args.registration_file,
            "database": args.database,
            "output_dir": args.output_dir,
        }
    if args.kind == "rqgm-provider-research-replay":
        return {
            "kind": "rqgm_provider_research_replay",
            "bundle_file": args.bundle_file,
            "policy_request_file": args.policy_request_file,
        }
    if args.kind == "future-panel-prepare":
        return {
            "kind": "future_panel_prepare",
            "database_file": args.database,
            "panel_file": args.panel_file,
        }
    if args.kind == "future-panel-local-prerequisites":
        return {
            "kind": "future_panel_local_prerequisites",
            "panel_file": args.panel_file,
            "output_dir": args.output_dir,
            "calendar_facts_file": args.calendar_facts_file,
            "market_rules_facts_file": args.market_rules_facts_file,
        }
    if args.kind == "future-panel-register":
        if args.authority_mode == "signed" and (
            args.calendar_authority is None or args.market_rules_authority is None
        ):
            parser.error("signed future-panel-register requires both authority files")
        if args.authority_mode == "trusted_local_mechanical" and (
            args.calendar_authority is not None or args.market_rules_authority is not None
        ):
            parser.error("trusted_local_mechanical does not accept authority files")
        params = {
            "kind": "future_panel_register",
            "output_file": args.output,
            "database_file": args.database,
            "panel_file": args.panel_file,
            "source_receipt_files": args.source_receipt,
            "calendar_file": args.calendar_file,
            "calendar_authority_file": args.calendar_authority,
            "market_rules_file": args.market_rules_file,
            "market_rules_authority_file": args.market_rules_authority,
        }
        if args.authority_mode != "signed":
            params["authority_mode"] = args.authority_mode
        return params
    if args.kind == "registered-panel-capture":
        return {
            "kind": "registered_panel_capture",
            "registration_file": args.registration_file,
            "database": args.database,
            "effective_date": args.date,
            "phase": args.phase,
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


def _collector_child_writer_token(
    params: dict, database: Path, argv: list[str]
) -> object | None:
    """Open child-only authority before a collector writer can construct Cache."""

    from .collector_continuity import (
        classify_collector_child_environment,
        database_has_collector_genesis,
        open_collector_child_writer_authority,
    )

    is_child = classify_collector_child_environment()
    if is_child:
        # This branch must precede every SQLite marker or Cache operation.
        return open_collector_child_writer_authority(
            argv=(os.path.realpath(sys.executable), "-m", "stockdata.cli", *argv)
        )
    if params["kind"] not in {
        "forward_capture",
        "forward_context_capture",
        "forward_corporate_actions_capture",
    }:
        return None
    if not database_has_collector_genesis(database):
        return None
    return open_collector_child_writer_authority(
        argv=(os.path.realpath(sys.executable), "-m", "stockdata.cli", *argv)
    )


def _run_cache_command(params: dict, database: Path, writer_token: object | None) -> dict:
    from .cache import Cache
    from .mcp_handlers import handle_ffd_query, handle_ffd_quote_history
    from .service import HistoryService

    cache = Cache(database, writer_token=writer_token)
    svc = HistoryService(cache)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if params["kind"] == "forward_corporate_actions_capture":
        from .forward_corporate_actions import capture_forward_corporate_actions

        return capture_forward_corporate_actions(cache, params["observation_date"])
    if params["kind"] == "forward_context_capture":
        from .forward_context import capture_forward_context

        return capture_forward_context(cache, params["effective_date"])
    if params["kind"] == "forward_capture":
        from .forward_capture import capture_forward_evidence

        if params["codes_file"]:
            codes = [
                line.strip() for line in Path(params["codes_file"]).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        else:
            codes = [c.strip() for c in params["codes"].split(",") if c.strip()]
        return capture_forward_evidence(
            cache,
            codes,
            params["start_date"],
            params["end_date"] or None,
            source=params["source"],
            adjustment_version=params["adjustment_version"],
        )
    if params["kind"] == "query":
        if params.get("finalized_only"):
            from .columnar import to_columnar
            from .ticker import normalize

            end = params["end_date"] or today
            rows = svc.get_history(
                params["code"], params["start_date"], end, today=today,
                finalized_only=True,
            )
            return to_columnar({normalize(params["code"]): rows})
        return handle_ffd_query(params, svc, today=today)
    if params["kind"] == "realtime":
        from . import api

        return api.get_realtime(params["code"])
    if params["kind"] == "update":
        from .finalization import latest_finalized_date
        from .sync import default_final_date, sync_symbols

        if params["codes_file"]:
            codes = [
                line.strip() for line in Path(params["codes_file"]).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        else:
            codes = [c.strip() for c in params["codes"].split(",") if c.strip()]
        calendar = cache.trading_calendar
        end = params["end_date"] or (
            latest_finalized_date(calendar=calendar)
            if calendar.has_data()
            else default_final_date()
        )
        return sync_symbols(
            cache,
            codes,
            params["start_date"],
            end,
            adjustment_mode=params["adjustment_mode"],
        )
    if params["kind"] == "snapshot_create":
        from .snapshot import create_snapshot

        return create_snapshot(
            cache,
            params["output"],
            params["as_of"],
            codes=params["codes"] or None,
            source=params["source"],
            adjustment_mode=params["adjustment_mode"],
            adjustment_version=params["adjustment_version"],
        )
    if params["kind"] == "snapshot_verify":
        from .snapshot import verify_snapshot

        return verify_snapshot(params["snapshot_dir"])
    return handle_ffd_quote_history(params, svc, today=today)


def main(argv=None):

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    params = build_params(raw_argv)
    db = Path(
        params.get("database")
        or os.environ.get("STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite"))
    )
    from .collector_continuity import (
        CollectorContinuityError,
        classify_collector_child_environment,
    )

    if classify_collector_child_environment():
        if params["kind"] not in {
            "forward_capture",
            "forward_context_capture",
            "forward_corporate_actions_capture",
        }:
            raise CollectorContinuityError("collector child environment does not authorize this command")
        writer_token = _collector_child_writer_token(params, db, raw_argv)
        try:
            out = _run_cache_command(params, db, writer_token)
            json.dump(out, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0
        finally:
            if writer_token is not None:
                from .collector_continuity import close_collector_writer_authority

                close_collector_writer_authority(writer_token)
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
    if params["kind"] == "update_calendar":
        from .cache import Cache

        cache = Cache(db)
        try:
            rows = cache.refresh_trading_calendar(
                params["start_date"], params["end_date"]
            )
            json.dump(
                {"refreshed": True, "rows": rows, "database": str(cache.path)},
                sys.stdout,
                ensure_ascii=False,
            )
            sys.stdout.write("\n")
        finally:
            cache.close()
        return 0
    if params["kind"] == "rqgm_provider_export":
        from .provider_export import export_verified_provider_receipt
        out = export_verified_provider_receipt(params["bundle_file"])
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "rqgm_provider_materialize":
        from .provider_materializer import materialize_provider_bundle

        out = materialize_provider_bundle(
            output_dir=params["output_dir"],
            database_file=params["database"],
            registration_file=params["registration_file"],
            snapshot_staging_directory=params["snapshot_staging_directory"],
            panel_file=params["panel_file"],
            source_receipt_files=params["source_receipts"],
            execution_adjustment_file=params["execution_adjustment_file"],
            signal_adjustment_file=params["signal_adjustment_file"],
            component_files=params["component_files"],
            component_authority_files=params["component_authority_files"],
            source=params["source"],
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "rqgm_provider_materialize_registered":
        from .provider_materializer import materialize_registered_provider_bundle

        bundle_file = materialize_registered_provider_bundle(
            registration_file=params["registration_file"],
            database=params["database"],
            output_dir=params["output_dir"],
        )
        json.dump({"bundle_file": str(bundle_file)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "rqgm_provider_research_replay":
        try:
            request_raw = Path(params["policy_request_file"]).read_bytes()
            request = json.loads(request_raw.decode("ascii"))
            canonical_request = json.dumps(
                request,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("research replay policy request file is invalid") from exc
        if request_raw != canonical_request:
            raise ValueError("research replay policy request file is not canonical")
        from .provider_export import run_trusted_local_research_replay_bridge

        out = run_trusted_local_research_replay_bridge(
            params["bundle_file"], policy_request=request
        )
        sys.stdout.write(
            json.dumps(
                out,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        return 0
    if params["kind"] == "future_panel_prepare":
        from .future_panel_registration import prepare_future_collector_database

        out = prepare_future_collector_database(
            database_file=params["database_file"],
            panel_file=params["panel_file"],
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "future_panel_local_prerequisites":
        from .trusted_local_prerequisites import materialize_trusted_local_prerequisites

        out = materialize_trusted_local_prerequisites(
            panel_file=params["panel_file"],
            output_dir=params["output_dir"],
            calendar_facts_file=params["calendar_facts_file"],
            market_rules_facts_file=params["market_rules_facts_file"],
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "future_panel_register":
        from .future_panel_registration import register_future_panel

        out = register_future_panel(
            output_file=params["output_file"],
            database_file=params["database_file"],
            panel_file=params["panel_file"],
            source_receipt_files=params["source_receipt_files"],
            calendar_file=params["calendar_file"],
            calendar_authority_file=params["calendar_authority_file"],
            market_rules_file=params["market_rules_file"],
            market_rules_authority_file=params["market_rules_authority_file"],
            authority_mode=params.get("authority_mode", "signed"),
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if params["kind"] == "registered_panel_capture":
        from .registered_panel_capture import capture_registered_panel

        out = capture_registered_panel(
            params["registration_file"],
            database=params["database"],
            effective_date=params["effective_date"],
            phase=params["phase"],
        )
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        if isinstance(out, list) and any(
            item.get("terminal_event_type") != "ATTEMPT_COMPLETED"
            for item in out
        ):
            return 1
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
    writer_token = _collector_child_writer_token(params, db, raw_argv)
    try:
        out = _run_cache_command(params, db, writer_token)
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        # 写入型命令（update / forward-capture 等）结果中 errors>0 时返回非零，
        # 让 launchd/cron/CI 能感知失败；读取型命令不受影响。
        if (
            isinstance(out, dict)
            and params["kind"]
            in {
                "update",
                "forward_capture",
                "forward_context_capture",
                "forward_corporate_actions_capture",
                "registered_panel_capture",
            }
            and out.get("errors", 0) > 0
        ):
            return 1
        return 0
    finally:
        if writer_token is not None:
            from .collector_continuity import close_collector_writer_authority

            close_collector_writer_authority(writer_token)


if __name__ == "__main__":
    sys.exit(main())
