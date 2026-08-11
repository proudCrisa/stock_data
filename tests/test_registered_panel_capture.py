from __future__ import annotations

import json

import pytest

from stockdata.cli import build_params
import stockdata.registered_panel_capture as registered_panel_capture
from stockdata.registered_panel_capture import (
    RegisteredPanelCaptureError,
    capture_registered_panel,
    capture_spec_from_registration,
)


SYMBOLS = [
    "000001.SZ",
    "000333.SZ",
    "000725.SZ",
    "000858.SZ",
    "002415.SZ",
    "300750.SZ",
    "600030.SH",
    "600036.SH",
    "600276.SH",
    "600519.SH",
    "601166.SH",
    "601318.SH",
]
SESSIONS = ["2026-08-12", "2026-08-13", "2026-08-14"]


def _registration(tmp_path, **changes):
    cells = sorted(f"{symbol}@{day}" for symbol in SYMBOLS for day in SESSIONS)
    import hashlib

    payload = {
        "schema_version": "rqgm-forward-panel-registration/1",
        "registered_at": "2026-08-11T23:14:47+08:00",
        "as_of": "2026-08-11",
        "symbols": SYMBOLS,
        "sessions": SESSIONS,
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
        "panel_sha256": hashlib.sha256(
            json.dumps(cells, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "ascii"
            )
        ).hexdigest(),
        "workspace_count": 36,
        "outcome_feedback_used": False,
        "status": "AWAITING_FULL_SNAPSHOT_READINESS",
    }
    payload.update(changes)
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_registered_capture_binds_exact_pending_panel(tmp_path) -> None:
    spec = capture_spec_from_registration(
        _registration(tmp_path), database=tmp_path / "evidence.sqlite", effective_date="2026-08-12"
    )

    assert spec.symbols == tuple(SYMBOLS)
    assert spec.source == "tencent"
    assert spec.effective_date == "2026-08-12"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sessions": ["2026-08-12", "2026-08-13", "2026-08-16"]}, "non-trading"),
        ({"outcome_feedback_used": True}, "outcome blind"),
        ({"status": "READY"}, "not pending"),
    ],
)
def test_registered_capture_rejects_drifted_panel(tmp_path, changes, message) -> None:
    with pytest.raises(RegisteredPanelCaptureError, match=message):
        capture_spec_from_registration(
            _registration(tmp_path, **changes),
            database=tmp_path / "evidence.sqlite",
            effective_date="2026-08-12",
        )


def test_registered_capture_rejects_unregistered_day(tmp_path) -> None:
    with pytest.raises(RegisteredPanelCaptureError, match="not registered"):
        capture_spec_from_registration(
            _registration(tmp_path), database=tmp_path / "evidence.sqlite", effective_date="2026-08-15"
        )


def test_registered_capture_delegates_bound_spec_to_existing_capture(tmp_path, monkeypatch) -> None:
    captured = {}

    def capture(spec, phase):
        captured["spec"] = spec
        captured["phase"] = phase
        return [{"result": "captured"}]

    monkeypatch.setattr(registered_panel_capture, "capture_phase", capture)

    assert capture_registered_panel(
        _registration(tmp_path),
        database=tmp_path / "evidence.sqlite",
        effective_date="2026-08-12",
        phase="pre_open",
    ) == [{"result": "captured"}]
    assert captured["spec"].effective_date == "2026-08-12"
    assert captured["phase"] == "pre_open"


def test_registered_capture_cli_params() -> None:
    assert build_params(
        [
            "registered-panel-capture",
            "--registration-file", "/tmp/panel.json",
            "--database", "/tmp/evidence.sqlite",
            "--date", "2026-08-12",
            "--phase", "pre_open",
        ]
    ) == {
        "kind": "registered_panel_capture",
        "registration_file": "/tmp/panel.json",
        "database": "/tmp/evidence.sqlite",
        "effective_date": "2026-08-12",
        "phase": "pre_open",
    }
