# Capability Map: ffd-local-daily-history

| Capability | Spec exists? | Delta | Note |
|---|---|---|---|
| `daily-history` | no（greenfield，本 change 首次定义） | ADDED | 本地日线历史行情：按 code + 时间段返回前复权 OHLCV，列式并行数组，drop-in 兼容 findesk 消费端 |

## 能力职责（daily-history）

- 输入：标的代码（支持 `.SH/.SZ` 后缀、6 位纯码、指数/ETF）+ 时间段（start_date / end_date）。
- 输出：列式并行数组 `{data: {time, code, open, high, low, close, volume}}`（补齐 open、去双重 wrapper）。
- 多源：百度K线(前复权)主 → mootdx 补 → 腾讯实时今日 bar。
- 落地：本地缓存 DB（SQLite，(code,date) 主键），先查缓存再增量拉取，离线可用。
- 暴露：MCP 工具 `ffd_query(function='history', ...)` 与 `ffd_quote_history(...)`。

## 子能力（同一 change 内，作为 daily-history 的 scenarios，不单列 capability）

- ticker 归一化（.SH/.SZ ↔ sh/sz ↔ 纯码）
- 多源 fallback 与 close 交叉一致性告警（可选）
- 缓存命中 / 增量拉取 / 离线返回

## Skipped capabilities

无。super-trader-rqgm 对 findesk 的使用仅限日线历史，其余 findesk 能力（财务/研报/
资金面/打板/ETF期权/舆情）均**明确不在范围**（见 findings.md §8），不作为 skipped
待办，而是显式非目标。

## Baselines initialized this run

无。greenfield，无既有代码可建 baseline；本 change 的 spec 即 `daily-history` 首次定义。

## Gate 确认项（Stage 7 审批需回带）

1. 能力范围仅 `daily-history` 一项 —— 是否认可？
2. 接口契约一旦定稿即成为 super-trader-rqgm 的 drop-in 契约（高风险契约）—— 需用户确认。
