# qmt-supplemental-source Specification (delta)

## ADDED Requirements

### Requirement: QMT 前复权日线以独立身份入库

系统 SHALL 将 QMT 通道的前复权日线写入 `daily` 表,身份固定为
`source="qmt"`、`adjustment_mode="qfq"`、`adjustment_version="qmt-front-v1"`,
且绝不写入其他任何身份的行。

#### Scenario: 正常入库
- Given QMT 通道可达且标的 `600519.SH` 有本地历史
- When 执行 QMT 日线同步
- Then `daily` 表新增/覆盖的行全部带 `qmt/qfq/qmt-front-v1` 身份
- And 其他身份(baostock/wind/tonghuashun)的行数不变

#### Scenario: 可重复执行
- Given 某区间已入库
- When 以相同窗口再次同步
- Then 同身份同 (code,date) 行被覆盖为最新值,总行数不膨胀

### Requirement: 通道交互只读、禁止池回退

QMT 客户端 SHALL 只使用 GET 类端点与 `/fulldata` 按需通道;
`/fulldata` 失败时 MUST 直接报错,不得回退到「替换常驻标的池」路径。

#### Scenario: fulldata 失败
- Given `/fulldata` 提交后返回错误状态或超时
- When 同步该标的
- Then 该标的记为失败并跳过,不发起任何会改变 Windows 常驻标的池的请求

### Requirement: 入库行校验与停牌证据

每行 bar 必须满足:日期为合法 ISO 日期;open/high/low/close 有限且为正;
volume 有限且非负;`low <= open/close <= high`。停牌行 SHALL 跳过入库,
但记录为该标的的停牌证据日。

#### Scenario: 非法行拒收
- Given 一行 close 为 0 或 high < low
- When 同步
- Then 该行不入库,计入 invalid 并在输出中列示

#### Scenario: 停牌日
- Given 某日 `suspendFlag` 为真
- When 同步
- Then 该日无 bar 入库,但该日计入停牌证据用于覆盖验证

### Requirement: 覆盖声明可验证

`sync_coverage` SHALL 只在「库内他源交易日历 + 已观测停牌日」能完整解释
`[min,max]` 区间时记录;库内交易日历为空时 MUST NOT 记录。

#### Scenario: 覆盖空洞
- Given 区间内某库内交易日既无 bar 又无停牌证据
- When 同步完成
- Then 该标的不记录 sync_coverage,并在输出中列示 COVERAGE-HOLE

### Requirement: 同步对主链路非阻塞

`daily_sync.sh` 的 QMT 阶段 SHALL 在 baostock 更新之后运行;
QMT 阶段失败 MUST NOT 改变脚本的主退出码(由 baostock 阶段决定),
失败 SHALL 触发系统通知。

#### Scenario: QMT 通道断开时的日更
- Given baostock 更新成功,QMT 通道不可达
- When launchd 触发 daily-sync
- Then 脚本退出码为 0(baostock 语义),日志记录 QMT 阶段失败并系统通知

### Requirement: 账户数据密封隔离

账户/持仓观测 SHALL 写入 `~/.stockdata/qmt-account/` 下的 0600 权限 JSON 文件,
含 `captured_at`、`account`、`positions` 与规范化内容的 sha256;
MUST NOT 写入主库 `daily`/`sync_coverage`,MUST NOT 将账户内容写入日志。

#### Scenario: 捕获与校验
- Given 通道可达且账户已登录
- When 执行账户捕获
- Then 生成 0600 权限的时间戳 JSON,verify 函数可校验 sha256 自洽
- And 日志/stdout 只出现文件路径与哈希,不出现持仓明细

### Requirement: 凭据注入

Token SHALL 仅从 `QMT_TOKEN` 环境变量或 `~/.stockdata/qmt-token`(0600)读取;
仓库中 MUST NOT 出现 token 值。

#### Scenario: token 文件权限不当
- Given `~/.stockdata/qmt-token` 权限非 0600
- When 任意 QMT 操作读取 token
- Then 拒绝读取并报错,不发起通道请求
