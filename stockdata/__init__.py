"""stockdata —— 可靠 A 股行情数据接口。

经 20 股实网多源交叉验证：日线主源 baostock(前复权) + 通达信(mootdx/pytdx) 交叉 +
腾讯实时；收盘价/OHLC 多源一致，与实时价 100% 吻合。开箱即用，无需 API key。

    from stockdata import get_daily, get_daily_df, get_realtime, cross_check

    data = get_daily("600519.SH", "2024-01-01", "2024-06-30")
    df   = get_daily_df("600519.SH", "2024-01-01", "2024-06-30")
    q    = get_realtime("600519.SH")
    xv   = cross_check("600519.SH", "2026-06-01", "2026-07-08")
"""
from .stockdata import get_daily, get_daily_df, get_realtime, cross_check

__version__ = "1.0.0"
__all__ = ["get_daily", "get_daily_df", "get_realtime", "cross_check"]
