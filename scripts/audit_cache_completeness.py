"""缓存完整性审计：以 baostock 官方交易日历为基准，逐标的检查日线缓存。

检查项（默认针对 baostock 前复权主身份，可用参数切换到 tonghuashun 等其他身份）：
  1. 内部空洞：已覆盖区间 [min,max] 内缺失的交易日（停牌空洞不视为缺口，
     本脚本只报告，由人工判定是否停牌）。
  2. 尾部滞后：最新交易日之后仍未覆盖的交易日数。

用法：
    .venv/bin/python scripts/audit_cache_completeness.py [end_date] [source] [adjustment_mode]
    # 例：审计同花顺身份
    .venv/bin/python scripts/audit_cache_completeness.py 2026-08-14 tonghuashun qfq

退出码：0 = 全部完整；1 = 存在缺口。需网络（拉取交易日历）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path


def _default_db() -> Path:
    return Path(os.environ.get(
        "STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))


def _trading_calendar(start: str, end: str) -> list[str]:
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        days = []
        while rs.error_code == "0" and rs.next():
            day, is_trading = rs.get_row_data()
            if is_trading == "1":
                days.append(day)
        return days
    finally:
        bs.logout()


def main() -> int:
    end = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    source = sys.argv[2] if len(sys.argv) > 2 else "baostock"
    adj_mode = sys.argv[3] if len(sys.argv) > 3 else "qfq"
    conn = sqlite3.connect(str(_default_db()))
    conn.row_factory = sqlite3.Row
    codes = [r["code"] for r in conn.execute(
        "SELECT DISTINCT code FROM daily WHERE source=? AND adjustment_mode=? "
        "ORDER BY code", (source, adj_mode))]
    if not codes:
        print("no rows for identity", source, adj_mode)
        return 1

    cal = _trading_calendar("2015-01-01", end)
    if not cal:
        print("empty trading calendar")
        return 1
    latest = cal[-1]
    print(f"calendar: {len(cal)} trading days .. {latest}; auditing {len(codes)} codes")

    incomplete = 0
    for code in codes:
        dates = {r["date"] for r in conn.execute(
            "SELECT date FROM daily WHERE code=? AND source=? AND adjustment_mode=?",
            (code, source, adj_mode))}
        lo, hi = min(dates), max(dates)
        interior = [d for d in cal if lo <= d <= hi and d not in dates]
        behind = [d for d in cal if d > hi]
        if interior or behind:
            incomplete += 1
            print(f"  {code}: cov={lo}..{hi} interior_missing={len(interior)} "
                  f"tail_behind={len(behind)}")
            if interior:
                print(f"    interior dates: {interior[:10]}"
                      f"{' ...' if len(interior) > 10 else ''}")
    conn.close()

    if incomplete:
        print(f"INCOMPLETE: {incomplete}/{len(codes)} codes have gaps vs {latest}")
        return 1
    print(f"ALL COMPLETE: {len(codes)} codes cover the trading calendar to {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
