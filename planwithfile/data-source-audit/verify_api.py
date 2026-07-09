#!/usr/bin/env python3
"""端到端验证 stockdata.api 的对外函数（供 super-trader-rqgm 调用）。"""
from stockdata import api

print("=== 1) ffd_query findesk兼容 (600519 历史) ===")
d = api.ffd_query(function="history", code="600519.SH",
                  start_date="2026-07-01", end_date="2026-07-08")["data"]
print("字段:", sorted(d.keys()))
print("行数:", len(d["time"]), "| 末日:", d["time"][-1], "| close:", d["close"][-1],
      "| open:", d["open"][-1])

print("\n=== 2) get_daily_df (DataFrame) ===")
df = api.get_daily_df("000001.SZ", "2026-07-01", "2026-07-08")
print(df.tail(3).to_string())

print("\n=== 3) get_realtime 实时价 ===")
print(api.get_realtime("600519.SH"))

print("\n=== 4) cross_check 多源交叉验证 ===")
xv = api.cross_check("600519.SH", "2026-07-04", "2026-07-08")
for dt in sorted(xv)[-3:]:
    r = xv[dt]
    print(f"  {dt}: consensus={r.consensus} n_sources={r.n_sources} "
          f"agreement={r.agreement} realtime_match={r.realtime_match}")

print("\n=== 5) ffd_quote_history 多标的 ===")
q = api.ffd_quote_history(codes=["600519.SH", "000001.SZ"],
                          start_date="2026-07-07", end_date="2026-07-08")["data"]
from collections import Counter
print("各标的行数:", dict(Counter(q["code"])))
