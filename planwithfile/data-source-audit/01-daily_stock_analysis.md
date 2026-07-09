# 数据源审计 — daily_stock_analysis

> 状态：源码审计完成（Explore agent）。实网数值验证进行中（见 verification.md）。
> 日期：2026-07-08

## 定位

生产级**多数据源行情系统**（Python 3.10+）。`data_provider/` 下 11 个 fetcher，
`DataFetcherManager` 做优先级 failover + 熔断（CircuitBreaker，连续失败 3 次冷却 ~5min）+
防封节流。覆盖 A股/港/美/日/韩/台。统一列名 `date/open/high/low/close/volume/amount/pct_chg`。

**结论：行情类（日线+实时）数据源，daily 已是全集，不缺源。** a-stock-data / go-stock
不再补行情源，只补深度数据与交叉验证。

## 数据源清单（priority 越小越先试）

| 源 | 类型 | 需 key | 复权 | 可持续 | 备注 |
|---|---|---|---|---|---|
| Tencent P0 | HTTP web.ifzq.gtimg.cn | 否 | qfq 前复权 | ★高·免费 | 实测不封 IP |
| Efinance P0 | SDK(东财HTTP爬虫) | 否 | 前复权 fqt=1 | 中·免费易限流 | ETF量额曾恒0(#527) |
| AkShare P1 | SDK(HTTP爬虫) | 否 | qfq | 中·免费接口易变 | 美股复权失效(#311) |
| Tushare P2(动态提升) | HTTP API | **是 token+积分** | 支持 | 高·需token | 积分不足即失效 |
| Pytdx(通达信) P2 | **TCP直连**(内置8台) | 否 | 原始不复权 | 中·服务器不稳 | 内置冷却 |
| TickFlow P2 | HTTP API | **是 key** | ⚠默认 none 不复权 | 付费 | 混用致价格不一致 |
| Baostock P3 | SDK | 否 | 前复权 adjustflag=2 | ★高·免费 | 日线补充,登录制 |
| AlphaVantage P3 | HTTP | **是 key** | 支持 | 付费(美股) | 速率限 |
| Finnhub P2 | HTTP/SDK | **是 key** | — | 付费(美股) | |
| YFinance P4 | SDK | 否 | auto_adjust | 中·免费(港美兜底) | 速率限 |
| Longbridge P5 | SDK(HTTP+WS) | **是 OAuth** | ForwardAdjust | 付费(港美优先) | OAuth 易过期 |

**免费无 key（可持续核心）**：Tencent / Efinance / AkShare / Pytdx / Baostock / YFinance。
**A股默认链**：Tushare(有token)→Efinance/Tencent→AkShare→Pytdx→Baostock→YFinance。
**实时行情默认**：`tencent,akshare_sina,efinance,akshare_em`（有 token 前插 tushare）。

## 运行/测试

- Python 3.10+；`pip install -r requirements.txt`；配 `.env`（模板 `.env.example` 48KB）。
- 入口：`python main.py`（--dry-run/--stocks/--market-review/--schedule/--serve）；`uvicorn server:app`。
- pytest（`setup.cfg`，markers: unit/integration/network）。33 个数据源测试。

## 可靠性红旗（源码审计发现）

1. ⚠ **测试以离线 mock 契约为主，缺乏对实网复权数值正确性的持续校验** —— 交易决策最大盲点。
2. ⚠ AkShare 美股日线复权失效(#311)；Efinance ETF 量额恒 0(#527)、beg 参数报错(#541)。
3. ⚠ TickFlow 默认不复权，与其它 qfq 源混用致价格不一致（须显式设 forward）。
4. ⚠ Tushare 需 token **且**积分；Longbridge OAuth 无头环境易过期。
5. ⚠ 免费源共性：efinance/akshare 走东财爬虫，上游变更/限流即批量失败；pytdx TCP 不稳。

## 对最终方案的启示

daily 的**多源 failover 架构**是本次要产出的"可靠本地数据源"的地基。缺的是：
①实网数值正确性的持续交叉验证（本审计要补的核心）；②深度决策数据（从 a-stock-data 吸收）。
