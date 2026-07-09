# Mode: greenfield

## 判定

**greenfield** —— `~/workspaces/stock_data/` 是空目录（仅有一个未填充的 `.codegraph/`），
无源码、无 git、无构建系统。这是一个全新项目。

## 依据

- 目标是**新建**一个本地数据服务，而非修改现有生产系统。
- 虽然要"复刻" findesk 的行为，但复刻对象是外部付费 API，其接口契约通过逆向
  `super-trader-rqgm` 得到（见 findings.md），本项目从零实现。
- 有现成开源蓝本（a-stock-data / go-stock），但采用"参考自研"而非"改造它们的仓库"，
  所以本仓库仍是 greenfield。

## 对流程门的影响

- 需要 Stage 5a 能力地图（greenfield 强制）。
- 首个能力（daily-history）的 change 文件夹 spec 即该能力的**首次定义**，无需 baseline。
- 因涉及**替换外部依赖**，Stage 7 审批门需明确：本地服务的接口契约一旦定稿，
  即成为 super-trader-rqgm 的 drop-in 契约（高风险契约，需用户确认）。

## 与 super-trader-rqgm 的关系

本项目是 greenfield，但它的"验收"部分依赖对 super-trader-rqgm 的 **brownfield 式**
切换验证（drop-in 冒烟）。该切换属于**另一次**、在 super-trader-rqgm 仓库内进行的变更，
不在本 change 的源码编辑范围（本 change 只交付 stock_data 项目本身 + 一份切换指引）。
