"""Run one complete prospective panel capture window."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
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
        prices = tuple(
            base
            + [
                "forward-capture",
                "--database",
                str(spec.database),
                "--codes",
                ",".join(spec.symbols),
                "--start",
                spec.effective_date,
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
