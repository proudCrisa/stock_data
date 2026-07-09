"""FastMCP server —— 暴露 findesk 同名工具 ffd_query / ffd_quote_history。

薄壳：注入真实 HistoryService（baostock 主 + mootdx 补 + 腾讯今日）与当日日期，
转调 mcp_handlers 的纯逻辑。super-trader-rqgm 把 FFD MCP 指向本 server 即可 drop-in。

运行:
    python -m stockdata.server            # stdio MCP server
缓存 DB 默认 ~/.stockdata/cache.sqlite，可用环境变量 STOCKDATA_DB 覆盖。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .cache import Cache
from .service import HistoryService
from .mcp_handlers import handle_ffd_query, handle_ffd_quote_history


def _default_db() -> Path:
    return Path(os.environ.get("STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))


mcp = FastMCP("stockdata")
_service = HistoryService(Cache(_default_db()))


def _today() -> str:
    return date.today().isoformat()


@mcp.tool()
def ffd_query(function: str, code: str = "", start_date: str = "",
              end_date: str = "") -> dict:
    """findesk 兼容：历史日线查询。function 目前支持 'history'。

    返回列式并行数组 {data:{time,code,open,high,low,close,volume}}（前复权）。
    """
    return handle_ffd_query(
        {"function": function, "code": code,
         "start_date": start_date, "end_date": end_date},
        _service, today=_today())


@mcp.tool()
def ffd_quote_history(codes, start_date: str = "", end_date: str = "") -> dict:
    """findesk 兼容：多标的历史日线。codes 为列表或单个字符串。

    返回列式并行数组，多标的按 (code,time) 平铺。
    """
    return handle_ffd_quote_history(
        {"codes": codes, "start_date": start_date, "end_date": end_date},
        _service, today=_today())


def main():
    mcp.run()


if __name__ == "__main__":
    main()
