# stockdata API 文档

可靠 A 股行情数据接口。经 20 股实网多源交叉验证：日线主源 baostock 前复权 +
通达信 mootdx/pytdx 交叉 + 腾讯实时，收盘价 OHLC 多源一致、与实时价 100% 吻合。
开箱即用，无需 API key。数据落地本地 SQLite。

供 super-trader-rqgm 等项目调用，取代原 findesk 数据源。

## 引入

    import sys
    sys.path.insert(0, "/Users/cdzhangxueli/workspaces/stock_data")
    import stockdata as sd

依赖：pip install baostock mootdx pytdx requests pandas

代码格式：get_ 系列接受 600519 / 600519.SH / sh600519，内部自动归一化。
日期 YYYY-MM-DD；end_date 省略默认今天。

## 1. get_daily(code, start_date, end_date) 返回 dict

历史日线前复权，列式并行数组 data 里含 time/code/open/high/low/close/volume。
各字段等长、按日期升序。多标的按 code+time 平铺。

## 2. get_daily_df(code, start_date, end_date) 返回 DataFrame

列 date/open/high/low/close/volume。

## 3. get_realtime(code) 返回 dict 或 None

实时价，腾讯今日盘中 bar。非交易时段或无成交返回 None。
返回 date/open/high/low/close/volume。

## 4. cross_check(code, start_date, end_date, tol=0.005) 返回 dict

多源交叉验证。返回 date 到 DayConsensus 的映射：consensus 共识收盘价、
n_sources 有效源数、agreement 多源一致、outliers 离群源、realtime_match 最新日与实时一致。
交易前置检查：agreement 且 realtime_match 为 True 才用。

## 稳定性（20 股实测）

- baostock 前复权：历史主源，全程可用
- mootdx/pytdx 通达信 TCP：交叉主力，不封 IP
- 腾讯：实时基准，逐股命中

注意：指数已用指数专用接口；除权日涨跌幅以 baostock 前复权口径为准；
cross_check agreement 为 False 表示多源分歧，该日数据需人工核对。

## 验证脚本（可复现）

    .venv/bin/python verify_stockdata.py
    .venv/bin/python planwithfile/data-source-audit/backtest20.py
    .venv/bin/python planwithfile/data-source-audit/verify_returns.py 688111 sh
