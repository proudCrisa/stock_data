#!/usr/bin/env python3
"""端到端验证 stockdata 纯净 API（供 super-trader-rqgm 调用）。真实联网。"""
import stockdata as sd

print("=== 1) get_daily 历史日线（列式）===")
d = sd.get_daily("600519.SH", "2026-07-01", "2026-07-08")["data"]
print("字段:", sorted(d.keys()))
print("行数:", len(d["time"]), "| 末日:", d["time"][-1], "| close:", d["close"][-1], "| open:", d["open"][-1])

print("\n=== 2) get_daily_df DataFrame ===")
df = sd.get_daily_df("000001.SZ", "2026-07-01", "2026-07-08")
print(df.tail(3).to_string())

print("\n=== 3) get_realtime 实时价 ===")
print(sd.get_realtime("600519.SH"))

print("\n=== 4) cross_check 多源交叉验证 ===")
xv = sd.cross_check("600519.SH", "2026-07-04", "2026-07-08")
for dt in sorted(xv)[-3:]:
    r = xv[dt]
    print(f"  {dt}: consensus={r.consensus} n_sources={r.n_sources} agreement={r.agreement} realtime_match={r.realtime_match}")
print("\nAPI 全部可用")
