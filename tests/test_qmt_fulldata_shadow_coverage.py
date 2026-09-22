import json

import pytest

from stockdata import qmt_fulldata_shadow_coverage as coverage


def test_missing_registered_captures_fail_closed_without_coverage_output(tmp_path):
    registration = {"schema_version": "qmt-fulldata-shadow-registration/1", "registered_at": "2026-09-15T02:16:14+08:00", "capture_revision": "a" * 40, "target_asof": "2026-09-14", "start": "2025-06-25", "end": "2026-09-14", "count": 300, "period": "1d", "authority_grade": "shadow", "decision_eligible": False, "actions": [], "permitted_use": "offline_review_only", "requests": [{"symbol": "000300.SH", "adjustment": "raw"}] * 23}
    (tmp_path / "registration.json").write_bytes(coverage._canonical(registration))
    (tmp_path / "execution-identity-receipt.json").write_text("{}")
    (tmp_path / "captures").mkdir()
    (tmp_path / "coverage").mkdir()
    with pytest.raises(coverage.QmtFulldataShadowCoverageError):
        coverage.write_coverage(tmp_path)
    assert list((tmp_path / "coverage").iterdir()) == []
