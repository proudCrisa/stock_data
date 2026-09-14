"""hermetic 测试:QMT 账户密封捕获。"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from stockdata.qmt_account_capture import (
    SCHEMA_VERSION,
    QmtAccountCaptureError,
    capture_qmt_account,
    verify_qmt_account_capture,
)

_SNAPSHOT = {
    "generated": "2026-09-14T15:00:00",
    "account": {
        "sections": {
            "ACCOUNT": [{"m_dBalance": 1000000.0, "m_dAvailable": 250000.5}],
            "POSITION": [
                {"m_strInstrumentID": "600519", "m_nVolume": 100},
                {"m_strInstrumentID": "000001", "m_nVolume": 2000},
            ],
        }
    },
}


class TestCapture:
    def test_happy_path(self, tmp_path):
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        payload = json.loads(path.read_text())
        assert payload["schema"] == SCHEMA_VERSION
        assert payload["account"]["m_dBalance"] == 1000000.0
        assert len(payload["positions"]) == 2
        assert len(payload["content_sha256"]) == 64

    def test_no_account_section_rejected(self, tmp_path):
        with pytest.raises(QmtAccountCaptureError, match="ACCOUNT"):
            capture_qmt_account({"account": {"sections": {}}},
                                output_root=tmp_path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("snapshot", [
        {"account": None},
        {"account": []},
        {"account": {"sections": []}},
        {"account": {"sections": {"ACCOUNT": "not-a-list"}}},
        {"account": {"sections": {"ACCOUNT": [{}], "POSITION": "x"}}},
    ])
    def test_malformed_containers_raise_domain_error(self, snapshot, tmp_path):
        """任何一层容器畸形都抛领域错误(而非 AttributeError 逃逸)。"""
        with pytest.raises(QmtAccountCaptureError):
            capture_qmt_account(snapshot, output_root=tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_not_dict_rejected(self, tmp_path):
        with pytest.raises(QmtAccountCaptureError):
            capture_qmt_account([], output_root=tmp_path)

    def test_short_write_looped_to_completion(self, tmp_path, monkeypatch):
        import stockdata.qmt_account_capture as mod

        real_write = os.write

        def partial_write(fd, data):
            return real_write(fd, data[:5])  # 每次最多写 5 字节

        monkeypatch.setattr(mod.os, "write", partial_write)
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        assert verify_qmt_account_capture(path)  # 内容完整、哈希自洽

    def test_write_failure_rolls_back_artifact(self, tmp_path, monkeypatch):
        import stockdata.qmt_account_capture as mod

        def broken_write(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(mod.os, "write", broken_write)
        with pytest.raises(OSError):
            capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        assert list(tmp_path.iterdir()) == []  # 残缺产物已回滚

    def test_no_overwrite_same_name(self, tmp_path):
        now = datetime(2026, 9, 14, 7, 0, 0, tzinfo=timezone.utc)
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path, now=now)
        # 同内容 → 同哈希 → 同名,O_EXCL 拒绝第二次写入
        with pytest.raises(FileExistsError):
            capture_qmt_account(_SNAPSHOT, output_root=tmp_path, now=now)
        # 内容变化 → 哈希变 → 新文件名,正常落盘
        other = json.loads(json.dumps(_SNAPSHOT))
        other["account"]["sections"]["ACCOUNT"][0]["m_dBalance"] = 2.0
        p2 = capture_qmt_account(other, output_root=tmp_path, now=now)
        assert p2 != path and p2.is_file()


class TestVerify:
    def test_roundtrip(self, tmp_path):
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        payload = verify_qmt_account_capture(path)
        assert payload["account"]["m_dAvailable"] == 250000.5

    def test_tamper_detected(self, tmp_path):
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        payload = json.loads(path.read_text())
        payload["account"]["m_dBalance"] = 1.0
        path.write_text(json.dumps(payload))
        with pytest.raises(QmtAccountCaptureError, match="不自洽"):
            verify_qmt_account_capture(path)

    def test_bad_perms_rejected(self, tmp_path):
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        os.chmod(path, 0o644)
        with pytest.raises(QmtAccountCaptureError, match="0600"):
            verify_qmt_account_capture(path)

    def test_bad_schema_rejected(self, tmp_path):
        path = capture_qmt_account(_SNAPSHOT, output_root=tmp_path)
        payload = json.loads(path.read_text())
        payload["schema"] = "other/0"
        path.write_text(json.dumps(payload))
        with pytest.raises(QmtAccountCaptureError, match="schema"):
            verify_qmt_account_capture(path)
