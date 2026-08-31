# QMT 只读探针

这个探针只回答一个问题：Windows 上现有 QMT 本地缓存，能否导出一份可在 Mac 独立校验的
小型日线证据。它不是正式 `DataProduct`，不能进入交易引擎，也不能产生交易动作。

## 安全边界

- 只导入 `xtquant.xtdata`，只调用 `get_market_data_ex`。
- 固定 `fill_data=False`，不会下载历史数据、订阅行情、连接账户或调用交易接口。
- 每次最多 3 个代码、180 个自然日；QMT 请求的 `count` 固定为 256，每个代码/复权组合
  最多 256 行，整个 JSON 最多 2 MiB。
- 默认同时采集 raw（`dividend_type=none`）和 qfq（`dividend_type=front`）。任一组合无数据或
  身份不匹配时整体失败，不生成部分工件。
- 工件固定为 `decision_eligible=false`、`decision_authority=false`、`actions=[]`，用途只有
  环境探测和离线校验。
- 脚本自身不会选择仓库状态、SQLite、QMT 数据目录或 `trading/state`；输出目录完全由操作者
  显式指定，因此不要把 `--output-root` 指向这些目录。

## Windows 采集

先在 QMT 客户端中确认这三个代码对应区间的历史数据已经存在于本地。然后在能够
`import xtquant` 的 QMT Python 环境中，从 `stock_data` 仓库根目录执行：

```powershell
python .\scripts\export_qmt_readonly_probe.py capture `
  --code 588730.SH `
  --code 561980.SH `
  --code 600000.SH `
  --start 2026-03-02 `
  --end 2026-08-28 `
  --output-root D:\qmt-probe-output
```

成功时输出 `status=captured` 和一个以 SHA-256 命名的 JSON 文件。若提示 `xtquant.xtdata is
unavailable`，应改用 QMT 自带或已经配置好 `xtquant` 的 Python；不要通过脚本自动安装、下载
或扫描 QMT 目录。

## Mac 校验

把生成的单个 JSON 文件手工复制到 Mac。Mac 不需要安装 `xtquant`，从 `stock_data` 根目录执行：

```bash
python scripts/export_qmt_readonly_probe.py verify \
  --artifact /absolute/path/to/<artifact_sha256>.json
```

只有输出 `status=verified_diagnostic_only` 才说明文件的 canonical bytes、外层 hash、请求范围、
代码/复权组合闭包、逐组合 receipt/rows hash 和零交易权限合同都通过。每个组合的
`coverage.status` 固定为 `observed_subset_unverified`；它只陈述实际观察到的首尾日期、watermark
和行数，不声称覆盖了请求区间的所有交易日。raw/qfq 同时采集时，两者日期面板必须一致。
这个结果仍不代表来源真实性、完整历史覆盖、QMT 正式数据源、策略 Alpha 或生产交易就绪。

## 停止条件

首次完成“Windows 三标的采集 + Mac 校验”后停止。不要扩大到全市场，不接定时任务，不接
`engine.cli daily`。raw/qfq 参数、字段、单位或 QMT 版本语义若与实际环境不符，保留失败输出并
在适配层核对，不猜测映射。至少积累 20 个交易日的独立 shadow parity 证据后，才讨论是否提出
正式数据源接入提案。

QMT API 参数以官方文档为准：
[xtdata 原生 Python API](https://dict.thinktrader.net/nativeApi/xtdata.html)。
