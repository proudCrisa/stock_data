"""Bind a prospective capture invocation to one registered RQGM panel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path

from stockdata.collector_continuity import (
    CollectorContinuityError,
    execute_registered_collector_phase,
)
from stockdata.forward_panel_capture import capture_phase  # noqa: F401 - audit sentinel
from stockdata.future_panel_registration import (
    REGISTRATION_SCHEMA,
    TRUSTED_LOCAL_AUTHORITY_MODE,
    TRUSTED_LOCAL_REGISTRATION_SCHEMA,
    reverify_registration_prerequisites,  # noqa: F401 - retained compatibility surface
)


_EXPECTED_KEYS = {
    "schema_version",
    "registered_at",
    "as_of",
    "symbols",
    "sessions",
    "source",
    "adjustment_mode",
    "adjustment_version",
    "database_path",
    "panel_sha256",
    "workspace_count",
    "outcome_feedback_used",
    "status",
    "prerequisite_files",
    "prerequisites",
    "prerequisites_sha256",
}
_TRUSTED_LOCAL_EXPECTED_KEYS = _EXPECTED_KEYS | {"authority_mode"}
class RegisteredPanelCaptureError(ValueError):
    """Raised when a capture request does not exactly match its registration."""


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegisteredPanelCaptureError("registration_file has duplicate keys")
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise RegisteredPanelCaptureError("registration_file is not canonical JSON") from exc


def _read_registration(path: str | Path) -> Mapping[str, object]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise RegisteredPanelCaptureError("registration_file must name a regular file")
    try:
        raw = candidate.read_bytes()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegisteredPanelCaptureError("registration_file must contain JSON") from exc
    if not isinstance(value, Mapping):
        raise RegisteredPanelCaptureError("registration_file schema is incomplete")
    if raw != _canonical(dict(value)):
        raise RegisteredPanelCaptureError("registration_file must use canonical JSON bytes")
    schema = value.get("schema_version")
    if schema not in {REGISTRATION_SCHEMA, TRUSTED_LOCAL_REGISTRATION_SCHEMA}:
        raise RegisteredPanelCaptureError("registration has an unsupported schema")
    expected_keys = (
        _EXPECTED_KEYS if schema == REGISTRATION_SCHEMA else _TRUSTED_LOCAL_EXPECTED_KEYS
    )
    if set(value) != expected_keys or (
        schema == TRUSTED_LOCAL_REGISTRATION_SCHEMA
        and value.get("authority_mode") != TRUSTED_LOCAL_AUTHORITY_MODE
    ):
        raise RegisteredPanelCaptureError("registration_file schema is incomplete")
    return value


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
    _read_registration(registration_file)
    try:
        outcomes = execute_registered_collector_phase(
            registration_file,
            database=database,
            effective_date=effective_date,
            phase=phase,
        )
    except CollectorContinuityError as exc:
        raise RegisteredPanelCaptureError(str(exc)) from exc
    return [
        {
            "step_id": outcome.step_id,
            "step_ordinal": outcome.step_ordinal,
            "attempt_id": outcome.attempt_id,
            "terminal_event_sha256": outcome.terminal_event_sha256,
            "terminal_event_type": outcome.terminal_event_type,
            "classification": outcome.classification,
            "retryable": outcome.retryable,
            "process_result_known": outcome.process_result_known,
            "returncode": outcome.returncode,
            "raw_class": outcome.raw_class,
        }
        for outcome in outcomes
    ]
