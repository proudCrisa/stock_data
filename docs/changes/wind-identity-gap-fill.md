# Wind 身份缺口补齐设计与实施记录

日期：2026-08-24/25 · 分支：`audit/data-completeness`

## 背景与审计结论

基线：`~/.stockdata/cache.sqlite`（140,164 行 / 556 代码），两个互不相交的价格身份：

| 身份 | 代码数 | 审计发现 |
|---|---|---|
| `baostock/qfq/baostock-adjustflag-2` | 83 | 无内部空洞；47 只尾部滞后 5 个交易日（止于 2026-08-14） |
| `tonghuashun/qfq/ths-qfq-v1` | 473 | 473 只全部尾部滞后 5 天；19 只有内部空洞（130 个代码日） |

交易日历基准：以库内全覆盖代码推导（2828 个交易日，最新 2026-08-21），
与 `scripts/audit_cache_completeness.py` 的 baostock 官方日历审计结果一致。

## 设计决策

1. **baostock 身份用原生管道补齐**（`sync.sync_symbols` + `fetch_baostock`）：
   生产读取路径（`HistoryService`，见 `service.py`）只走 baostock 身份，
   原生管道保证来源/复权口径/收据约定完全一致。

2. **外部数据源一律以独立身份入库，绝不混源**：
   Wind 数据以 `wind/qfq/wind-fwd-v1` 身份写入。
   项目明确「生产历史只用 baostock，避免复权混源」（`service.py` 模块注释）；
   把 Wind 数据写成 baostock/tonghuashun 身份属于伪造溯源，且不同供应商
   前复权因子可能在接缝处产生价格跳变。

3. **tonghuashun 身份本体保持只读冻结**：
   `make_tonghuashun_service()` 设计上就是 473 只离线宇宙
   （「缺口不自动回补」，`stockdata.py`），不改动其语义；
   需要新鲜数据的消费方用 `make_service(source="wind",
   adjustment_version="wind-fwd-v1")` 读取 wind 身份。

4. **停牌日不入库**：Wind 对停牌日返回平推价 + 空成交量/成交额，
    ingest 时 volume 解析失败即跳过（与「停牌空洞不视为缺口」语义一致，
   见 `cache.py` 模块注释）。

5. **入库幂等**（行集合层面）：`Cache.upsert` 按身份主键冲突覆盖，
   `record_sync_coverage` 按 min/max 合并区间，脚本可任意重跑；
   注意重跑会刷新 `retrieved_at`，数据库字节级状态并非严格幂等。

6. **限流应对**：Wind 单次调用限 3 个标的且有限流；采用子代理串行 +
   每次调用间隔 12s + 失败 60s 退避重试一次的节奏（并行爆发会被限流，
   单worker温柔串行已验证可全量通过）。

## Codex 交叉审查与加固（2026-08-25）

Codex 初审发现 3 高 / 4 中 / 2 低，已逐项修复：

- **高：`HistoryService` 混源回补路径**。非 baostock 身份未显式传 fetcher
  时，默认 baostock fetcher 会把 baostock 数据误标成该身份。修复：
  `service.py` 中 `HistoryService.__init__` 对非 baostock 身份默认只读
  （`_empty_fetch`），库层面封死该路径；`make_service(fetch_missing=True,
  source=非baostock)` 这条显式逃生门同步改为直接 `ValueError`（确需回补
  应显式传与该身份口径一致的 `primary_fetch`）。补
  `test_service.py::TestNonBaostockReadOnly`，并将
  `test_make_service.py` 中原先断言该混源行为的用例改写为断言拒绝。
- **高：`sync_coverage` 过度声明风险**。ingest 记录覆盖前先验证：
  [min,max] 内每个库内交易日都必须有 bar 或有已观测停牌行解释，
  否则拒绝记录覆盖并报告（exit 2）。
- **高：数值无校验**。ingest 现校验 ISO 日期、有限值、正价格、非负成交量、
  OHLC 关系（low ≤ open/close ≤ high），非法行拒收并逐条报告（exit 2）。
- **中：停牌跳过过宽**。空成交量（停牌）与其余解析失败分别计数；
  表头缺列整文件拒绝；全部不可用返回 1。
- **中：代码未规范化**。入库前经 `ticker.normalize` 规范化。
- **中：重复文件冲突**。同代码同日期数值冲突时该日整日拒收并报告；
  全部干净后 CSV 移入 `ingested/` 归档，避免陈旧文件回滚新数据。
- **低：文档措辞**。幂等语义已按上节修正。
- **低：缺测试**。新增 `tests/test_ingest_wind_csv.py`（14 例）。

**已知行为（设计内，非缺陷）**：baostock 身份查询覆盖"今天"时，
`HistoryService` 会把腾讯实时 bar 追加到返回序列（行级 source 标注为
`tencent/intraday/tencent-realtime-v1`，`is_final=False`）；
非 baostock 身份默认不合并（见下节第二轮修复）。

## Codex 第二轮复审与加固（2026-08-25）

二轮复审 2 高 / 2 中 / 1 低，已逐项修复：

- **高：空交易日历时 coverage 验证失效**。`SELECT DISTINCT date FROM daily`
  为空时任何区间都会被当作无空洞。修复：日历为空一律不记录
  `sync_coverage`（宁可缺 coverage 也不过度声明），计入 problems（exit 2）。
- **高：非 baostock 服务返回值混源**。`_today` 默认腾讯实时取数，
  `_merge_today` 会把 intraday 行并入返回序列，与「只读本地缓存」契约冲突。
  修复：`HistoryService.__init__` 对非 baostock 身份默认 `_none_today`；
  补 `test_service.py::test_non_baostock_source_does_not_merge_today_bar`。
- **中：表头错误未计入退出码**。坏表头文件现计入 problems（exit 2），
  与头部退出码说明一致。
- **中：部分失败批次归档语义不自洽**。改为按文件归档：无 invalid 且不涉及
  冲突/空洞代码的文件移入 `ingested/`，脏文件留在原目录待人工处理。
- **低：冲突记录字符串反解析**。改为结构化 `(code, day)` 集合，
  打印文案不参与业务逻辑。

测试增至 18 例（`tests/test_ingest_wind_csv.py`）+ 2 例 service 守卫，
全部通过；全量套件通过。

## Codex 第三轮复审与加固（2026-08-25）

三轮复审判定 2 条修好、3 条未完全修好、1 条新增低，已全部修复：

- **高：空日历重跑自证覆盖**。第一轮写入的 wind 行会在第二轮被当作日历
  证据自证无缺口。修复：覆盖验证的交易日历只取 `source != 'wind'` 的行，
  本身份数据永远不能自证；补 `test_rerun_cannot_self_certify_coverage`。
- **中：停牌证据文件误归档**。停牌行原先不计入文件的代码集合，只含停牌
  证据的文件可能在代码有覆盖空洞时被归档、丢失证据。修复：停牌行同样
  计入文件代码集合；补两个归档判定用例。
- **中：全坏表头退出码措辞**。脚本头部退出码说明修正为
  「1 = 无 CSV 或全部表头不可用」。
- **低：文档测试数量不实**（已随本节一并更正为 20 例）。

测试：20 例 ingest + 2 例 service 守卫全部通过；全量套件通过。

## 实施

- 新增 `scripts/ingest_wind_csv.py`：扫描 CSV 目录（`trade_date,wind_code,
  open,high,low,close,volume[,amt]`），按代码分组 upsert 为
  `wind/qfq/wind-fwd-v1` 身份，并记录 `sync_coverage`。
- baostock 身份：83 只经原生 `sync_symbols` 补齐至最新 finalized 交易日。
- wind 身份：473 只 ths 宇宙 + 28 只与 baostock 重叠代码（组批时误用
  未排除清单所致，保留作为跨源验证副本），覆盖 2026-08-17 起。

## 验证

- 入库行 0 空值 / 0 非正价格。
- 与 tonghuashun 身份重叠的 38 个代码日收盘价完全一致（中位偏差 0.0000%）。
- 130 个内部空洞代码日中，真实成交全部补上；残余空洞经 Wind 空成交量
  确认为停牌（如 `002155.SZ` 2026-08-20/21）。
- 终检：任一身份覆盖到最新交易日的代码 555/556（唯一例外为停牌标的）。

## 遗留问题（未处理）

1. 473 只代码历史仅约 8 个月（ths 身份自 2025-12-22 起）；深度回填到
   2015 约百万行量级，待决策。
2. 生产默认 API 不读 wind 身份；消费方需显式 `make_service(source="wind",
   adjustment_version="wind-fwd-v1")`（只读，不会网络回补）。
3. wind 身份日常维护需要每个交易日增量同步（目前手动/子代理执行）。
