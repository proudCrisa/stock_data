# Decisions（Stage 6 挑战门）

四视角自审。gstack 评审仅在能真正降低不确定性时启用。

## 1. 业务 / 用户价值

- **价值成立**：findesk 付费+不稳定+截断，是 super-trader-rqgm 回测/anchor 扩展的真实痛点
  （代码注释直书"付费, 仅11行截断"）。本地版补上"任意历史区间前复权"这块独有缺口。
- **范围克制正确**：只复刻实际用到的日线历史，不做 findesk 其余 9 层能力，避免过度建设。
- 结论：无需 `/office-hours` —— 问题与用户明确，无产品层不确定性。

## 2. 风险 / 失败 / 回滚

- **P1 复权口径**：百度K线是否真前复权、能否拉任意起始日、停牌如何表达 —— **最大不确定性**。
  处置：tasks Slice 3 强制"实现前真实样本核对"并写 evidence；spec 有一致性验证 requirement；
  baostock 作交叉校验兜底。→ 已降为可控。
- **P2 drop-in 契约偏差**：去双重 wrapper 会否影响 `process_ffd_data.py`。
  处置：保留外层 data 键，仅 super-trader-rqgm 侧改一行读取；契约测试 + 冒烟锁定。
- 回滚：单点封装 + 保留 FFD fallback + 已落地缓存，均可一键切回。
- 结论：风险有明确缓解，无 P0 阻塞。

## 3. 实现 / 简洁性 / 契合

- 纯 Python 同栈、以成熟蓝本(a-stock-data)自研，避免跨语言与运行时耦合。
- 三层（fetch/cache/mcp）职责清晰，无过度抽象；多源交叉校验降级为可选，避免复杂度膨胀。
- 结论：架构与 workspaces 约定一致。**是否需要 `/plan-eng-review`？** —— 见下方待定项。

## 4. 历史 / 规则 / 代码图冲突

- 与 CLAUDE.md ProofRails 一致（spec-before-code、TDD、evidence）。
- 与 super-trader-rqgm 现状一致（主源已 mootdx，本项目补历史区间，不冲突）。
- stock_data codegraph 为空（新项目），无既有代码冲突。
- 结论：无规则/历史冲突。

## 未决高影响项（带入 Stage 7 审批）

1. **复权口径策略**：主源百度K线若实测复权不达标，是否接受切 baostock 为历史主源？
2. **drop-in 边界**：确认本 change **不**编辑 super-trader-rqgm 业务代码，切换由独立变更执行。
3. 是否需要 `/plan-eng-review` 对拉取层/缓存做一次工程评审？——倾向**否**：蓝本成熟、
   架构常规，评审边际收益低；除非用户希望更谨慎。

## Gate 判定

无 P0/P1 未缓解阻塞。可进入 Stage 7 审批门。

## Stage 7 审批结论（2026-07-08）

- ✅ **批准进入实现**：从 Slice 0 骨架开始，TDD 逐切片推进。
- ✅ **复权主源授权**：百度K线实测前复权**不达标时，自动切 baostock 作历史主源**（前复权+
  停牌标记齐全），无需再次询问。达标则维持"百度K线主 + mootdx 补 + 腾讯实时"。
- ✅ **drop-in 边界确认**：本 change 不编辑 super-trader-rqgm 业务代码；切换为独立变更。
- 未选工程评审（/plan-eng-review）—— 直接实现。
