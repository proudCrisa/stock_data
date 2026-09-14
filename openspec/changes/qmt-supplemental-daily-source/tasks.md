# Tasks

- [x] 1. `stockdata/fetch_qmt.py`:QmtChannelClient(status / snapshot_front / history_fulldata,无池回退)+ 同步函数(校验、停牌证据、upsert、coverage 验证)
- [x] 2. `scripts/sync_qmt_daily.py`:CLI(--codes-file / --db / --fulldata / --timeout),错误分级退出码
- [x] 3. `stockdata/qmt_account_capture.py` + `scripts/capture_qmt_account.py`:0600 密封账户快照 + verify(实盘验证通过)
- [x] 4. 测试:`tests/test_fetch_qmt.py`、`tests/test_qmt_account_capture.py`(hermetic,全绿)
- [x] 5. `scripts/daily_sync.sh`:追加非阻塞 QMT 阶段
- [x] 6. 真实通道冒烟:诊断 + 3 标的比对 baostock qfq(600519.SH 14 重叠日零偏差)
- [x] 7. 真实同步:快照路径入库;单位 bug 数据修复后 qmt 身份 25 代码(池∩面板 24 + fulldata 回填 1),全部单因子版本、股数口径
- [x] 8. `docs/technical-manual.md` 数据源章节补 QMT 身份说明(实库口径)
- [x] 9. codex 交叉审查(sol / high effort):16 轮、44 个发现全部修复;第 16 轮零发现收敛
- [ ] 10. 合并 main 并 push(待最终全量回归绿)
