"""Run one complete prospective panel capture window."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from collections.abc import Callable


class ForwardPanelCaptureError(RuntimeError):
    """Raised when one required prospective capture step fails."""


@dataclass(frozen=True)
class CaptureSpec:
    database: Path
    effective_date: str
    symbols: tuple[str, ...]
    source: str
    adjustment_version: str


def _base_command(spec: CaptureSpec) -> list[str]:
    return [
        sys.executable,
        "-m",
        "stockdata.cli",
    ]


def _cohort_start(spec: CaptureSpec) -> str:
    if not spec.database.exists():
        return spec.effective_date
    try:
        with sqlite3.connect(spec.database) as connection:
            row = connection.execute(
                "SELECT spec_json FROM forward_capture_cohort WHERE singleton=1"
            ).fetchone()
    except sqlite3.OperationalError:
        return spec.effective_date
    if row is None:
        return spec.effective_date
    cohort = json.loads(str(row[0]))
    expected = {
        "symbols": list(sorted(spec.symbols)),
        "source": spec.source,
        "adjustment_mode": "raw",
        "adjustment_version": spec.adjustment_version,
    }
    if any(cohort.get(key) != value for key, value in expected.items()):
        raise ForwardPanelCaptureError("forward capture cohort identity drift")
    return str(cohort["start"])


def commands_for_phase(spec: CaptureSpec, phase: str) -> tuple[tuple[str, ...], ...]:
    """Return the ordered commands required for one PIT capture window."""
    base = _base_command(spec)
    context = tuple(
        base
        + [
            "forward-context-capture",
            "--database",
            str(spec.database),
            "--date",
            spec.effective_date,
        ]
    )
    if phase == "pre_open":
        corporate_actions = tuple(
            base
            + [
                "forward-corporate-actions-capture",
                "--database",
                str(spec.database),
                "--date",
                spec.effective_date,
            ]
        )
        return context, corporate_actions
    if phase == "post_close":
        cohort_start = _cohort_start(spec)
        prices = tuple(
            base
            + [
                "forward-capture",
                "--database",
                str(spec.database),
                "--codes",
                ",".join(spec.symbols),
                "--start",
                cohort_start,
                "--end",
                spec.effective_date,
                "--source",
                spec.source,
                "--adjustment-version",
                spec.adjustment_version,
            ]
        )
        return context, prices
    raise ValueError("phase must be pre_open or post_close")


def capture_phase(
    spec: CaptureSpec,
    phase: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, object]]:
    """Execute all commands in order and stop before a partial phase is reported ready."""
    results: list[dict[str, object]] = []
    for command in commands_for_phase(spec, phase):
        completed = runner(command, check=False, capture_output=True, text=True)
        result = {
            "command": list(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        results.append(result)
        if completed.returncode != 0:
            raise ForwardPanelCaptureError(
                json.dumps(result, ensure_ascii=True, sort_keys=True)
            )
    return results


__all__ = ["CaptureSpec", "ForwardPanelCaptureError", "capture_phase", "commands_for_phase"]
