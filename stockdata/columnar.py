"""列式返回组装 —— findesk drop-in 兼容格式。

顶层 {data: {time,code,open,high,low,close,volume}}，各字段等长并行数组；
多标的按 (code,time) 平铺。补齐 open；无 findesk 的双重 JSON wrapper。
"""
from __future__ import annotations

_FIELDS = ("time", "code", "open", "high", "low", "close", "volume")


def to_columnar(rows_by_code: dict) -> dict:
    """{code: [bar,...]} → {"data": {time,code,open,high,low,close,volume}}。

    bar 为内部形态 {date,open,high,low,close,volume}；date 映射为列式的 time。
    """
    cols = {k: [] for k in _FIELDS}
    for code, rows in rows_by_code.items():
        for b in rows:
            cols["time"].append(b["date"])
            cols["code"].append(code)
            cols["open"].append(b.get("open"))
            cols["high"].append(b.get("high"))
            cols["low"].append(b.get("low"))
            cols["close"].append(b.get("close"))
            cols["volume"].append(b.get("volume"))
    return {"data": cols}
