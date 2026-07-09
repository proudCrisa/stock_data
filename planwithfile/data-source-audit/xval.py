#!/usr/bin/env python3
"""实网多源交叉验证：同一标的最近日线收盘价横向比对 + 与腾讯实时价对照。

用途：确认哪个免费源"稳定"（可用/一致/复权），数据是否与实时拉取一致。
运行：.venv/bin/python planwithfile/data-source-audit/xval.py 600519 sh
"""
import datetime as dt
import socket
import sys
import urllib.request

CODE = sys.argv[1] if len(sys.argv) > 1 else "600519"
MKT = sys.argv[2] if len(sys.argv) > 2 else "sh"   # sh / sz
END = dt.date.today().isoformat()
START = (dt.date.today() - dt.timedelta(days=20)).isoformat()
results = {}


def last3(d):
    return dict(list(d.items())[-3:])


# 1) akshare 东财 前复权
try:
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=CODE, period="daily",
                            start_date=START.replace('-', ''),
                            end_date=END.replace('-', ''), adjust="qfq")
    results['akshare_qfq'] = {str(r['日期'])[:10]: round(float(r['收盘']), 2)
                              for _, r in df.tail(3).iterrows()}
except Exception as e:
    results['akshare_qfq'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

# 2) efinance 前复权
try:
    import efinance as ef
    df = ef.stock.get_quote_history(CODE, klt=101, fqt=1)
    results['efinance_qfq'] = {r['日期'][:10]: round(float(r['收盘']), 2)
                               for _, r in df.tail(3).iterrows()}
except Exception as e:
    results['efinance_qfq'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

# 3) baostock 前复权
try:
    import baostock as bs
    bs.login()
    rs = bs.query_history_k_data_plus(f"{MKT}.{CODE}", "date,close",
                                      start_date=START, end_date=END,
                                      frequency="d", adjustflag="2")
    d = {}
    while rs.next():
        r = rs.get_row_data()
        d[r[0]] = round(float(r[1]), 2)
    bs.logout()
    results['baostock_qfq'] = last3(d)
except Exception as e:
    results['baostock_qfq'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

# 4) mootdx 通达信 不复权
try:
    from mootdx.quotes import Quotes
    srv = None
    for ip in ['119.97.185.59', '124.70.133.119', '116.205.183.150']:
        try:
            with socket.create_connection((ip, 7709), timeout=2):
                srv = (ip, 7709)
                break
        except Exception:
            pass
    c = Quotes.factory(market='std', server=srv)
    df = c.bars(symbol=CODE, frequency=9, offset=5)
    results['mootdx_raw'] = {str(r['datetime'])[:10]: round(float(r['close']), 2)
                             for _, r in df.tail(3).iterrows()}
except Exception as e:
    results['mootdx_raw'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

# 5) pytdx 通达信 不复权
try:
    from pytdx.hq import TdxHq_API
    market = 1 if MKT == "sh" else 0
    api = TdxHq_API()
    with api.connect('119.97.185.59', 7709):
        data = api.get_security_bars(9, market, CODE, 0, 5)
        df = api.to_df(data)
        results['pytdx_raw'] = {str(r['datetime'])[:10]: round(float(r['close']), 2)
                                for _, r in df.tail(3).iterrows()}
except Exception as e:
    results['pytdx_raw'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

# 6) 腾讯实时价
try:
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={MKT}{CODE}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
    parts = raw.split('"')[1].split("~")
    results['tencent_realtime'] = {'price': round(float(parts[3]), 2),
                                   'date': parts[30][:8]}
except Exception as e:
    results['tencent_realtime'] = f"ERR {type(e).__name__}: {str(e)[:70]}"

print(f"===== {CODE} 多源实网交叉验证 ({END}) =====")
for src, val in results.items():
    print(f"{src:20}: {val}")
