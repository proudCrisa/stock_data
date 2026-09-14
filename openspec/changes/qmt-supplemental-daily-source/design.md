# Design

## 架构位置

```
Windows QMT (QmtExport/2.0, 策略 v11)
   │  frp stcp 隧道
   ▼
127.0.0.1:8000  ──X-Token──►  stockdata/fetch_qmt.py  (QmtChannelClient)
                                   │  /  status 健康检查
                                   │  /fulldata history_kline (dividend_type=front)
                                   ▼
                        scripts/sync_qmt_daily.py
                                   │  校验(wind 级防御规则)
                                   ▼
                        Cache.upsert(source="qmt", adjustment_mode="qfq",
                                     adjustment_version="qmt-front-v1")
                                   │
                                   ▼
                        ~/.stockdata/cache.sqlite  daily 表(第五身份)

账户/持仓:  scripts/capture_qmt_account.py ──► ~/.stockdata/qmt-account/
                                                (0600 JSON + sha256,不进主库)
```

## 关键决策

### D1: 身份隔离,永不混源
`source="qmt"`, `adjustment_mode="qfq"`, `adjustment_version="qmt-front-v1"`。
与 wind 入库(`scripts/ingest_wind_csv.py`)同一原则:复权口径未经交叉验证前,
每个源独立身份,消费方显式选择。`Cache.upsert` 只替换同身份行,天然隔离。

### D2: 禁止池回退
`qmt_full.py` 的 `history()` 在 `/fulldata` 失败时会回退到「替换常驻标的池 +
轮询快照」,这会覆盖 Windows 端 309 标的池。本项目的客户端**不实现回退路径**:
`/fulldata` 失败即报错。批量同步绝不 mutate 通道端状态——与现有
`qmt_pool_replay` 的 get-only 语义一致。

### D3: 校验规则对齐 wind 入库
每行 bar 必须:日期合法 ISO;OHLC 有限且为正;volume 有限非负;
`low <= open/close <= high`。停牌行(`suspendFlag` 真或 volume 缺失/0 且
服务端标记)不入库但记为停牌证据。`sync_coverage` 只在「库内他源交易日历 +
已观测停牌日」完整解释 `[min,max]` 时记录——日历为空一律不记录。

### D4: 非阻塞自动化
`daily_sync.sh` 在 baostock `update` 之后追加 QMT 阶段:
- baostock 退出码 = 脚本退出码(主链路语义不变);
- QMT 阶段独立记录退出码,失败时系统通知,但 **不改写** 主退出码;
- 通道不可达(token 缺失/隧道断)时 QMT 阶段快速失败(exit 3),不拖慢主链路。

### D4b: 批量前置健康门
进入标的循环前必须过 `assert_ready()`:服务标识存在、`latest_exists` 为真、
`latest_age_sec ≤ 900`(v11 策略全天 60s 一跳)。防止「HTTP 导出进程活着但
Windows 策略已停」时整批标的逐个轮询干等数小时。单标的 fulldata 等待上限
默认 60s(CLI `--timeout` 可调);轮询只对登记期瞬时 404 重试,其余错误立即失败。

### D5: 凭据注入
顺序:`QMT_TOKEN` env → `~/.stockdata/qmt-token`(要求 0600,否则拒绝)。
仓库、脚本、openspec 中不出现 token 值。

### D6: 账户数据密封隔离
账户/持仓属隐私数据,按 authority 约束走文件密封而非主库:
`~/.stockdata/qmt-account/<UTC时间戳>.json`,0600 权限,内容含
`{captured_at, account, positions, sha256(规范化内容)}`。提供 verify 函数。
不外发、不入库、不进日志(日志只记文件路径与哈希)。

## 数据形状

`/fulldata` `history_kline` 返回(经 qmt_full.py 实证):
`{status:"ok", data:{symbol:{index:[dates...], columns:[{open,high,low,close,volume,...}]}}}`。
解析时只取 `open/high/low/close/volume` 五个字段 + `suspendFlag`(若有);
其余字段忽略但不视为错误(向前兼容)。

## 同步窗口
默认 `start = today - 30d`(与 baostock 日更滚动窗口一致),`--start` 可覆盖做
历史回填。QMT 本地无数据的标的返回错误 → 该标的记 error 跳过,批次继续。

## 错误分级(脚本退出码)
- 0:全部干净
- 1:无可用代码文件 / 通道不可达且无一行入库
- 2:部分标的失败 / invalid 行 / 覆盖空洞(部分成功)
- 3:通道不可达或 token 缺失(快速失败,未尝试入库)

## 测试策略
全部 hermetic:`QmtChannelClient` 接受可注入的 HTTP 传输函数,测试用假传输
模拟状态/历史/错误/超时;同步逻辑对临时 sqlite 库跑。账户捕获测试验证
0600 权限、sha256 自洽、内容规范化。不打真实通道的测试一律不进套件
(真实冒烟手动执行,见 tasks.md)。
