# Proposal

## Why

super-trader-rqgm 当前的历史区间前复权日线依赖 `ffd.findesk.cn`（FFD MCP）：
**付费**、**不稳定**、单次返回**被截断（约 11 行）**。这使自进化策略的回测/anchor 扩展
无法可靠地获取任意历史区间。需要一个**本地、稳定、离线可用**的替代，且要能**零改动或
极小改动**地切换 super-trader-rqgm。

## What changes

新建 `stock_data` 项目，交付 **`daily-history`** 能力：

1. **拉取层**：以 a-stock-data(SKILL.md) 行情层为蓝本自研 —— 百度K线(前复权)主源、
   mootdx(TCP，`tdx_client()` 防 BESTIP bug)补齐近端、腾讯实时提供今日 bar。
2. **缓存层**：本地 SQLite，(code, date) 主键存 OHLCV；先查缓存再增量拉取，断网命中缓存。
3. **MCP 层**：暴露与 findesk **同名**的两个工具 `ffd_query`(function='history') 与
   `ffd_quote_history`，返回列式并行数组 `{data:{time,code,open,high,low,close,volume}}`
   —— 兼容 super-trader-rqgm 现有解析，且**补齐 open**、**去掉双重 JSON wrapper**。
4. **切换指引**：一份文档，指导 super-trader-rqgm 把 FFD MCP 指向本地 server（≤ 一处封装）。

## Non-goals

- 不做分钟/周/月 K；不做财务/研报/资金面/板块/打板/期权/舆情（findesk 其余能力，未被使用）。
- 不做 UI/AI 分析；不做港股/美股。
- 不在本 change 内编辑 super-trader-rqgm 业务代码（切换属另一次变更）。

## Impact

- 新增能力：`daily-history`（openspec/specs/daily-history）。
- 新增项目 `stock_data`：Python 包 + SQLite 缓存 + MCP server。
- 下游：super-trader-rqgm 的 `sync_market_data.py` / `extend_anchor_q2.py` / `ffd_sync.py`
  的数据来源切到本地（drop-in）。
- 外部依赖：新增 mootdx / requests / pandas / stockstats（拉取层）；MCP SDK。
  移除对 findesk 付费源的运行时依赖。

## Risks

| 风险 | 缓解 |
|---|---|
| 百度K线复权口径 / 起始日范围 / 停牌表达 与 findesk 不一致 | Stage 9 前用真实样本核对；必要时引入 baostock 交叉校验 |
| mootdx 海外/TCP 不可达 | `tdx_client()` 快速失败 + 百度K线为主源不依赖 mootdx 拉历史 |
| drop-in 契约字段/结构偏差导致 super-trader-rqgm 解析失败 | spec 明确列式结构 + 保留一层 data 键；提供冒烟测试 |
| 多源 close 数值差异 | 单一主源保证自洽；交叉校验仅告警不阻断 |

## Rollback

- super-trader-rqgm 侧切换为单点封装，保留原 FFD 调用路径为 fallback，一键切回。
- 本地 server 不可用时，已落地的历史缓存 DB / JSON 仍可直接喂给回测。
