# stockdata

可靠的本地 A 股日线历史行情服务。以多源交叉验证的方式提供前复权日线与实时行情，
本地 SQLite 增量缓存、离线可读，开箱即用、无需 API key。

作为 `ffd.findesk.cn`（FFD MCP，付费且不稳定）的 **drop-in 替换**，
以同名 MCP 工具 `ffd_query` / `ffd_quote_history` 供 super-trader-rqgm 等项目调用。

## 特性

- **多源交叉验证**：日线主源 baostock（前复权）+ 通达信 mootdx/pytdx 交叉 + 腾讯实时今日 bar；
  经 20 股实网验证，收盘价/OHLC 多源一致，与实时价 100% 吻合。
- **增量缓存**：本地 SQLite 存 OHLCV，`(code, date)` 唯一。每次请求只对已覆盖区间左右两端的
  缺失日期发起网络拉取，命中缓存的区间零网络开销；断网时仍可读历史。
- **今日实时叠加**：请求区间覆盖到今天时，用腾讯实时 bar 覆盖/追加当日数据（baostock 盘中未定）。
- **三种接入方式**：Python API、兼容 CLI（列式 JSON 到 stdout）、stdio MCP server。
- **无需 API key**，纯公开数据源。

## 安装

```bash
cd stock_data
python3 -m venv .venv
.venv/bin/pip install -e .          # 核心依赖：baostock/mootdx/requests/pandas
.venv/bin/pip install -e ".[mcp]"   # 需要 MCP server 时
```

要求 Python >= 3.9。

## 用法

代码格式接受 `600519` / `600519.SH` / `sh600519`，内部自动归一化；日期 `YYYY-MM-DD`，
`end_date` 省略默认今天。

### 1. Python API

```python
import stockdata as sd

sd.get_daily("600519.SH", "2024-01-01", "2024-06-30")   # dict，列式并行数组
sd.get_daily_df("600519.SH", "2024-01-01", "2024-06-30") # DataFrame
sd.get_realtime("600519.SH")                             # 实时 bar，非交易时段返回 None
sd.cross_check("600519.SH", "2026-06-01", "2026-07-08")  # 多源交叉验证
```

### 2. CLI

输出 findesk 格式列式 JSON 到 stdout，供下游脚本零改造对接：

```bash
.venv/bin/stockdata-cli history --code 600519.SH --start 2024-01-01 --end 2024-06-30
.venv/bin/stockdata-cli quote_history --codes 600519.SH,000001.SZ --start 2024-01-01 --end 2024-06-30
.venv/bin/stockdata-cli realtime --code 600519.SH
```

### 3. MCP server

```bash
.venv/bin/stockdata   # 启动 stdio MCP server，暴露 ffd_query / ffd_quote_history
```

在调用方 MCP 配置里注册该 server（command 指向 `.venv/bin/stockdata`）。工具名与 findesk 一致，
切换 server 指向即可 drop-in。

缓存 DB 默认 `~/.stockdata/cache.sqlite`，可用环境变量 `STOCKDATA_DB` 覆盖。

## 架构

```
请求 → 归一化代码 → 算缺口(missing_gaps) → 缺口拉主源(baostock，失败兜底 mootdx) → 写缓存(upsert)
     → 读缓存区间(get_range) → 若覆盖到今天则叠加腾讯实时 bar → 返回
```

| 层 | 文件 | 职责 |
|---|---|---|
| 拉取 | `fetch_baostock.py` / `fetch_mootdx.py` / `fetch_tencent.py` | 各数据源适配 |
| 缓存 | `cache.py` | SQLite 存储、缺口计算、增量 upsert |
| 编排 | `service.py` | 多源 + 缓存 + 增量的流程编排 |
| 交叉验证 | `consensus.py` | 多源共识收盘价、离群检测 |
| 对外 | `api.py` / `cli.py` / `server.py` | Python API / CLI / MCP 三种入口 |

## 测试

```bash
.venv/bin/pytest          # 离线用例（默认跳过 network 标记）
.venv/bin/pytest -m network  # 需真实网络/数据源的用例
```

## 许可与致谢

- 本项目代码见 `stockdata/`。详细 API 说明见 [`API.md`](API.md)，
  切换指引见 [`docs/MIGRATION-super-trader-rqgm.md`](docs/MIGRATION-super-trader-rqgm.md)。
- `vendor/superpowers/` 为开发所遵循的方法论技能库快照（MIT License，
  上游 https://github.com/obra/superpowers），详见 `vendor/superpowers/VENDOR.md`。
