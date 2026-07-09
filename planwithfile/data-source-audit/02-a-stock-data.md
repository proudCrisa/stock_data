# 数据源审计 — a-stock-data

> 状态：源码审计完成（Explore agent）。接口存活需实测（CHANGELOG 显示接口极易漂移）。
> 日期：2026-07-08

## 定位

A 股全栈数据工具包，**纯文档**（单个 SKILL.md 2648 行，内嵌 Python，无 .py 文件、无测试、无 CI）。
供 AI 抄写/exec。10 层架构、40 端点、13 数据源。**几乎全部免费无 key**（仅 iwencai 需 key）。

**用途定位**（用户明确）：**集各家所长充实数据源**，主要吸收 daily 没有的**深度决策数据**。

## 可扩充深度能力清单（daily 纯行情源没有的）

| 能力 | 源 | 需 key | 可靠性 |
|---|---|---|---|
| 研报列表+PDF+三年EPS一致预期 | 东财 reportapi + pdf.dfcfw.com | 否 | 稳(实测47928篇) |
| 龙虎榜席位TOP5+机构 | 东财 datacenter RPT_DAILYBILLBOARD | 否 | 稳 |
| 全市场龙虎榜 | 东财 datacenter | 否 | 稳 |
| 涨停/炸板/跌停/昨涨停四池(连板/封板/炸板/晋级率) | 东财 push2ex getTopicZT/ZB/DT | 否 | v3.3实测 |
| 涨停揭秘(原因/封板成功率/板型) | 同花顺 data.10jqka | 否 | 稳 |
| 打板情绪(炸板率/连板梯队) | 本地派生 | 否 | 派生 |
| ETF期权 T型报价/希腊字母/IV | 新浪 hq.sinajs+futures | 否 | 交易所IV不自算 |
| 北向资金分钟流向 | 同花顺 hexin hsgtApi | 否 | 东财北向2024-08断供→同花顺 |
| 融资融券 | 东财 datacenter | 否 | 稳 |
| 大宗交易 | 东财 RPT_DATA_BLOCKTRADE | 否 | v3.1修 |
| 股东户数(筹码集中度代理) | 东财 RPT_HOLDERNUMLATEST | 否 | 稳(⚠无真cyq成本分布) |
| 分红送转 | 东财 RPT_SHAREBONUS_DET | 否 | 稳 |
| 个股资金流(分钟/120日) | 东财 push2/push2his fflow | 否 | 住宅IP偶风控 |
| 概念/板块归属 | 东财 push2 slist | 否 | v3.2.2替换失效百度 |
| 行业板块排名 | 东财 push2 clist | 否 | v3.0换东财 |
| 限售解禁日历 | 东财 RPT_LIFT_STAGE | 否 | 稳 |
| 个股新闻/全球7x24资讯 | 东财 search-api/np-weblist | 否 | v3.2.1修 |
| 新浪财报三表 | quotes.sina.cn | 否 | v3.2.1修 |
| 巨潮公告全文 | cninfo hisAnnouncement | 否 | v3.2.2修orgId |
| 互动易问答 | irm.cninfo | 否 | 稳 |
| 同花顺热榜/东财人气榜 | dq.10jqka/emappdata | 否 | 稳 |
| iwencai NL语义搜研报 | openapi.iwencai.com | **是**(+X-Claw) | 唯一需key |

## CHANGELOG 失效记录（证明爬虫接口极易漂移 → 必须实测）

1. 财联社 cls.cn 全面 404（迁 Next.js）→ 弃用，改东财全球资讯
2. 百度 PAE getrelatedblock 概念板块失效（ResultCode 10003）→ 换东财 slist
3. 百度 PAE fundflow 资金流下线（返 null）→ 换东财 push2
4. 大宗交易 RPT_DATA_OCCURTRADE 报表下线 → RPT_DATA_BLOCKTRADE
5. 龙虎榜机构 RPT_ORGANIZATION_BUSSINESS 下线 → 席位筛选
6. 东财北向净买额 2024-08 起断供 → 同花顺 + 本地 CSV 缓存
7. 同花顺行业板块加 401 反爬 → 东财 push2
8. 巨潮公告 orgId 硬编码错 → 动态 szse_stock.json 映射
9. 东财全球资讯缺 req_trace 返 403
10. 住宅 IP 间歇风控（HTTP 000/空）

## 防封机制（可移植）

- **em_get()**：所有东财请求唯一入口。串行限流(间隔≥1s+抖动) + Keep-Alive 会话复用 +
  Retry(429/5xx，403 不重试)。阈值：>5/s、并发≥10、1min≥200 触发封 IP。
- **tdx_client()**：规避 mootdx 0.11.x BESTIP bug，顺序探测内置 10 台 TCP 服务器 + 三级 fallback。

## 关键红旗

- ⚠ **纯文档无代码无测试无 CI** —— 接口失效了没有兜底，10 条失效史证明这些爬虫接口**极易漂移**。
- ⚠ 大量依赖东财单一家（push2/datacenter/reportapi）—— 东财一封，深度数据大面积失效。
- ⚠ 无真正筹码成本分布(cyq)，仅用股东户数代理。

## 对最终方案的启示

深度数据能力**逐个采纳前必须实测当前存活**，且要包进有测试/CI 的代码（不能停留在文档）。
优先采纳"稳"标记且非东财独家的能力。防封机制(em_get/tdx_client)直接移植。
