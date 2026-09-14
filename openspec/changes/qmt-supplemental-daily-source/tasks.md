# Tasks

- [x] 1. `stockdata/fetch_qmt.py`:QmtChannelClient(status / history_fulldata,无池回退)+ `sync_qmt_daily()`(校验、停牌证据、upsert、coverage 验证)
- [x] 2. `scripts/sync_qmt_daily.py`:CLI(--codes-file / --start / --db),错误分级退出码
- [x] 3. `stockdata/qmt_account_capture.py` + `scripts/capture_qmt_account.py`:0600 密封账户快照 + verify
- [x] 4. 测试:`tests/test_fetch_qmt.py`、`tests/test_qmt_account_capture.py`(hermetic,假传输)
- [x] 5. `scripts/daily_sync.sh`:追加非阻塞 QMT 阶段
- [ ] 6. 真实通道冒烟:诊断 + 3 标的同步进临时库 + 比对 baostock qfq 重叠区间
- [ ] 7. 真实同步:全面板 QMT 增量入库(主库)
- [ ] 8. `docs/technical-manual.md` 数据源章节补 QMT 身份说明
- [ ] 9. codex 交叉审查(sol / high effort)→ 修复 → 复审
- [ ] 10. 合并 main 并 push
