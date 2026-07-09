# Tasks

> TDD 铁律：每个实现任务先写失败测试（RED）→ 最小实现（GREEN）→ 重构。
> 无失败测试不写生产代码。

## 阶段门

- [ ] Discovery complete（findings.md / capability-map.md 已定稿）✅ 已完成
- [ ] Spec scenarios approved（Stage 7 审批门）
- [ ] Tests or characterization evidence added
- [ ] Review against spec
- [ ] Verification evidence captured
- [ ] Archive/update durable knowledge

## Slice 0 — 项目骨架

- [ ] 建 Python 包骨架（`stock_data/` 包、`pyproject.toml`、pytest 配置、`.gitignore`）
- [ ] 声明依赖：mootdx、requests、pandas、stockstats、mcp(SDK)
- [ ] 冒烟：`pytest` 能跑（0 用例通过）+ 包可 import

## Slice 1 — ticker 归一化（先做，被所有源依赖）

- [ ] 测试：`600519.SH`/`600519`/`sh600519` ↔ 内部规范形态互转；ETF/指数识别
- [ ] 实现：移植 a-stock-data 的市场前缀/归一化逻辑（最小版）
- [ ] 验证：各源所需格式转换正确

## Slice 2 — 缓存层（SQLite）

- [ ] 测试：(code,date) upsert 去重；查区间；命中/缺口计算；空库行为
- [ ] 实现：SQLite schema（code,date,open,high,low,close,volume,adjust,source,updated）+ 读写
- [ ] 验证：增量缺口合并、离线读取

## Slice 3 — 拉取层：百度K线主源（前复权）

- [ ] **实现前**：真实拉取样本核对复权口径 / 起始日范围 / 停牌表达（写入 evidence）
- [ ] 测试：百度K线返回 → 归一化为内部 OHLCV 行（mock 固定响应）
- [ ] 实现：`baidu_kline`（参考 a-stock-data `baidu_kline_with_ma`，去掉 MA，保 OHLCV）
- [ ] 验证：一只个股 + 一只 ETF 跨季度拉取成功

## Slice 4 — 拉取层：mootdx 补齐 + tdx_client 防坑

- [ ] 测试：mootdx bars → 内部 OHLCV；tdx_client 服务器探测/fallback（mock）
- [ ] 实现：移植 `tdx_client()`；`mootdx_bars`（frequency=9）
- [ ] 验证：BESTIP 空串场景不崩；海外不可达快速失败

## Slice 5 — 拉取层：腾讯实时今日 bar

- [ ] 测试：tencent_quote 解析 → 今日 bar（覆盖/追加逻辑）
- [ ] 实现：`tencent_quote`（参考 a-stock-data）
- [ ] 验证：end_date=今日 时最后一根 bar 为实时价

## Slice 6 — service 编排（多源 + 缓存 + 增量）

- [ ] 测试：查缓存→缺口拉主源→mootdx 兜底→今日实时→写回；断网离线返回；主源失败兜底
- [ ] 实现：service.get_history(code, start, end)
- [ ] 验证：三条 scenario（命中/增量/离线）+ 兜底

## Slice 7 — 列式返回组装（drop-in 契约）

- [ ] 测试：返回结构契约（顶层 data、七字段等长数组、含 open、无双重 wrapper）；多标的平铺
- [ ] 实现：rows → 列式并行数组 `{"data": {...}}`
- [ ] 验证：契约断言全过

## Slice 8 — MCP 同名工具

- [ ] 测试：`ffd_query(function='history',...)` 与 `ffd_quote_history(...)` 返回契约结构
- [ ] 实现：MCP server（Python SDK）暴露两工具，转调 service
- [ ] 验证：MCP inspector / 本地调用两工具成功

## Slice 9 — 口径一致性验证 + drop-in 冒烟

- [ ] 与 `super-trader-rqgm/data/ffd_2024H1_batch*.json` 重叠区间比对 close（写 evidence）
- [ ] 写《super-trader-rqgm 切换指引》（如何把 FFD MCP 指向本地，保留 fallback）
- [ ] drop-in 冒烟：按指引切换后跑一次 super-trader-rqgm 回测流程无异常（不改其业务代码）

## Slice 10 — 文档与归档

- [ ] 写 stock_data 的 CLAUDE.md / README（命令、架构、数据源、缓存位置）
- [ ] openspec 归档 change；更新 durable 知识
- [ ] （用户授权后）codegraph 重新索引
