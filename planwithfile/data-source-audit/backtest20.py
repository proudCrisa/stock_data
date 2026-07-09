#!/usr/bin/env python3
"""20 股批量多源交叉回测验证 —— 数据可信度的硬证据。

覆盖所有已存数据维度，逐股逐源比对：
  1) 历史区间日线（最近 ~20 交易日）—— OHLC 四价全字段，不只 close
  2) 当天最新 bar
  3) 与腾讯实时价交叉（最新交易日 close vs 实时 price）

多个独立源（baostock 前复权 / mootdx 通达信TCP / pytdx 通达信TCP / efinance 东财）对同一
(股, 日, 字段) 给出的值，在容差内一致 → 可信。任一源失败自动跳过，不影响其它源判定。

判定：对每只股，取"最近 8 个共同交易日"，比对 open/high/low/close 四价：
  - 每个 (日,字段) 的多源相对极差 <= tol(默认 0.5%) 记为一致
  - 全部一致 → PASS；否则列出不一致项
用法：
  .venv/bin/python planwithfile/data-source-audit/backtest20.py
  .venv/bin/python planwithfile/data-source-audit/backtest20.py 600519,000001,...   # 自定义股池
"""
import datetime as dt
import socket
import sys
import urllib.request

# ---- 20 支覆盖各板块/市场类型的股池（沪深主板/创业板/科创板/ETF/指数）----
DEFAULT_POOL = [
    ("600519", "sh"), ("601398", "sh"), ("600036", "sh"), ("601899", "sh"),
    ("600030", "sh"), ("688981", "sh"), ("688111", "sh"),          # 科创板
    ("000001", "sz"), ("000002", "sz"), ("000858", "sz"),
    ("002475", "sz"), ("002594", "sz"),                            # 中小板
    ("300750", "sz"), ("300059", "sz"), ("300760", "sz"),          # 创业板
    ("510050", "sh"), ("510300", "sh"), ("159915", "sz"),          # ETF
    ("000300", "sh"), ("399006", "sz"),                            # 指数(沪深300/创业板指)
]

TOL = 0.005   # 0.5% 相对容差
NDAYS = 8     # 每股比对最近 N 个共同交易日
END = dt.date.today().isoformat()
START = (dt.date.today() - dt.timedelta(days=40)).isoformat()

FIELDS = ("open", "high", "low", "close")


def fetch_baostock(code, mkt):
    import baostock as bs
    import contextlib, os
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        bs.login()
        rs = bs.query_history_k_data_plus(
            f"{mkt}.{code}", "date,open,high,low,close",
            start_date=START, end_date=END, frequency="d", adjustflag="2")
        out = {}
        while rs.next():
            r = rs.get_row_data()
            out[r[0]] = {"open": float(r[1]), "high": float(r[2]),
                         "low": float(r[3]), "close": float(r[4])}
        bs.logout()
    return out


def fetch_mootdx(code, mkt):
    from mootdx.quotes import Quotes
    srv = None
    for ip in ["119.97.185.59", "124.70.133.119", "116.205.183.150"]:
        try:
            with socket.create_connection((ip, 7709), timeout=2):
                srv = (ip, 7709); break
        except Exception:
            pass
    c = Quotes.factory(market="std", server=srv)
    df = c.index(symbol=code, frequency=9, offset=30) if _is_index(code, mkt) \
        else c.bars(symbol=code, frequency=9, offset=30)
    out = {}
    for _, r in df.iterrows():
        d = str(r["datetime"])[:10]
        out[d] = {"open": float(r["open"]), "high": float(r["high"]),
                  "low": float(r["low"]), "close": float(r["close"])}
    return out


def _is_index(code, mkt):
    return code.startswith("399") or (mkt == "sh" and code.startswith("000"))


def fetch_pytdx(code, mkt):
    from pytdx.hq import TdxHq_API
    market = 1 if mkt == "sh" else 0
    api = TdxHq_API()
    out = {}
    with api.connect("119.97.185.59", 7709):
        if _is_index(code, mkt):
            data = api.get_index_bars(9, market, code, 0, 30)
        else:
            data = api.get_security_bars(9, market, code, 0, 30)
        df = api.to_df(data)
        for _, r in df.iterrows():
            d = str(r["datetime"])[:10]
            out[d] = {"open": float(r["open"]), "high": float(r["high"]),
                      "low": float(r["low"]), "close": float(r["close"])}
    return out


def fetch_efinance(code, mkt):
    import efinance as ef
    df = ef.stock.get_quote_history(code, klt=101, fqt=1)
    out = {}
    for _, r in df.tail(30).iterrows():
        d = r["日期"][:10]
        out[d] = {"open": float(r["开盘"]), "high": float(r["最高"]),
                  "low": float(r["最低"]), "close": float(r["收盘"])}
    return out


def fetch_tencent_realtime(code, mkt):
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={mkt}{code}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
    parts = raw.split('"')[1].split("~")
    return {"price": float(parts[3]), "date": parts[30][:8]}


# baostock/efinance 前复权；mootdx/pytdx 近端不复权（近端无除权，四价应一致）
SOURCES = {
    "baostock": fetch_baostock,
    "mootdx": fetch_mootdx,
    "pytdx": fetch_pytdx,
    "efinance": fetch_efinance,
}


def check_stock(code, mkt):
    series = {}
    errs = {}
    for name, fn in SOURCES.items():
        try:
            series[name] = fn(code, mkt)
        except Exception as e:
            errs[name] = f"{type(e).__name__}"
    try:
        rt = fetch_tencent_realtime(code, mkt)
    except Exception:
        rt = None

    if len(series) < 2:
        return {"code": code, "verdict": "SKIP(源不足2)", "sources_ok": list(series),
                "errs": errs, "mismatches": []}

    # 最近 NDAYS 个共同交易日
    common = sorted(set.intersection(*[set(s) for s in series.values()]))[-NDAYS:]
    mismatches = []
    checked = 0
    for d in common:
        for f in FIELDS:
            vals = {name: series[name][d][f] for name in series if d in series[name]}
            checked += 1
            v = list(vals.values())
            base = sorted(v)[len(v) // 2]  # 中位数
            if base and (max(v) - min(v)) / abs(base) > TOL:
                mismatches.append((d, f, vals))

    # 实时价 vs 最新共同日 close
    rt_line = ""
    if rt and common:
        latest = common[-1]
        closes = [series[n][latest]["close"] for n in series]
        base = sorted(closes)[len(closes) // 2]
        match = base and abs(rt["price"] - base) / abs(base) <= TOL
        rt_line = f"实时{rt['price']} vs 共识{base} -> {'一致' if match else '不一致'}"

    verdict = "PASS" if not mismatches else f"FAIL({len(mismatches)})"
    return {"code": code, "verdict": verdict, "sources_ok": list(series),
            "errs": errs, "checked": checked, "common_days": len(common),
            "mismatches": mismatches[:3], "realtime": rt_line}


def main():
    pool = DEFAULT_POOL
    if len(sys.argv) > 1:
        pool = [(c[:6], "sh" if c[0] in "6591" and not c.startswith(("00", "30", "15", "39"))
                 else ("sh" if c.startswith(("5", "6", "000300")) else "sz"))
                for c in sys.argv[1].split(",")]

    print(f"===== 20 股多源交叉回测（tol={TOL*100:.1f}%, 最近{NDAYS}日 × OHLC四价）=====")
    print(f"{'代码':<8}{'判定':<12}{'成功源':<28}{'实时交叉':<32}")
    print("-" * 90)
    npass = nfail = nskip = 0
    fails = []
    for code, mkt in pool:
        r = check_stock(code, mkt)
        ok = ",".join(r["sources_ok"])
        print(f"{code:<8}{r['verdict']:<12}{ok:<28}{r.get('realtime',''):<32}")
        if r["verdict"] == "PASS":
            npass += 1
        elif r["verdict"].startswith("FAIL"):
            nfail += 1
            fails.append(r)
        else:
            nskip += 1
    print("-" * 90)
    print(f"PASS={npass}  FAIL={nfail}  SKIP={nskip}  / 共 {len(pool)} 股")
    for r in fails:
        print(f"\n[FAIL] {r['code']} 不一致项(前3):")
        for d, f, vals in r["mismatches"]:
            print(f"   {d} {f}: {vals}")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
