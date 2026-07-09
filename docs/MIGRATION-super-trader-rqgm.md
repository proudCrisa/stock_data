# super-trader-rqgm 切换到 stockdata 指引

本地版 `stockdata` 复刻了 `ffd.findesk.cn`（FFD MCP）的**日线历史 OHLCV** 能力，
底层用 baostock(前复权) + mootdx + 腾讯实时，本地 SQLite 缓存，离线可用。

> 本指引不修改 super-trader-rqgm 的业务/策略代码。切换是数据来源的替换，
> 且保留原路径为 fallback，可一键回退。

## 前置

```bash
cd ~/workspaces/stock_data
python3.12 -m venv .venv          # 若未建
.venv/bin/pip install -e .        # 安装 stockdata + 依赖（baostock/mootdx/requests/pandas）
.venv/bin/pip install "mcp>=1.2"  # 仅 MCP server 方式需要
```

## 返回格式（与 findesk 兼容）

```json
{"data": {
  "time":   ["2024-06-03", ...],
  "code":   ["600519.SH", ...],
  "open":   [1499.41, ...],
  "high":   [...], "low": [...],
  "close":  [1489.77, ...],
  "volume": [...]
}}
```

- 与 findesk 一致：顶层 `data` 键 + 列式并行数组，消费端 `raw.get("data", raw)` 不变。
- **改进 1**：补齐 `open`（findesk 原缺）。
- **改进 2**：去掉 findesk 的双重 JSON wrapper（原 `wrapper["result"]` 再 `json.loads`）。
  → `scripts/process_ffd_data.py` 若直接对接本地版，把 `inner = json.loads(wrapper["result"])`
    改为 `inner = wrapper`（少一层解包）即可；`ffd_sync.py` 无需改动。

## 方式 A：CLI 管道（最省事，零代码改动）

`ffd_sync.py` 本就读 stdin 的列式 JSON，直接管道对接：

```bash
# 单标的
.venv/bin/stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30 \
  | python3 ~/workspaces/super-trader-rqgm/scripts/ffd_sync.py

# 多标的
.venv/bin/stockdata-cli quote_history --codes 600519.SH,000001.SZ --start 2024-01-01 --end 2024-06-30 \
  | python3 ~/workspaces/super-trader-rqgm/scripts/ffd_sync.py
```

stdout 为纯 JSON（baostock 登录日志已被抑制）。缓存 DB 默认 `~/.stockdata/cache.sqlite`，
可用 `STOCKDATA_DB=/path/to.sqlite` 覆盖。

## 方式 B：Python import（贴合 sync_market_data.py 的 FFD 备用模式）

`sync_market_data.py:152` 原本提示 `ffd_query(function='history', start_date=...)`。
本地版提供等价函数，可在 FFD 备用分支替换为：

```python
import sys
sys.path.insert(0, "/Users/<you>/workspaces/stock_data")
from datetime import date
from stockdata.cache import Cache
from stockdata.service import HistoryService
from stockdata.mcp_handlers import handle_ffd_query

_svc = HistoryService(Cache("/Users/<you>/.stockdata/cache.sqlite"))

def ffd_query(function, code, start_date, end_date=""):
    return handle_ffd_query(
        {"function": function, "code": code,
         "start_date": start_date, "end_date": end_date},
        _svc, today=date.today().isoformat())

# 用法与 findesk 一致：
data = ffd_query("history", "600519.SH", "2026-04-01")["data"]
```

## 方式 C：MCP server（若坚持 MCP 协议接入）

```bash
.venv/bin/stockdata          # 启动 stdio MCP server，暴露 ffd_query / ffd_quote_history
```

在调用方的 MCP 配置里注册该 server（command 指向 `.venv/bin/stockdata`）。工具名与 findesk
一致，调用方切换 server 指向即可。

## 回退

三种方式都不删除 super-trader-rqgm 原有代码路径。回退 = 停用本地对接、切回原 FFD/mootdx
分支即可；已落地的 `~/.stockdata/cache.sqlite` 也可直接读。

## 口径说明

baostock 前复权与 findesk 前复权的**基准日不同**，导致绝对价整体缩放（各标的 1%~15%），
但**逐日涨跌形态完全一致**（时段内偏差标准差 < 1.3%，见 evidence.md）。RQGM 回测基于收益率
序列，不受整体缩放影响。若某段需与 findesk 历史严格拼接，用同一复权口径重拉该段即可。
