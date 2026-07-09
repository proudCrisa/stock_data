# Findings: ffd-local-daily-history（只读发现）

## 1. findesk 的真实访问方式：FFD MCP（非直连 HTTP）

super-trader-rqgm 里没有任何 `findesk` / `ffd.findesk.cn` 的直接 HTTP 引用。
findesk 数据通过一个 **FFD MCP server** 访问。实际用到的 MCP 工具只有两个：

| MCP 工具 | 证据 | 用途 |
|---|---|---|
| `ffd_query(function='history', start_date=...)` | `scripts/sync_market_data.py:152` | 拉历史日线 |
| `ffd_quote_history` | `scripts/extend_anchor_q2.py:4`（docstring） | 拉指定时段日线 |

> ⚠️ findesk MCP 的完整工具 schema（全部 function 取值、参数名）未能从会话历史或
> MCP 配置中取回——server 定义当前不在 `~/.claude` 配置里。因此契约以**逆向到的用法
> + 缓存数据结构**为准。用户已确认"功能等价、格式可优化"，无需逐字段还原 findesk。

## 2. findesk 返回数据结构（来自真实缓存）

`data/ffd_2024H1_batch1.json` 实测顶层即列式并行数组（每个 351 长）：

```
time   : ["2024-01-02", "2024-01-03", ...]     # 交易日
code   : ["601899.SH", "601899.SH", ...]       # 带 .SH/.SZ 后缀
close  : [12.49, 12.53, ...]
volume : [113316938.0, 76049148.0, ...]        # 浮点
high   : [12.66, 12.59, ...]
low    : [12.39, 12.39, ...]
```

**关键点：**
- 无 `open` 字段 —— 本地版应**补齐 open**（三个候选源都能提供）。
- 多标的时按 (code, time) 平铺到同一组并行数组（长表/列式），不是嵌套字典。
- MCP 原始返回还有**双重 JSON wrapper**：`process_ffd_data.py:11-13` 显示
  `wrapper["result"]`（字符串）再 `json.loads` 得 `inner["data"]`。本地版应**去掉**
  这层 wrapper，直接返回 `{"data": {time,code,...}}`（保留一层 data 兼容
  `ffd_sync.py:36` 的 `raw.get("data", raw)`）。

## 3. super-trader-rqgm 消费端契约（drop-in 必须兼容）

- `ffd_sync.py:36-40`：`data = raw.get("data", raw)`；读 `data["time"/"code"/"close"/"volume"]`。
  → 本地版返回顶层含 `data` 键、data 内含这些并行数组即兼容。
- `process_ffd_data.py:13-18`：读 `inner["data"]` 的 `time/code/close/volume`。
  → 若去 wrapper，此脚本需微调（属 super-trader-rqgm 侧，本 change 出切换指引即可）。
- `extend_anchor.py:23`：注释明确"每个 data/ffd_*.json 含并行数组 time/code/close/volume"。

## 4. 现状数据源层级（痛点已在代码注释写明）

`sync_market_data.py:1-7` 头注释：
```
主数据源: mootdx (通达信TCP, 免费无限制, 100条日K线)
备用数据源: FFD MCP (付费, 仅11行截断)   ← 本项目要替换的对象
数据存于: ~/.hermes/memories/data/rqgm/market_data.json
```

- **主源已切 mootdx**（`sync_via_mootdx`, `bars(frequency=9, offset=100)`）——但只有 100 条、
  不复权，且**无法拉任意历史区间**。
- **实时价**走腾讯/新浪（`rqgm/realtime.py`: `qt.gtimg.cn` / `hq.sinajs.cn`）。
- **历史区间前复权**是 findesk 当前的独有价值 —— 也是本地版必须补上的核心能力缺口。
- 曾用 **akshare** 拉 2024 历史（`pull_2024_data.py`，`adjust="qfq"` 前复权），
  但按用户最新决策，历史日线改以**百度K线（前复权）主 + mootdx 补**。

## 5. 标的范围

- 硬编码历史列表 18 只（`pull_2024_data.py`、`ffd_sync.py`：茅台/宁德/紫金/京东方/
  中信证券/兴业/交行/豪威/东阳光/纳指ETF/招行/万科/立讯/恒瑞/沪深300/上证50/洛钼/江铜）。
- 但同步脚本已改为**动态跟随统一标的池** `rqgm.universe.load_universe()`
  （portfolio.holdings ∪ watchlist.items）。
- 含**指数/ETF**（159941/510050/000300 等），本地版拉取层需支持 ETF/指数代码。

## 6. 数据源蓝本能力对照（a-stock-data SKILL.md，已 clone）

| 源 | 函数 | 返回 | 复权 | 适用 |
|---|---|---|---|---|
| mootdx | `client.bars(symbol, frequency=9, offset=N)` | open/close/high/low/vol/amount/datetime | **不复权** | 近端补齐、TCP 不封 IP |
| 腾讯 | `tencent_quote(codes)` | price/open/high/low/last_close | 实时 | 今日 bar |
| 百度股市通 | `baidu_kline_with_ma(code, start_time)` | time/open/close/high/low/volume/amount + MA | **候选前复权** | 历史区间主源 |

- mootdx 0.11.x 有 **BESTIP 空串 bug**，SKILL.md 提供 `tdx_client()` helper 规避
  （TCP 探测可用服务器 + 三级 fallback + 海外快速失败）——本地版拉取层应移植此 helper。
- 东财接口需 `em_get()` 串行限流防封 —— 本项目行情层暂不依赖东财，可不引入。
- 市场前缀/ticker 归一化规则（.SH/.SZ ↔ sh/sz ↔ 6位纯码）SKILL.md 有现成逻辑，可移植。

> **待验证（Stage 9 前必须确认）：** 百度 K线是否真为前复权、能否拉任意起始日、
> 停牌日如何表达。SKILL.md 未逐字保证复权口径；需在实现前用真实拉取样本核对，
> 必要时以 baostock（前复权 + 停牌标记齐全）作为交叉校验源。

## 7. go-stock 的可借鉴点

- Go 项目，含前端 + AI 分析，行情抓取只是其中一层，**不直接复用代码**。
- 价值：其"数据全部保留本地 + SQLite"的工程做法，印证本地缓存 DB 方向；
  可参考它的表结构设计（按 code+date 主键存 OHLCV）。

## 8. 复刻能力边界（结论）

super-trader-rqgm 对 findesk 的**全部实际使用 = 日线历史 OHLCV（列式返回）**。
无财务、无基本面、无板块、无研报调用。因此本地版复刻范围 = **日线历史 OHLCV 一项能力**，
通过 `ffd_query(function='history')` 与 `ffd_quote_history` 两个 MCP 工具暴露。
