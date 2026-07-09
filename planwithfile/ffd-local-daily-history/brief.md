# Brief: ffd-local-daily-history

## Goal（目标）

在 `~/workspaces/stock_data/` 构建一个**稳定、本地化**的 A 股日线历史行情服务，
以 MCP 同名工具（`ffd_query` / `ffd_quote_history`）**drop-in 替换** `super-trader-rqgm`
当前依赖的 `ffd.findesk.cn`（FFD MCP，付费且不稳定）数据源。

## Non-goals（非目标）

- 不复刻 findesk 的财务/基本面/板块/研报等能力 —— super-trader-rqgm 当前**未使用**。
- 不做分钟/周/月 K 线 —— 只做**日线**。
- 不做 UI / 前端 / AI 分析（go-stock 的那部分能力不在范围内）。
- 不改动 super-trader-rqgm 的任何业务/策略代码；仅在其数据获取入口做 drop-in 切换。
- 不做港股/美股（findesk 用法仅涉及 A 股 + 场内 ETF/指数）。

## Expected users / callers（调用方）

- 主调用方：`super-trader-rqgm`（Python，RSI/RQGM 自进化策略回测与推荐）。
  - `scripts/sync_market_data.py`（FFD 备用模式：`ffd_query(function='history', start_date=...)`）
  - `scripts/extend_anchor_q2.py`（`ffd_quote_history` 拉时段日线）
  - `scripts/ffd_sync.py`、`scripts/process_ffd_data.py`（消费 FFD 列式 JSON）
- 未来潜在调用方：workspaces 下其它需要 A 股日线的项目（supertrader 等）。

## Success criteria（成功标准）

1. 本地 MCP server 暴露 `ffd_query` 与 `ffd_quote_history` 两个工具，
   入参与返回结构与 super-trader-rqgm 现有解析代码**兼容**（列式并行数组
   `time/code/close/volume/high/low`，并**补齐 open**）。
2. 给定 code + 时间段，返回真实前复权日线 OHLCV，与 findesk 历史缓存
   （`data/ffd_2024H1_batch*.json`）在重叠区间内 close 偏差可解释（复权口径差异内）。
3. 断网/数据源单点故障时，命中本地缓存 DB 仍可返回历史区间（离线可用）。
4. super-trader-rqgm 侧仅需改数据获取入口（≤ 一处封装），策略代码零改动即可跑通
   一次完整回测。
5. 有可复现的 build/test 证据（pytest 全绿 + 一次真实拉取样本 + 一次 drop-in 冒烟）。

## Risk tolerance / Rollback（风险容忍度与回滚）

- 风险容忍度：中。这是替换**外部付费依赖**，主要风险在数据口径一致性与拉取稳定性。
- 回滚：super-trader-rqgm 侧的切换是单点封装，保留原 FFD 调用路径为 fallback；
  本地服务不可用时可一键切回（或直接用已落地的历史缓存）。

## Deadline / urgency（紧迫度）

无硬 deadline。当前 findesk 已退化为"备用"角色（主源已是 mootdx），
但要彻底摆脱付费+不稳定依赖，需要本地版补上"历史区间前复权"这块 findesk 独有的口径。

## Available docs / inputs（已有输入）

- **逆向证据**：super-trader-rqgm 源码（见 findings.md）。
- **数据源蓝本**：`simonlin1212/a-stock-data`（SKILL.md，行情层 mootdx+腾讯+百度K线，
  含 tdx_client 防坑、东财防封）—— 6.7k⭐，2026-07 仍维护。
- **本地存储参考**：`ArvinLovegood/go-stock`（Go，数据全部保留本地，SQLite 落地思路）。
- findesk 本体文档不可读（付费源），故以逆向 + 蓝本为准。

## Key decisions locked（已锁定决策）

| 维度 | 决定 |
|---|---|
| 交付形态 | MCP 同名工具 server + 本地缓存 DB（drop-in） |
| 开源项目用法 | 以 a-stock-data 为蓝本自研；go-stock 借鉴本地存储 |
| 多源策略 | 百度K线(前复权)主 + mootdx 补 + 腾讯实时今日 bar |
| 能力范围 | 仅日线历史 OHLCV |
| 输出格式 | 列式并行数组，功能等价、补齐 open、去掉双重 JSON wrapper |
| 语言栈 | 纯 Python |
