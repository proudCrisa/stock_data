# Proposal

## Why

QMT 通道(mac 端经 frp stcp 隧道连 Windows QMT 导出策略,QmtExport/2.0)已实盘验证
活跃:309 标的常驻池、快照秒级新鲜、支持任意标的**前复权历史日线**(`/fulldata`
按需通道)与账户/持仓查询。但项目内 QMT 只接入了「密封研究采集」层
(`qmt_transport_capture` / `qmt_pool_replay` / `qmt_pool_batch_capture` /
`qmt_readonly_probe`),主库 `daily` 表没有 qmt 身份——通道的历史日线与账户数据
均未成为可查询、可审计的数据资产。

## What changes

1. **补充价格身份**(核心):新增 `qmt/qfq/qmt-front-v1` 身份写入 `daily` 表。
   - 新模块 `stockdata/fetch_qmt.py`:最小 urllib 通道客户端(status +
     `/fulldata` 按需历史,**禁止池回退**——批量任务不得覆盖 Windows 常驻标的池),
     复用 wind 入库的全套防御规则(有限正价、OHLC 关系、停牌证据、覆盖声明验证)。
   - 新脚本 `scripts/sync_qmt_daily.py`:滚动窗口增量同步,面板默认
     `config/panel-baostock.txt`,可重复执行(同身份 upsert)。
2. **账户数据**(密封):新模块 `stockdata/qmt_account_capture.py` +
   脚本 `scripts/capture_qmt_account.py`,把账户/持仓观测写成 0600 权限、
   带 sha256 的时间戳 JSON,落入 `~/.stockdata/qmt-account/`。**不进主库**。
3. **自动化**:`scripts/daily_sync.sh` 追加 QMT 阶段——**非阻塞**:QMT 失败
   只告警(系统通知 + 日志),绝不影响 baostock 主链路退出码。
4. Token 经环境变量 `QMT_TOKEN` 或 `~/.stockdata/qmt-token`(0600)注入,
   仓库不落地任何凭据。

## Non-goals

- QMT **不取代** baostock 主链路;`daily` 默认身份、CLI `update` 主流程、
  现有四身份均不变。QMT 是高置信**补充**源,消费方须显式选择身份。
- 不做分钟级/盘口/财务/新闻接入(通道有能力,本期不需要)。
- 不做三源自动仲裁(交叉验证已有 `scripts/compare_tencent_baostock.py` 先例,
  后续单独立项)。
- 不改动 Windows 端策略;不回退写常驻标的池。

## Impact

- `daily` 新增第五身份 `qmt/qfq/qmt-front-v1`;`sync_coverage` 同样规则记录。
- launchd `local.stockdata.daily-sync` 每日多跑一个非阻塞 QMT 阶段。
- 新增能力 spec:`qmt-supplemental-source`。

## Risks

| 风险 | 缓解 |
|---|---|
| Windows QMT 离线/隧道断 → 同步失败 | 非阻塞设计:失败只告警;失败率可从日志审计 |
| 某标的 QMT 本地无历史(未订阅下载) | 按标的记录错误并跳过,不阻塞其余标的;可扩充 |
| 复权口径与 baostock qfq 不一致 | 独立身份隔离,绝不混源;消费方显式选择 |
| token 泄漏 | 仅 env / 0600 文件注入,仓库零凭据 |
| /fulldata 回退覆盖常驻池 | 客户端硬编码 `allow_pool_fallback=False` 语义:回退路径不存在 |

## Rollback

- 删除 qmt 身份的全部状态(单事务):
  `DELETE FROM daily WHERE source='qmt'`,
  `DELETE FROM sync_coverage WHERE source='qmt'`,
  `DELETE FROM source_watermarks WHERE source='qmt'`
  (三者同属该身份;漏删会让覆盖声明超出实际数据、水位线误拒后续恢复)。
- daily_sync.sh 的 QMT 阶段为追加段,移除即恢复原行为。
