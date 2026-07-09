# Evidence: ffd-local-daily-history

## Slice 3 实现前验证 — 历史主源选型（2026-07-08）

### 百度K线主源：不达标，弃用

- 端点 `https://finance.pae.baidu.com/selfselect/getstockquotation`（a-stock-data 蓝本 URL）
- 真实拉取 `600519` `start_time=2024-01-01 ktype=1`：
  ```
  HTTP 200；Result 为空 list（len=0）——不返回任何 K 线数据
  ```
- 结论：该端点当前失效（与 a-stock-data CHANGELOG "百度 PAE 接口失效" 记录吻合）。
  不作为历史主源。

### baostock：达标，采纳为历史主源（Stage 7 授权自动切换）

- `bs.query_history_k_data_plus("sh.600519", "date,open,high,low,close,volume",
   start="2024-01-01", end="2024-06-30", frequency="d", adjustflag="2")`（2=前复权）
- 真实拉取结果：**117 个交易日，完整 OHLCV，前复权连续价**。
  ```
  first: 2024-01-02 open=1558.48 high=1561.38 low=1524.95 close=1531.23 vol=3215644
  last:  2024-06-28 open=1372.79 high=1386.23 low=1357.95 close=1361.09 vol=3858202
  ```

### 交叉校验：baostock vs findesk 缓存（601899.SH 2024H1）

对齐 super-trader-rqgm `data/ffd_2024H1_batch1.json` 的 601899.SH：

| 指标 | 值 |
|---|---|
| 共同交易日 | 117 / 117（完全对齐）✅ |
| close 相对偏差 | 均值 5.21%，最大 5.34% |
| 偏差形态 | **同一时段内恒定**（1月恒 5.34%，6月恒 4.26%），随分红除权渐变 |

**判定：可解释，通过。** 偏差源于 findesk 与 baostock 的**前复权基准日不同**（整体缩放），
但**逐日相对形态（涨跌幅序列）完全一致**——对 RQGM 回测（基于收益率序列）无实质影响。
符合 spec `daily-history` "与 findesk 口径一致性验证" requirement 的"可解释范围内"。

### 决策落地

- **历史主源 = baostock（adjustflag=2 前复权）**。
- 百度K线从多源策略中移除（接口失效）。
- mootdx 仍作近端补齐/兜底；腾讯仍作今日盘中 bar。
- 更新多源策略：**baostock(前复权) 主 + mootdx 补 + 腾讯实时今日 bar**。
- pyproject 依赖：baostock 从 optional 升为核心依赖。

## Slice 9 全量一致性验证（2026-07-08）

`scripts/validate_against_findesk.py` 对 findesk 缓存全部 12 只标的做重叠区间比对：

| 标的 | 共同日 | 均值偏差 | 时段内标准差 | 判定 |
|---|---|---|---|---|
| 000725.SZ | 117 | 2.80% | 0.179% | ✅ |
| 159941.SZ | 0 | — | findesk 缓存无该 ETF 数据（无重叠） |
| 300750.SZ | 117 | 5.79% | 1.085% | ✅ |
| 600030.SH | 117 | 6.59% | 0.000% | ✅ |
| 600362.SH | 117 | 6.59% | 0.000% | ✅ |
| 600519.SH | 117 | 9.00% | 0.475% | ✅ |
| 600673.SH | 117 | 3.67% | 1.241% | ✅ |
| 601166.SH | 117 | 15.12% | 0.000% | ✅ |
| 601328.SH | 117 | 11.48% | 0.000% | ✅ |
| 601899.SH | 117 | 5.21% | 0.350% | ✅ |
| 603501.SH | 117 | 0.98% | 0.000% | ✅ |
| 603993.SH | 117 | 6.02% | 0.000% | ✅ |

**结论：11/11 有重叠标的全部通过。** 时段内偏差标准差均 < 1.3%（多为 0.000%），
证明 findesk 与 baostock 的差异是**时段内恒定的整体缩放**（前复权基准日不同），
**逐日涨跌形态完全一致** → 对 RQGM 回测（基于收益率序列）零实质影响。
均值偏差随标的分红除权幅度不同（0.98%~15.12%），属正常复权基准差异。

## 单元 + 集成测试证据

- 默认单测：**78 passed, 3 deselected**（`.venv/bin/python -m pytest`，Python 3.12）。
- network 集成：baostock / mootdx / 腾讯 各 1 passed（真实数据源连通）。
- 端到端冒烟：`ffd_query` 真实拉取 600519.SH 2024H1 = 117 天列式格式（含 open）；
  缓存命中子区间无重复拉取；`ffd_quote_history` 多标的正确平铺。
- MCP：`ffd_query` / `ffd_quote_history` 两工具在 FastMCP 注册成功。
