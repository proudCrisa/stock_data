"""兼容 CLI —— 输出 findesk 格式列式 JSON 到 stdout。

供"读 stdin 列式 JSON"的下游脚本零改造对接。
build_params 为纯逻辑（argv→params），main 负责真实 service 调用与 IO。

用法:
    stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30
    stockdata-cli quote_history --codes 600519.SH,000001.SZ --start 2024-01-01 --end 2024-06-30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


def build_params(argv: list) -> dict:
    parser = argparse.ArgumentParser(prog="stockdata-cli")
    sub = parser.add_subparsers(dest="kind", required=True)

    q = sub.add_parser("history")
    q.add_argument("--code", required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", default="")

    m = sub.add_parser("quote_history")
    m.add_argument("--codes", required=True, help="逗号分隔多标的")
    m.add_argument("--start", required=True)
    m.add_argument("--end", default="")

    r = sub.add_parser("realtime")
    r.add_argument("--code", required=True)

    args = parser.parse_args(argv)
    if args.kind == "history":
        return {"kind": "query", "function": "history", "code": args.code,
                "start_date": args.start, "end_date": args.end}
    if args.kind == "realtime":
        return {"kind": "realtime", "code": args.code}
    return {"kind": "quote_history",
            "codes": [c.strip() for c in args.codes.split(",") if c.strip()],
            "start_date": args.start, "end_date": args.end}


def main(argv=None):
    from .cache import Cache
    from .service import HistoryService
    from .mcp_handlers import handle_ffd_query, handle_ffd_quote_history

    params = build_params(sys.argv[1:] if argv is None else argv)
    db = Path(os.environ.get("STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))
    svc = HistoryService(Cache(db))
    today = date.today().isoformat()

    if params["kind"] == "query":
        out = handle_ffd_query(params, svc, today=today)
    elif params["kind"] == "realtime":
        from . import api
        out = api.get_realtime(params["code"])
    else:
        out = handle_ffd_quote_history(params, svc, today=today)
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
