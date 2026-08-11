"""Bind a prospective capture invocation to one registered RQGM panel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import hashlib
import json
from pathlib import Path

from stockdata.forward_panel_capture import CaptureSpec, capture_phase


_REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/1"
_EXPECTED_KEYS = {
    "schema_version",
    "registered_at",
    "as_of",
    "symbols",
    "sessions",
    "source",
    "adjustment_mode",
    "adjustment_version",
    "panel_sha256",
    "workspace_count",
    "outcome_feedback_used",
    "status",
}


class RegisteredPanelCaptureError(ValueError):
    """Raised when a capture request does not exactly match its registration."""


def _read_registration(path: str | Path) -> Mapping[str, object]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise RegisteredPanelCaptureError("registration_file must name a regular file")
    try:
        raw = candidate.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegisteredPanelCaptureError("registration_file must contain JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _EXPECTED_KEYS:
        raise RegisteredPanelCaptureError("registration_file schema is incomplete")
    return value


def _texts(value: object, field: str, count: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != count:
        raise RegisteredPanelCaptureError(f"registration {field} has the wrong size")
    result = tuple(value)
    if any(not isinstance(item, str) or not item or item.strip() != item for item in result):
        raise RegisteredPanelCaptureError(f"registration {field} is invalid")
    if len(set(result)) != len(result):
        raise RegisteredPanelCaptureError(f"registration {field} has duplicates")
    return result


def _panel_sha256(symbols: tuple[str, ...], sessions: tuple[str, ...]) -> str:
    cells = sorted(f"{symbol}@{day}" for symbol in symbols for day in sessions)
    return hashlib.sha256(
        json.dumps(cells, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def capture_spec_from_registration(
    registration_file: str | Path,
    *,
    database: str | Path,
    effective_date: str,
) -> CaptureSpec:
    """Return a capture spec only for an exact, pending registered session."""

    registration = _read_registration(registration_file)
    if registration["schema_version"] != _REGISTRATION_SCHEMA:
        raise RegisteredPanelCaptureError("registration has an unsupported schema")
    if registration["outcome_feedback_used"] is not False:
        raise RegisteredPanelCaptureError("registration must be outcome blind")
    if registration["status"] != "AWAITING_FULL_SNAPSHOT_READINESS":
        raise RegisteredPanelCaptureError("registration is not pending capture")
    if registration["adjustment_mode"] != "raw":
        raise RegisteredPanelCaptureError("registered capture requires raw adjustment")
    if not isinstance(registration["source"], str) or not registration["source"]:
        raise RegisteredPanelCaptureError("registration source is invalid")
    if (
        not isinstance(registration["adjustment_version"], str)
        or not registration["adjustment_version"]
    ):
        raise RegisteredPanelCaptureError("registration adjustment version is invalid")
    if registration["workspace_count"] != 36:
        raise RegisteredPanelCaptureError("registration workspace count is invalid")

    symbols = _texts(registration["symbols"], "symbols", 12)
    sessions = _texts(registration["sessions"], "sessions", 3)
    if sessions != tuple(sorted(sessions)):
        raise RegisteredPanelCaptureError("registration sessions are not sorted")
    try:
        session_dates = tuple(date.fromisoformat(item).isoformat() for item in sessions)
        requested_date = date.fromisoformat(effective_date).isoformat()
    except ValueError as exc:
        raise RegisteredPanelCaptureError("registration or requested date is invalid") from exc
    if any(date.fromisoformat(item).weekday() >= 5 for item in session_dates):
        raise RegisteredPanelCaptureError("registration contains a non-trading weekday")
    if requested_date not in session_dates:
        raise RegisteredPanelCaptureError("requested date is not registered")
    if registration["panel_sha256"] != _panel_sha256(symbols, sessions):
        raise RegisteredPanelCaptureError("registration panel identity drifted")

    return CaptureSpec(
        database=Path(database).expanduser(),
        effective_date=requested_date,
        symbols=symbols,
        source=registration["source"],
        adjustment_version=registration["adjustment_version"],
    )


def capture_registered_panel(
    registration_file: str | Path,
    *,
    database: str | Path,
    effective_date: str,
    phase: str,
) -> list[dict[str, object]]:
    """Run one existing capture phase after binding it to a registered panel."""

    if phase not in {"pre_open", "post_close"}:
        raise RegisteredPanelCaptureError("phase must be pre_open or post_close")
    return capture_phase(
        capture_spec_from_registration(
            registration_file, database=database, effective_date=effective_date
        ),
        phase,
    )
