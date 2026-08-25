# WIP Codex 交叉审查记录（2026-08-25)

## 范围

三个 openspec change 的在制实现（约 4 万行 diff,36 改 + 30 新文件）:

- `bind-forward-collector-continuity`:collector 创世绑定 + append-only 连续性账本 + 崩溃恢复 fail-closed;registration `/4`;finalized 价格 append-only（仅字节等价幂等）。
- `enable-verified-provider-readiness`：九组件独立验证全通过且无阻塞才允许 `ready=true` 的正向聚合路径；从不可变数据库和绑定回执重建价格。
- `export-rqgm-pit-feature-snapshot`：只读导出 PIT 特征快照，`execution_grade=false` 必须保持。

审查方式：`codex exec`(gpt-5.5,reasoning high）对全量 diff + openspec 语义文档做源码级审查，重点为 fail-closed 漏洞、语义覆盖、跨身份混源、测试有效性。

## 结论

**未发现阻断问题，可以提交。**

Codex 重点复核路径：

- `provider_materializer` / `provider_export`:READY 只来自九组件逐项验证、空 aggregate blocker、continuity 语义校验和 export 复验；broken continuity 在 readiness 前阻断。
- `collector_continuity`:`ATTEMPT_COMPLETED` 需要 raw postcondition=`complete`、子进程结果已知、`returncode == 0`、plumbing 未失败；partial/forbidden/异常路径不会完成。
- `cache` / `sync` / `forward_capture`:collector 写入仍受 writer token、active attempt tail、nonce、ledger lock 约束；finalized daily 非幂等覆盖由 trigger 和 postcondition 阻断。
- `rqgm_feature_snapshot`：硬编码 `execution_grade=false`、`authoritative_for_execution=false`，并从 receipt response hash 与 artifact 自身字节复验，不依赖后续 DB 状态。

## 验证

- 全量 pytest 套件：100% 通过（含三个 change 全部新测试）。
- Codex 独立复跑重点子集：`test_collector_attempt_launch` / `test_collector_ledger` / `test_collector_raw_postcondition` / `test_collector_recovery` / `test_verified_provider_readiness` / `test_provider_continuity_negative_gate` / `test_rqgm_feature_snapshot`，全部通过。

备注：codex 尝试启动 `gpt-5.6-sol` 子代理对 provider readiness 做二次复核，两次等待未返回后关闭；结论基于主线源码审查与上述测试。ruff 基线（HEAD）本身有 166 个错误，项目未强制 lint,WIP 新增项为同类型风格问题，未处理。
