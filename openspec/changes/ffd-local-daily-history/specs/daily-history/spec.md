# daily-history Specification (delta)

## ADDED Requirements

### Requirement: 按标的与时间段返回日线历史 OHLCV

系统 SHALL 接受标的代码与时间段（start_date / end_date），返回该区间内每个交易日的
前复权日线数据，字段包含 open、high、low、close、volume。

#### Scenario: 单标的历史区间
- Given 标的 `600519.SH` 与时间段 `2024-01-01`~`2024-06-30`
- When 调用历史查询
- Then 返回该区间全部交易日，每日含 open/high/low/close/volume 五个数值
- And 日期升序、无重复交易日
- And close 为前复权价（跨除权日连续，无跳变缺口）

#### Scenario: ETF / 指数标的
- Given 标的 `510050.SH`（上证50ETF）或 `000300.SH`（沪深300）
- When 调用历史查询
- Then 正常返回 OHLCV（拉取层识别 ETF/指数并走对应源），不抛"非个股"错误

#### Scenario: 空区间 / 停牌
- Given 时间段内该标的全程停牌或无交易日
- When 调用历史查询
- Then 返回空的并行数组（各字段长度为 0），而非报错或返回 null

### Requirement: 列式并行数组返回结构（drop-in 兼容）

系统 SHALL 以列式并行数组返回，顶层含 `data` 键，`data` 内为等长的
`time / code / open / high / low / close / volume` 数组，使 super-trader-rqgm 现有解析
（`raw.get("data", raw)` 后读并行数组）无需重构即可消费。

#### Scenario: 返回结构契约
- Given 任意成功的历史查询
- When 检查返回 JSON
- Then 顶层存在 `data` 对象
- And `data.time/code/open/high/low/close/volume` 均为数组且长度相等
- And 不存在 findesk 的双重 wrapper（无 `result` 字符串再次 JSON 编码）

#### Scenario: 多标的长表平铺
- Given 一次查询多个标的
- When 返回
- Then 各标的的行按 (code, time) 平铺进**同一组**并行数组
- And 可用 code 字段分组还原每只标的的时间序列

#### Scenario: 补齐 open 字段
- Given findesk 原返回无 open
- When 本地版返回
- Then open 字段存在且为该交易日开盘价（严格超集，不破坏仅读 close/volume/high/low 的消费端）

### Requirement: 本地缓存与离线可用

系统 SHALL 将拉取到的日线落地本地存储（(code, date) 唯一），查询时先命中缓存、
仅对缺口区间增量拉取；外部数据源不可达时，命中缓存的区间仍能返回。

#### Scenario: 缓存命中不重复拉取
- Given 某标的某区间已在缓存
- When 再次查询同区间
- Then 直接由缓存组装返回，不发起外部拉取

#### Scenario: 增量拉取缺口
- Given 缓存已有 `2024-01`~`2024-03`，请求 `2024-01`~`2024-06`
- When 查询
- Then 仅拉取 `2024-04`~`2024-06` 缺口并合并，最终返回完整区间

#### Scenario: 断网离线返回
- Given 外部数据源全部不可达，但请求区间已在缓存
- When 查询
- Then 返回缓存区间数据（不因拉取失败而整体报错）

### Requirement: 多源拉取与今日盘中 bar

系统 SHALL 以 baostock(前复权)为历史主源，mootdx 补齐近端/兜底，
end_date 覆盖当日时以腾讯实时价提供今日 bar。

> 主源选型说明：原计划的百度K线接口（finance.pae.baidu.com getstockquotation）经真实
> 拉取实测已失效（Result 返回空）；按 Stage 7 授权切换为 baostock（adjustflag=2 前复权），
> 已与 findesk 缓存交叉校验通过（见 planwithfile evidence.md）。

#### Scenario: 主源失败兜底
- Given baostock 对某标的拉取失败
- When 查询该标的历史
- Then 回退 mootdx 拉取近端数据，返回可用结果或明确的可解释错误（不静默返回错价）

#### Scenario: 今日盘中 bar
- Given end_date = 今日且处于交易时段
- When 查询
- Then 最后一根 bar 使用腾讯实时价（盘中滚动），其 date 为今日

#### Scenario: mootdx 客户端健壮性
- Given mootdx 0.11.x 环境或 BESTIP 为空串
- When 初始化 mootdx 客户端
- Then 经 `tdx_client()` 显式探测可用服务器创建成功；海外全不可达时快速失败并给出明确报错

### Requirement: MCP 同名工具暴露

系统 SHALL 暴露与 findesk 同名的 MCP 工具 `ffd_query` 与 `ffd_quote_history`，
使 super-trader-rqgm 仅切换 MCP 指向即可替换数据源。

#### Scenario: ffd_query(function='history')
- Given 调用 `ffd_query(function='history', start_date=..., end_date=..., code=...)`
- When 执行
- Then 返回上述列式结构历史数据（与 `sync_market_data.py:152` 的调用签名兼容）

#### Scenario: ffd_quote_history
- Given 调用 `ffd_quote_history(codes=[...], start_date=..., end_date=...)`
- When 执行
- Then 返回多标的列式历史数据（与 `extend_anchor_q2.py` 用途兼容）

### Requirement: 与 findesk 历史数据口径一致性验证

系统 SHALL 在重叠区间内与 findesk 既有缓存（super-trader-rqgm `data/ffd_2024H1_batch*.json`）
比对 close，偏差需落在可解释范围（复权口径差异内）。

#### Scenario: 重叠区间比对
- Given 本地拉取 `601899.SH` 的 2024H1 与 findesk 缓存重叠
- When 对齐交易日比对 close
- Then 相对偏差在阈值内（前复权基准不同导致的整体缩放可接受，逐日相对形态一致）
- And 若逐日形态出现不可解释跳变，则记为验证失败并告警
