"""MCP 工具处理逻辑（纯逻辑，与 FastMCP 注册分离）。

handle_ffd_query        —— 对应 findesk ffd_query(function='history', ...)
handle_ffd_quote_history —— 对应 findesk ffd_quote_history(codes, ...)

均解析入参 → HistoryService.get_history → to_columnar 列式组装。
"""
from __future__ import annotations

from .columnar import to_columnar
from .service import HistoryService


def handle_ffd_query(params: dict, service: HistoryService, today: str) -> dict:
    """ffd_query：目前仅支持 function='history'（super-trader-rqgm 实际所用）。"""
    fn = params.get("function")
    if fn != "history":
        raise ValueError(f"不支持的 function: {fn!r}（本地版仅复刻 history）")
    code = params.get("code")
    if not code:
        raise ValueError("缺少 code")
    start = params.get("start_date")
    end = params.get("end_date") or today
    rows = service.get_history(code, start, end, today=today)
    return to_columnar({code: rows})


def handle_ffd_quote_history(params: dict, service: HistoryService, today: str) -> dict:
    """ffd_quote_history：多标的历史，平铺进同一组列式数组。"""
    codes = params.get("codes")
    if not codes:
        raise ValueError("缺少 codes")
    if isinstance(codes, str):
        codes = [codes]
    start = params.get("start_date")
    end = params.get("end_date") or today
    rows_by_code = {c: service.get_history(c, start, end, today=today) for c in codes}
    return to_columnar(rows_by_code)
