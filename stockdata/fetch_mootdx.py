"""mootdx 拉取层 —— 近端补齐/兜底源（通达信 TCP，不复权原始价）。

⚠️ mootdx 返回【不复权】原始价，不能与 baostock 前复权序列混入同一历史序列；
仅用于主源失败兜底、或补当日近端 bar。复权口径由 service 层负责隔离。

含 tdx_client()：规避 mootdx 0.11.x BESTIP.HQ 空串 bug（移植自 a-stock-data 蓝本）。
"""
from __future__ import annotations

import socket

from .ticker import to_mootdx

# 实测可用备选服务器（TCP 7709），按蓝本 2026-06 验证列表
_TDX_SERVERS = [
    ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
    ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
    ("123.60.70.228", 7709), ("124.71.9.153", 7709), ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


def _probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def tdx_client(market: str = "std"):
    """创建 mootdx 客户端，规避 0.11.x BESTIP 空串 bug；顺序兜底。"""
    from mootdx.quotes import Quotes

    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            return Quotes.factory(market=market, server=(ip, port))
    try:
        return Quotes.factory(market=market, bestip=True)
    except Exception:
        pass
    try:
        return Quotes.factory(market=market)
    except Exception as e:
        raise RuntimeError(
            "所有 mootdx 服务器均不可达（海外网络 TCP 7709 常全超时）。"
            f"原始错误: {e}"
        )


def _parse_bars_df(df) -> list[dict]:
    """mootdx DataFrame → 内部 bar 列表（date 升序，OHLCV）。"""
    if df is None or len(df) == 0:
        return []
    out = []
    for idx, row in df.iterrows():
        # date 优先取 datetime 列，退回 index
        raw = row["datetime"] if "datetime" in row and row["datetime"] else idx
        date_str = raw.strftime("%Y-%m-%d") if hasattr(raw, "strftime") else str(raw)[:10]
        out.append({
            "date": date_str,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", row.get("vol", 0))),
        })
    out.sort(key=lambda b: b["date"])
    return out


def fetch_mootdx(code: str, offset: int = 100) -> list[dict]:
    """拉取最近 offset 根日 K（不复权）。需网络。失败抛异常。"""
    client = tdx_client()
    symbol = to_mootdx(code)
    df = client.bars(symbol=symbol, frequency=9, offset=offset)
    return _parse_bars_df(df)
