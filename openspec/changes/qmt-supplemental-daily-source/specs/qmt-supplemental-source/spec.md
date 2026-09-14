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
- When 再次同步
- Then 同身份同 (code,date) 行被覆盖为最新值,总行数不膨胀

#### Scenario: 除权后全量重述(单因子版本)
- Given 某标的已入库,随后发生分红除权,QMT 前复权重述其全部历史
- When 执行同步
- Then 本次通道返回的全部历史行整体覆盖入库(同一复权因子版本)
- And 通道返回范围之外的更早日行被删除
- And 重述后转为非正价的日期,其旧因子残留正价行被删除
- And 库内该标的的 qmt 序列不存在新旧因子混排的接缝

### Requirement: 快照主路径与 fulldata 备路径

日更 SHALL 使用快照主路径:一次 `GET /latest` 取全池前复权 K 线;
`/fulldata` 逐标的按需通道仅用于回填/池外标的(实证 ~5 分钟/标的,
不作日更路径)。池外面板标的 SHALL 记为 `not_in_pool` 而非错误。

#### Scenario: 日更快照同步
- Given 通道健康(快照 60s 级新鲜)
- When 执行日更
- Then 只发起 `/` 与 `/latest` 两个请求,池∩面板标的全部入库
- And 池外面板标的列入 not_in_pool,不影响退出码

#### Scenario: 池内标的记录畸形
- Given 快照中某池内标的的记录为空/缺 index
- When 执行日更
- Then 该标的记入 errors(而非 not_in_pool),退出码为 2

#### Scenario: 全部标的均不在池
- Given 请求的代码全部不在常驻池
- When 执行日更
- Then 退出码为 0(合法状况,不产生失败告警)

### Requirement: 通道交互只读、禁止池回退

QMT 客户端 SHALL 只使用 GET 类端点与 `/fulldata` 按需通道;
`/fulldata` 失败时 MUST 直接报错,不得回退到「替换常驻标的池」路径。

#### Scenario: fulldata 失败
- Given `/fulldata` 提交后返回错误状态或超时
- When 同步该标的
- Then 该标的记为失败并跳过,不发起任何会改变 Windows 常驻标的池的请求

### Requirement: 入库行校验与停牌证据

每行 bar 必须满足:日期为合法 ISO 日期;open/high/low/close 有限;
volume 有限且非负;`low <= open/close <= high`。停牌行 SHALL 跳过入库,
但记录为该标的的停牌证据日。前复权深历史因累计除权转为非正价的行
SHALL 跳过入库(Cache 正价不变量),但作为已观测证据参与覆盖声明,
并单独计数(nonpositive);晚于最新已定稿交易日的行 MUST NOT 入库。

#### Scenario: 非法行拒收
- Given 一行 close 为 NaN 或 high < low 或 volume 为负
- When 同步
- Then 该行不入库,计入 invalid 并在输出中列示

#### Scenario: 非正价前复权深历史
- Given 某标的 2021 年的前复权行因累计除权转为非正
- When 同步
- Then 该行不入库,计入 nonpositive
- And 该日视为已观测(不构成覆盖空洞)
- And 库内同日旧因子残留行被删除

#### Scenario: 停牌日
- Given 某日 `suspendFlag` 为真或成交量为 0
- When 同步
- Then 该日无 bar 入库,但该日计入停牌证据用于覆盖验证

#### Scenario: 盘中运行
- Given 当前时刻早于当日收盘定稿(Asia/Shanghai 16:00)
- When 同步
- Then 当日演化中的 bar 不入库,上界为最新已定稿交易日

### Requirement: 覆盖声明可验证且与刷新一致

同步 SHALL 先完整验证再写库(fail-closed):返回含非法行、库内他源日历为空、
或 `[min,max]` 内存在无证据解释的日历交易日时,该标的**一行不写**。
验证通过后,`sync_coverage` SHALL **替换**为本次实际验证的区间
(上下界取所有已观测日,含尾部停牌/零成交证据),MUST NOT 以 MIN/MAX
合并旧区间——刷新是破坏性的,合并会让声明超出库内实际数据。

#### Scenario: 非法行整标的拒收
- Given 本次返回中某行 OHLC 关系被破坏
- When 同步
- Then 该标的整批不写库,记 errors,库内保持旧因子版本的一致序列

#### Scenario: 覆盖空洞
- Given 区间内某库内交易日既无 bar 又无停牌/非正价证据
- When 同步完成
- Then 该标的一行不写、不记录 sync_coverage,输出列示 COVERAGE-HOLE

#### Scenario: 窗口滑动后覆盖收缩
- Given 上次验证区间 [D1, D2],本次通道返回区间 [D2, D3](左端右移)
- When 同步完成
- Then sync_coverage 为 [D2, D3],不再保留 D1 起点
- And daily 表中早于 D2 的 qmt 行已删除,声明与库内数据一致

#### Scenario: 尾部停牌推进覆盖
- Given 最后一个有效 bar 在 D1,之后 D2 为停牌证据日
- When 同步完成
- Then 覆盖区间右界为 D2

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
