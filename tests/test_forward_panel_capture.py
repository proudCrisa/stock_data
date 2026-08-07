from __future__ import annotations

from pathlib import Path
import sqlite3
from subprocess import CompletedProcess

import pytest

from stockdata.forward_panel_capture import (
    CaptureSpec,
    ForwardPanelCaptureError,
    capture_phase,
    commands_for_phase,
)


SPEC = CaptureSpec(
    database=Path("/tmp/evidence.sqlite"),
    effective_date="2026-08-10",
    symbols=("000001.SZ", "600519.SH"),
    source="tencent",
    adjustment_version="tencent-qt-daily-v1",
)


def test_pre_open_orders_context_before_corporate_actions() -> None:
    commands = commands_for_phase(SPEC, "pre_open")
    assert [command[3] for command in commands] == [
        "forward-context-capture",
        "forward-corporate-actions-capture",
    ]


def test_post_close_orders_context_before_prices() -> None:
    commands = commands_for_phase(SPEC, "post_close")
    assert [command[3] for command in commands] == [
        "forward-context-capture",
        "forward-capture",
    ]
    assert "--start" in commands[1]
    assert commands[1][commands[1].index("--start") + 1] == "2026-08-10"


def test_post_close_reuses_bound_cohort_start(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE forward_capture_cohort "
            "(singleton INTEGER PRIMARY KEY, spec_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO forward_capture_cohort VALUES (1, ?)",
            (
                '{"adjustment_mode":"raw","adjustment_version":'
                '"tencent-qt-daily-v1","source":"tencent",'
                '"start":"2026-07-27","symbols":'
                '["000001.SZ","600519.SH"]}',
            ),
        )
    spec = CaptureSpec(
        database=database,
        effective_date="2026-08-10",
        symbols=SPEC.symbols,
        source=SPEC.source,
        adjustment_version=SPEC.adjustment_version,
    )

    commands = commands_for_phase(spec, "post_close")

    assert commands[1][commands[1].index("--start") + 1] == "2026-07-27"


def test_post_close_rejects_bound_cohort_identity_drift(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE forward_capture_cohort "
            "(singleton INTEGER PRIMARY KEY, spec_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO forward_capture_cohort VALUES (1, ?)",
            (
                '{"adjustment_mode":"raw","adjustment_version":'
                '"tencent-qt-daily-v1","source":"tencent",'
                '"start":"2026-07-27","symbols":["000001.SZ"]}',
            ),
        )
    spec = CaptureSpec(
        database=database,
        effective_date="2026-08-10",
        symbols=SPEC.symbols,
        source=SPEC.source,
        adjustment_version=SPEC.adjustment_version,
    )

    with pytest.raises(ForwardPanelCaptureError, match="identity drift"):
        commands_for_phase(spec, "post_close")


def test_phase_stops_after_first_failed_step() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        return CompletedProcess(command, 1, stdout="", stderr="failed")

    with pytest.raises(ForwardPanelCaptureError):
        capture_phase(SPEC, "pre_open", runner=runner)
    assert len(calls) == 1
