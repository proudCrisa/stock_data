#!/usr/bin/env python3
"""涨跌幅一致性验证 —— 证明复权基准差异不影响交易决策。

688111 等标的多源间绝对价差异，根因是前复权基准日不同（baostock qfq vs 通达信不复权）。
交易决策依赖收益率序列（日涨跌幅），不受复权基准整体缩放影响。
比对各源日涨跌幅，若一致 → 数据对交易决策可信。

用法: .venv/bin/python planwithfile/data-source-audit/verify_returns.py 688111 sh
"""
import contextlib
import datetime as dt
import os
import sys

CODE = sys.argv[1] if len(sys.argv) > 1 else "688111"
MKT = sys.argv[2] if len(sys.argv) > 2 else "sh"
START = (dt.date.today() - dt.timedelta(days=30)).isoformat()
END = dt.date.today().isoformat()


def baostock_closes():
    import baostock as bs
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        bs.login()
        rs = bs.query_history_k_data_plus(f"{MKT}.{CODE}", "date,close",
                                          start_date=START, end_date=END,
                                          frequency="d", adjustflag="2")
        d = {}
        while rs.next():
            r = rs.get_row_data()
            d[r[0]] = float(r[1])
        bs.logout()
    return d


def pytdx_closes():
    from pytdx.hq import TdxHq_API
    market = 1 if MKT == "sh" else 0
    api = TdxHq_API()
    d = {}
    with api.connect("119.97.185.59", 7709):
        data = api.get_security_bars(9, market, CODE, 0, 20)
        df = api.to_df(data)
        for _, r in df.iterrows():
            d[str(r["datetime"])[:10]] = float(r["close"])
    return d


def pct_series(closes):
    dates = sorted(closes)
    out = {}
    for i in range(1, len(dates)):
        prev, cur = closes[dates[i - 1]], closes[dates[i]]
        if prev:
            out[dates[i]] = round((cur / prev - 1) * 100, 3)
    return out


bao = pct_series(baostock_closes())
tdx = pct_series(pytdx_closes())
common = sorted(set(bao) & set(tdx))[-8:]

print(f"===== {CODE} 日涨跌幅一致性（baostock前复权 vs 通达信不复权）=====")
print(f"{'日期':<12}{'baostock%':>12}{'pytdx%':>12}{'差异':>10}")
maxdiff = 0.0
for d in common:
    diff = abs(bao[d] - tdx[d])
    maxdiff = max(maxdiff, diff)
    flag = "OK" if diff < 0.01 else "!!"
    print(f"{d:<12}{bao[d]:>12}{tdx[d]:>12}{diff:>9.3f} {flag}")
print("-" * 48)
print(f"最大涨跌幅差异: {maxdiff:.4f} 个百分点")
print("结论:", "涨跌幅完全一致 -> 复权基准差异不影响交易决策"
      if maxdiff < 0.01 else "涨跌幅存在差异，需深查")
