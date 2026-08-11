"""baostock 拉取层 —— 历史日线主源（前复权）。

adjustflag: 1=后复权, 2=前复权, 3=不复权。历史区间用 2（与 findesk 口径可解释对齐）。
纯解析 _parse_rows 与网络 fetch_baostock 分离，便于无网络单测。
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone

from .finalization import latest_finalized_date

from .ticker import to_baostock

_NUMERIC = ("open", "high", "low", "close", "volume")
_ADJUST_FLAGS = {"hfq": "1", "qfq": "2", "raw": "3"}
_ADJUST_MODES = {value: key for key, value in _ADJUST_FLAGS.items()}


class CapturedBars(list[dict]):
    """Parsed bars with the exact provider request/response capture metadata."""

    def __init__(self, bars: list[dict], capture_receipt: dict):
        super().__init__(bars)
        self.capture_receipt = capture_receipt


@contextlib.contextmanager
def _suppress_stdout():
    """抑制 baostock login/logout 向 stdout 的打印，保护 CLI 的 JSON 输出契约。"""
    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stdout(devnull):
            yield


def _to_float(s: str) -> float:
    s = (s or "").strip()
    return float(s) if s else 0.0


def _parse_rows(fields: str, rows: list) -> list[dict]:
    """baostock 行（list of list）+ 列名字符串 → 内部 bar 列表。"""
    cols = [c.strip() for c in fields.split(",")]
    out = []
    for r in rows:
        rec = dict(zip(cols, r))
        bar = {"date": rec["date"]}
        for k in _NUMERIC:
            bar[k] = _to_float(rec.get(k, ""))
        out.append(bar)
    return out


def fetch_baostock(
    code: str,
    start: str,
    end: str,
    adjustflag: str | None = None,
    adjustment_mode: str = "qfq",
) -> list[dict]:
    """拉取 [start,end] 前复权日线。需网络 + baostock 登录。

    返回内部 bar 列表（date 升序，date/open/high/low/close/volume）。
    失败抛异常，交由上层 service 决定兜底。
    """
    import baostock as bs

    if adjustflag is None:
        try:
            adjustflag = _ADJUST_FLAGS[adjustment_mode]
        except KeyError as exc:
            raise ValueError(f"unsupported adjustment_mode: {adjustment_mode}") from exc
    elif adjustflag not in _ADJUST_MODES:
        raise ValueError(f"unsupported adjustflag: {adjustflag}")
    else:
        adjustment_mode = _ADJUST_MODES[adjustflag]

    bs_code = to_baostock(code)
    with _suppress_stdout():
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=adjustflag,
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock 查询失败({bs_code}): {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = f"baostock-adjustflag-{adjustflag}"
    fields = "date,open,high,low,close,volume"
    bars = _parse_rows(fields, rows)
    receipt = {
        "observed_at": retrieved_at,
        "source": "baostock",
        "request": {
            "method": "query_history_k_data_plus",
            "code": bs_code,
            "fields": fields,
            "start_date": start,
            "end_date": end,
            "frequency": "d",
            "adjustflag": adjustflag,
        },
        # Preserve raw provider strings before numeric normalization changes them.
        "response": {"fields": fields, "rows": rows},
    }
    for bar in bars:
        bar.update({
            "source": "baostock",
            "adjustment_mode": adjustment_mode,
            "adjustment_version": version,
            "retrieved_at": retrieved_at,
            "is_final": bar["date"] <= latest_finalized_date(),
            "_capture_receipt": receipt,
        })
    return CapturedBars(bars, receipt)
