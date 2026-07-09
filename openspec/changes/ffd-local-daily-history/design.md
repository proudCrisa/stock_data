# Design

## Current state

- `stock_data/` 为空（仅未填充的 .codegraph/）。
- super-trader-rqgm 通过 FFD MCP（`ffd_query` / `ffd_quote_history`）拉 findesk 历史日线，
  返回列式并行数组（time/code/close/volume/high/low，无 open），外包双重 JSON wrapper。
- findesk 付费、不稳定、单次截断 ~11 行。

## Target state

本地纯 Python 项目，分三层，暴露 findesk 同名 MCP 工具：

```
super-trader-rqgm  ──MCP──►  ffd-local MCP server
                                 │  ffd_query(function='history', code/start/end)
                                 │  ffd_quote_history(codes, start, end)
                                 ▼
                         ┌───────────────────────┐
                         │  service 层            │  查缓存→缺口增量拉取→写缓存→组装列式返回
                         └───────────────────────┘
                              │              │
                    ┌─────────┘              └──────────┐
                    ▼                                    ▼
            cache (SQLite)                        fetch 层（多源）
        (code,date)→ohlcv 主键               百度K线(前复权)主
        离线命中、增量                        mootdx(tdx_client)补
                                              腾讯实时→今日bar
```

## Key decisions

1. **同名 MCP 工具 + 列式返回**：drop-in 第一原则。返回 `{"data": {...}}`，data 内为
   `time/code/open/high/low/close/volume` 并行数组。保留外层 `data` 键（兼容
   `ffd_sync.py:36` `raw.get("data", raw)`），但去掉 findesk 那层 `result` 字符串再
   `json.loads` 的双重 wrapper（`process_ffd_data.py` 侧改一行读取即可）。
2. **补齐 open**：findesk 缺 open，三源均可提供，本地版一律带 open —— 严格超集，不破坏消费端。
3. **百度K线为历史主源**：用户决策。它带前复权候选且能按起始日拉区间；mootdx 只补近端
   （100 条、不复权）与主源失败兜底；腾讯只提供当日盘中 bar。
4. **SQLite 缓存 + 增量**：service 先按 (code, [start,end]) 查缓存，只对缺口区间拉取，
   命中即离线可用。借鉴 go-stock "数据保留本地" 思路。
5. **ticker 归一化**：统一内部用 `600519.SH` 形态；拉取层按源要求转换（mootdx 用 6 位纯码 +
   market，百度/腾讯用 sh/sz 前缀）。移植 a-stock-data 的归一化逻辑。
6. **纯 Python 同栈**：与 a-stock-data、super-trader-rqgm 一致，MCP 用 Python SDK，零跨语言。

## Alternatives rejected

- **直接 HTTP REST 服务**：需改 super-trader-rqgm 调用方式，非 drop-in，弃。
- **把 a-stock-data 作为运行时依赖**：绑定其代码风格/更新节奏、且它是 Skill(Markdown)
  形态不适合当库依赖，弃（改为"以其为蓝本自研"）。
- **Go 拉取 + Python MCP（借 go-stock）**：多一层进程间通信、运维变重，收益不抵成本，弃。
- **akshare 为历史主源**：曾用（pull_2024_data.py），但用户选百度K线主；akshare 可留作
  可选交叉校验，不作主源。
- **多源交叉校验为默认**：拉取成本与复杂度上升，降级为可选告警。

## Data flow / control flow

`ffd_query(function='history', code, start_date, end_date)`：
1. 归一化 code。
2. service 查 SQLite：命中区间 → 取；缺口 → 调 fetch(百度K线)；仍缺近端 → mootdx 补；
   end_date 含今日 → 腾讯实时覆盖/追加今日 bar。
3. 写回缓存（(code,date) upsert）。
4. 组装列式并行数组，包一层 `{"data": {...}}` 返回。

`ffd_quote_history(codes, start_date, end_date)`：多标的循环上述流程，结果平铺进同一组
列式数组（与 findesk 多标的长表一致）。

## Risk matrix

| 项 | 概率 | 影响 | 处置 |
|---|---|---|---|
| 百度K线复权/范围/停牌口径偏差 | 中 | 高 | 实现前真实样本核对 + baostock 交叉校验（spec 有验证 scenario） |
| mootdx 不可达 | 中 | 低 | 非历史主源；tdx_client 快速失败 |
| 缓存与最新价不一致 | 中 | 中 | 今日 bar 走实时覆盖；历史 bar 除权后可强制重拉 |
| drop-in 契约偏差 | 低 | 高 | 契约测试锁定字段/结构；冒烟跑通 super-trader-rqgm 一次回测 |

## Verification strategy

- TDD：每个 scenario 先写失败测试。
- 契约测试：返回结构与字段严格断言（列式、含 open、外层 data 键）。
- 真实拉取样本：至少 1 只个股 + 1 只 ETF/指数，跨一个季度，核对复权连续性。
- 交叉校验：与 super-trader-rqgm `data/ffd_2024H1_batch*.json` 重叠区间比对 close 偏差。
- drop-in 冒烟：把 super-trader-rqgm 数据入口指向本地，跑一次回测流程无异常。
