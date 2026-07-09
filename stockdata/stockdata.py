"""stockdata —— 可靠 A 股行情数据接口（供 super-trader-rqgm 等项目调用）。

经 20 股实网多源交叉验证：日线主源 baostock(前复权) + 通达信(mootdx/pytdx) 交叉 +
腾讯实时；收盘价/OHLC 多源一致，与实时价 100% 吻合。开箱即用，无需 API key。
数据落地本地 SQLite（离线可读、增量更新）。

对外函数：
  get_daily(code, start, end)        历史日线（前复权），列式 {data:{time,code,open,high,low,close,volume}}
  get_daily_df(code, start, end)     历史日线 → pandas DataFrame
  get_realtime(code)                 实时价（腾讯今日盘中 bar）
  cross_check(code, start, end)      多源交叉验证（共识 + 一致性 + 离群源 + 实时对照）

代码格式：接受 600519 / 600519.SH / sh600519 等，内部自动归一化。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from .cache import Cache
from .service import HistoryService
from .ticker import normalize, to_mootdx
from .columnar import to_columnar
from .consensus import cross_validate


def _default_db() -> Path:
    return Path(os.environ.get("STOCKDATA_DB",
                               str(Path.home() / ".stockdata" / "cache.sqlite")))


_service = HistoryService(Cache(_default_db()))


def _today() -> str:
    return date.today().isoformat()


def get_daily(code: str, start_date: str, end_date: str = "") -> dict:
    """历史日线（前复权）。返回列式 {data:{time,code,open,high,low,close,volume}}。"""
    end = end_date or _today()
    rows = _service.get_history(code, start_date, end, today=_today())
    return to_columnar({normalize(code): rows})


def get_daily_df(code: str, start_date: str, end_date: str = ""):
    """历史日线 → pandas DataFrame（date/open/high/low/close/volume）。"""
    import pandas as pd
    end = end_date or _today()
    rows = _service.get_history(code, start_date, end, today=_today())
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def get_realtime(code: str):
    """实时价（腾讯今日盘中 bar）。返回 {date,open,high,low,close,volume} 或 None。"""
    from .fetch_tencent import fetch_today_bar
    return fetch_today_bar(normalize(code))


def cross_check(code: str, start_date: str, end_date: str = "", tol: float = 0.005) -> dict:
    """多源(baostock 前复权 + 通达信) 交叉验证 + 腾讯实时对照。

    返回 {date: DayConsensus(consensus, n_sources, agreement, outliers, realtime_match)}。
    交易决策前调用确认：agreement=True（多源一致）且最新日 realtime_match=True（与实时一致）。
    """
    code = normalize(code)
    end = end_date or _today()
    series = {}
    try:
        from .fetch_baostock import fetch_baostock
        series["baostock"] = {b["date"]: b["close"]
                              for b in fetch_baostock(code, start_date, end)}
    except Exception as e:
        series["baostock"] = f"ERR {type(e).__name__}"
    try:
        series["tdx"] = {b["date"]: b["close"] for b in _fetch_tdx(code)}
    except Exception as e:
        series["tdx"] = f"ERR {type(e).__name__}"
    rt = get_realtime(code)
    realtime = {"date": rt["date"], "price": rt["close"]} if rt else None
    return cross_validate(series, tol=tol, realtime=realtime)


def _fetch_tdx(code: str) -> list:
    """通达信取数：指数用 c.index()，个股用 c.bars()（避免指数取数乱码陷阱）。"""
    from .fetch_mootdx import tdx_client, _parse_bars_df
    client = tdx_client()
    symbol = to_mootdx(code)
    digits, market = normalize(code).split(".")
    is_idx = digits.startswith("399") or (market == "SH" and digits.startswith("000"))
    df = client.index(symbol=symbol, frequency=9, offset=100) if is_idx \
        else client.bars(symbol=symbol, frequency=9, offset=100)
    return _parse_bars_df(df)
