# 数据源审计 — go-stock

> 状态：源码审计完成（Explore agent）。日期 2026-07-08。
> 定位：Go/Wails 桌面 AI 股票工具，"数据全部保留本地"，支持 A/港/美股。
> 对本项目主要是借鉴价值，不直接复用 Go 代码。

## 数据源清单（均免费，多为逆向公开接口）

| 源 | URL/协议 | 数据 | key |
|---|---|---|---|
| 腾讯 | qt.gtimg.cn / web.ifzq.gtimg.cn fqkline | 实时五档、K线 | 否(需 Referer) |
| 新浪 | hq.sinajs.cn / quotes.sina.cn | 实时、基金、K线 | 否(GB18030) |
| 东财 | push2his kline / push2ex 异动 / datacenter F10 / 板块资金 | K线/异动/财务/资金流 | 否(写死 ut) |
| 通达信 | TCP 二进制(gotdx) | K线、集合竞价 | 否 |
| 财联社 | cls.cn telegraph(带 sign) | 电报快讯 | 否 |
| 问财 | openapi.iwencai.com(Bearer)/免key网关 | NL 选股 | 官方API需key |
| 雪球 | xueqiu.com(chromedp 取 cookie) | 资讯 | 否 |
| 其他 | tushare(需token)、TradingView、韭研、百度/必应 | 基础/资讯 | 部分需 |

## 本地存储方案（对本项目最有借鉴意义）

SQLite（glebarez/sqlite）+ GORM，默认 data/stock.db。调优：WAL、busy_timeout=10000、
synchronous=NORMAL、512MB cache、连接池限 5。AutoMigrate ~38 张表（StockInfo 五档 /
StockChangeHistory 异动 / BKFundFlow 资金流 / Telegraph / SentimentResult / MarketStatistic）。

## 独特能力（可扩充借鉴）

1. 本地金融情感词典（go:embed + 用户词典）：离线情绪评分，不调 LLM，可移植 Python。
2. 东财 push2ex 全市场异动 + 去重落库：行情之外的增量。
3. 涨跌报警：比对关注股阈值 → 系统通知 + 飞书/钉钉。
4. AI 资讯：接 OpenAI 兼容端点做摘要 + MCP 工具。

## 实时性

无 WebSocket。robfig/cron（秒级）+ Wails EventsEmit。先判交易时段再拉、仅价格变动才推。

## 测试

真实测试：app_test.go(13) + backend ~20 个 *_test.go（6777 行），多为真实网络集成测试。

## 可借鉴点（对本项目）

1. SQLite WAL + 连接池调优 → 缓存层采纳。
2. 交易时段闸门再拉数据 → 省流量、避免非交易时段脏数据。
3. 本地情感词典离线评分 → 未来情绪面增强可移植。
4. 东财 push2ex 异动去重落库 → 深度数据扩充候选。

> 注意：go-stock 接口多为逆向非官方，与 daily 的腾讯/新浪/东财/通达信大量重叠——
> 行情源不构成增量，增量在"存储调优 + 异动 + 情绪词典"。
