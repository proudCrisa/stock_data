"""腾讯实时今日 bar 拉取层（qt.gtimg.cn，GBK，免 key）。

产出当日 bar {date, open, high, low, close, volume}，供 service 在 end_date 含当日时
覆盖/追加最后一根。零成交/零价为占位（停牌），返回 None（fail-closed，绝不返回假 bar）。

腾讯字段: [1]名称 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [30]时间戳 [33]最高 [34]最低
"""
from __future__ import annotations

import urllib.request

from .ticker import to_tencent

_TENCENT = "https://qt.gtimg.cn/q={sym}"


def parse_tencent_bar(raw: str, code: str):
    """解析腾讯 v_..="a~b~.." 行 → 今日 bar；占位/异常返回 None。"""
    try:
        inner = raw.split('"', 1)[1].rsplit('"', 1)[0]
        parts = inner.split("~")
        if len(parts) < 35 or not parts[3]:
            return None
        price = float(parts[3])
        volume = float(parts[6] or 0)
        if price <= 0 or volume <= 0:  # 停牌/无成交占位，拒绝
            return None
        ts = parts[30]
        date_str = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
        return {
            "date": date_str,
            "open": float(parts[5] or price),
            "high": float(parts[33] or price),
            "low": float(parts[34] or price),
            "close": price,
            "volume": volume,
        }
    except (IndexError, ValueError):
        return None


def _http_get(url: str) -> str:
    """GBK 文本抓取；隔离以便测试与网络分离。"""
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    return urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")


def fetch_today_bar(code: str):
    """当日 bar；无成交/失败返回 None（fail-closed，从不抛异常）。"""
    sym = to_tencent(code)
    try:
        return parse_tencent_bar(_http_get(_TENCENT.format(sym=sym)), code)
    except Exception:
        return None
