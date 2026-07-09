# Context Plan: ffd-local-daily-history

不加载全部内容。按需读取，聚焦"复刻 findesk 日线历史"这一最小闭环。

## Greenfield 上下文（本项目要新建什么）

- 目标 stack：纯 Python（MCP server + 拉取层 + 缓存 DB）。
- 团队约定：workspaces 多为 Python + 中文工作语言 + ProofRails 纪律（TDD 强制）。
- 初始质量门：pytest + 一次真实拉取证据 + drop-in 冒烟。

## 复刻契约来源（只读逆向，已完成）

从 `~/workspaces/super-trader-rqgm` 提取，不修改：

| 读取对象 | 目的 | 状态 |
|---|---|---|
| `scripts/sync_market_data.py` | 拿到 `ffd_query(function='history', start_date=...)` 签名 + 数据源层级 | ✅ 已读 |
| `scripts/extend_anchor_q2.py` | 确认 `ffd_quote_history` 工具存在与用途 | ✅ 已读 |
| `scripts/ffd_sync.py` / `process_ffd_data.py` | 确认列式返回结构 + 双重 JSON wrapper | ✅ 已读 |
| `scripts/pull_2024_data.py` | 现有 akshare 前复权拉取参照 | ✅ 已读 |
| `data/ffd_2024H1_batch1.json` | findesk 真实返回字段：time/code/close/volume/high/low | ✅ 已验 |
| `rqgm/universe.py`（load_universe） | 标的池动态来源（不再硬编码 18 只） | 待按需读 |

## 数据源蓝本（只读参考，已 clone 到 /tmp/ffd-refs）

| 参考对象 | 用途 |
|---|---|
| `a-stock-data/SKILL.md` 行情层 | mootdx `bars(frequency=9)`、腾讯 `tencent_quote`、`baidu_kline_with_ma`（前复权候选）、`tdx_client()` 防 BESTIP bug | 
| `a-stock-data` 数据源优先级章节 | "mootdx/腾讯不封 IP 优先，东财限流" 原则 | 
| `go-stock` | 本地 SQLite 落地 + 数据保留本地的工程做法（借鉴，不照搬 Go 代码） |

## 允许 / 禁止

**允许**：读文件、查 codegraph（本项目暂空，主要查 super-trader-rqgm）、只读 agent、
clone 参考仓到 /tmp。
**禁止**：本 change 审批前不编辑 stock_data 源码；任何情况下不编辑 super-trader-rqgm
的业务代码。
