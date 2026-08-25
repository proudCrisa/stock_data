from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import (
    COLLECTOR_GENESIS_SCHEMA,
    COLLECTOR_LEDGER_EVENT_SCHEMA,
    COLLECTOR_LEDGER_MAX_BYTES,
    COLLECTOR_LEDGER_MAX_LINE_BYTES,
    COLLECTOR_LEDGER_MAX_LINES,
    COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
    COLLECTOR_STEP_STATE_SCHEMA,
    LEDGER_EVENT_TYPES,
    CollectorContinuityError,
    PhysicalFileIdentity,
    append_collector_genesis_event,
    append_collector_ledger_event,
    build_collector_genesis_ledger_event,
    build_collector_ledger_event,
    canonical_json_bytes,
    canonical_json_sha256,
    open_existing_regular_file,
    parse_collector_ledger,
)


_NON_GENESIS_TYPES = tuple(event_type for event_type in LEDGER_EVENT_TYPES if event_type != "GENESIS")
_SESSIONS = ("2099-01-05", "2099-01-06", "2099-01-07")


def _identity(path: Path, file_ino: int) -> dict[str, object]:
    return PhysicalFileIdentity(str(path.resolve()), 1, 2, 1, file_ino).to_dict()


def _genesis(tmp_path: Path) -> dict[str, object]:
    database = tmp_path / "evidence.sqlite"
    ledger = tmp_path / "evidence.sqlite.collector-ledger.jsonl"
    return {
        "schema_version": COLLECTOR_GENESIS_SCHEMA,
        "database_uuid": "a" * 64,
        "cohort_sha256": "b" * 64,
        "database_identity": _identity(database, 3),
        "ledger_identity": _identity(ledger, 4),
        "collector_schema_sha256": "9" * 64,
        "created_at": "2026-08-23T09:00:00+08:00",
    }


def _detail(event_type: str, suffix: str = "") -> dict[str, object]:
    sha = "c" * 64
    common = {
        "registration_sha256": sha,
        "database_uuid": "a" * 64,
        "state_before_sha256": "d" * 64,
    }
    if event_type == "REGISTRATION_BOUND":
        return {
            "registration_sha256": sha,
            "panel_sha256": "e" * 64,
            "sessions": list(_SESSIONS),
            "sessions_sha256": canonical_json_sha256(list(_SESSIONS)),
            "prerequisites_sha256": "0" * 64,
            "bound_at": "2026-08-23T09:01:00+08:00" + suffix,
        }
    if event_type == "SQLITE_RECOVERY_STARTED":
        return {**common, "started_at": "2026-08-23T09:01:00+08:00" + suffix}
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        return {
            **common,
            "started_at": "2026-08-23T09:01:00+08:00" + suffix,
            "completed_at": "2026-08-23T09:02:00+08:00" + suffix,
            "state_after_sha256": "1" * 64,
            "recovery_classification": "rollback-journal-recovered" + suffix,
        }
    if event_type == "SQLITE_RECOVERY_FAILED":
        return {
            **common,
            "started_at": "2026-08-23T09:01:00+08:00" + suffix,
            "failed_at": "2026-08-23T09:02:00+08:00" + suffix,
            "state_after_sha256": "1" * 64,
            "failure_classification": "unclassified" + suffix,
            "retryable": True,
        }
    attempt = {
        **common,
        "session": "2099-01-05",
        "phase": "pre_open",
        "step_id": "pre_open_context",
        "step_ordinal": 0,
        "attempt_id": "attempt-1" + suffix,
        "command_sha256": "2" * 64,
        "lease_nonce_sha256": "5" * 64,
        "started_at": "2026-08-23T09:01:00+08:00" + suffix,
        "step_state_before": _step_state("d" * 64),
        "step_raw_before": _step_raw_before(),
    }
    if event_type == "ATTEMPT_STARTED":
        return attempt
    terminal = {
        **{key: value for key, value in attempt.items() if key != "lease_nonce_sha256"},
        "state_after_sha256": "1" * 64,
        "step_state_after": _step_state("1" * 64),
        "returncode": 0,
        "stdout_sha256": "3" * 64,
        "stdout_bytes": 0,
        "stderr_sha256": "4" * 64,
        "stderr_bytes": 0,
        "process_result_known": True,
        "process_launch_state": "handle_obtained",
        "recovered": False,
        "verifier_id": "raw-postcondition-v1",
    }
    if event_type == "ATTEMPT_COMPLETED":
        return {
            **terminal,
            "completed_at": "2026-08-23T09:02:00+08:00" + suffix,
            "recovered": False,
            "verifier_id": "raw-postcondition-v1" + suffix,
        }
    if event_type == "ATTEMPT_FAILED":
        return {
            **terminal,
            "failed_at": "2026-08-23T09:02:00+08:00" + suffix,
            "failure_classification": "child_no_commit",
            "retryable": True,
        }
    raise AssertionError(event_type)


def _recovery_start_detail(
    attempt_started: dict[str, object], suffix: str = ""
) -> dict[str, object]:
    attempt = attempt_started["event"]
    assert isinstance(attempt, dict)
    return {
        "registration_sha256": attempt["registration_sha256"],
        "database_uuid": attempt["database_uuid"],
        "state_before_sha256": attempt["state_before_sha256"],
        "attempt_id": attempt["attempt_id"],
        "attempt_started_event_sha256": attempt_started["event_sha256"],
        "recovery_id": canonical_json_sha256({"recovery": suffix or "one"}),
        "recovery_kind": "hot_delete_journal",
        "journal_identity": _identity(Path("/tmp/collector-recovery.journal"), 5),
        "journal_bytes": 0,
        "journal_sha256": "e" * 64,
        "started_at": "2026-08-23T09:01:00+08:00" + suffix,
    }


def _recovery_terminal_detail(
    recovery_started: dict[str, object], event_type: str, suffix: str = ""
) -> dict[str, object]:
    recovery = recovery_started["event"]
    assert isinstance(recovery, dict)
    state = _step_state("1" * 64)
    detail = {
        **recovery,
        "started_at": recovery["started_at"],
        "recovery_started_event_sha256": recovery_started["event_sha256"],
        "state_after_sha256": state["collector_state_sha256"],
        "step_state_after": state,
    }
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        detail.update(
            {
                "completed_at": "2026-08-23T09:02:00+08:00" + suffix,
                "recovery_classification": "hot_delete_journal_recovered",
            }
        )
    else:
        detail.update(
            {
                "failed_at": "2026-08-23T09:02:00+08:00" + suffix,
                "failure_classification": "rollback_journal_recovery_failed",
                "retryable": False,
            }
        )
    return detail


def _genesis_event(tmp_path: Path) -> dict[str, object]:
    return build_collector_genesis_ledger_event(_genesis(tmp_path))


def _step_state(collector_state_sha256: str) -> dict[str, object]:
    tables = tuple(continuity.COLLECTOR_STATE_TABLES)
    allowed_tables = (
        "collection_receipts",
        "forward_context_observations",
        "forward_universe_observations",
        "forward_status_observations",
    )
    return {
        "schema_version": COLLECTOR_STEP_STATE_SCHEMA,
        "collector_state_sha256": collector_state_sha256,
        "table_counts": {table: 0 for table in tables},
        "table_sha256": {table: "e" * 64 for table in tables},
        "outside_scope_sha256": {table: "f" * 64 for table in allowed_tables},
        "receipt_id_high_water": 0,
    }


def _step_raw_before(
    allowed_tables: tuple[str, ...] = (
        "collection_receipts",
        "forward_context_observations",
        "forward_universe_observations",
        "forward_status_observations",
    ),
) -> dict[str, object]:
    return {
        "schema_version": COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
        "selector_rows": {table: [] for table in sorted(allowed_tables)},
    }


def _build(
    previous: tuple[dict[str, object], ...],
    event_type: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_collector_ledger_event(
        previous_event=previous[-1] if previous else None,
        event_type=event_type,
        event=details if details is not None else _detail(event_type),
    )


def _chain_event(
    previous: dict[str, object], event_type: str, details: dict[str, object]
) -> dict[str, object]:
    candidate = {
        "schema_version": COLLECTOR_LEDGER_EVENT_SCHEMA,
        "seq": int(previous["seq"]) + 1,
        "event_type": event_type,
        "previous_event_sha256": previous["event_sha256"],
        "event": details,
    }
    candidate["event_sha256"] = canonical_json_sha256(candidate)
    return continuity.validate_collector_ledger_event(candidate)


def _unchecked_chain_event(
    previous: dict[str, object], event_type: str, details: dict[str, object]
) -> dict[str, object]:
    candidate = {
        "schema_version": COLLECTOR_LEDGER_EVENT_SCHEMA,
        "seq": int(previous["seq"]) + 1,
        "event_type": event_type,
        "previous_event_sha256": previous["event_sha256"],
        "event": details,
    }
    candidate["event_sha256"] = canonical_json_sha256(candidate)
    return candidate


_CONTEXT_ALLOWED_TABLES = frozenset(
    {
        "collection_receipts",
        "forward_context_observations",
        "forward_universe_observations",
        "forward_status_observations",
    }
)
_CORPORATE_ACTIONS_ALLOWED_TABLES = frozenset(
    {
        "collection_receipts",
        "forward_corporate_action_coverage",
        "forward_corporate_actions",
    }
)
_PRICES_ALLOWED_TABLES = frozenset({"collection_receipts", "daily", "sync_coverage"})

_STEP_MAPPING_CASES = tuple(
    (session, step_id, phase, session_index * 4 + local_step_index, allowed_tables)
    for session_index, session in enumerate(_SESSIONS)
    for step_id, phase, local_step_index, allowed_tables in (
        ("pre_open_context", "pre_open", 0, _CONTEXT_ALLOWED_TABLES),
        (
            "pre_open_corporate_actions",
            "pre_open",
            1,
            _CORPORATE_ACTIONS_ALLOWED_TABLES,
        ),
        ("post_close_context", "post_close", 2, _CONTEXT_ALLOWED_TABLES),
        ("post_close_prices", "post_close", 3, _PRICES_ALLOWED_TABLES),
    )
)


def _step_state_for_allowed_tables(
    collector_state_sha256: str, allowed_tables: frozenset[str]
) -> dict[str, object]:
    state = _step_state(collector_state_sha256)
    state["outside_scope_sha256"] = {
        table: "f" * 64 for table in sorted(allowed_tables)
    }
    return state


def _attempt_chain(
    tmp_path: Path, details: dict[str, object]
) -> tuple[dict[str, object], ...]:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _unchecked_chain_event(
        registration,
        "ATTEMPT_STARTED",
        details,
    )
    return genesis, registration, started


def _nested_recovery_chain(
    tmp_path: Path,
    recovery_terminal_type: str,
    attempt_terminal_type: str,
) -> tuple[dict[str, object], ...]:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    recovery_started = _build(
        (genesis, registration, attempt_started),
        "SQLITE_RECOVERY_STARTED",
        _recovery_start_detail(attempt_started),
    )
    recovery_terminal = _chain_event(
        recovery_started,
        recovery_terminal_type,
        _recovery_terminal_detail(recovery_started, recovery_terminal_type),
    )
    attempt_terminal_details = _detail(attempt_terminal_type)
    if attempt_terminal_details["process_result_known"]:
        attempt_terminal_details["process_launch_state"] = "handle_obtained"
    elif attempt_terminal_details["recovered"]:
        attempt_terminal_details["process_launch_state"] = "indeterminate"
    else:
        raise AssertionError("attempt terminal launch state is undefined")
    if recovery_terminal_type == "SQLITE_RECOVERY_FAILED":
        attempt_terminal_details["failure_classification"] = "rollback_journal_recovery_failed"
        attempt_terminal_details["retryable"] = False
    attempt_terminal = _chain_event(
        recovery_terminal,
        attempt_terminal_type,
        attempt_terminal_details,
    )
    return (
        genesis,
        registration,
        attempt_started,
        recovery_started,
        recovery_terminal,
        attempt_terminal,
    )


@pytest.mark.parametrize(
    ("recovery_terminal_type", "attempt_terminal_type"),
    (
        ("SQLITE_RECOVERY_COMPLETED", "ATTEMPT_COMPLETED"),
        ("SQLITE_RECOVERY_COMPLETED", "ATTEMPT_FAILED"),
        ("SQLITE_RECOVERY_FAILED", "ATTEMPT_FAILED"),
    ),
)
def test_parser_allows_nested_recovery_without_closing_attempt(
    tmp_path: Path,
    recovery_terminal_type: str,
    attempt_terminal_type: str,
) -> None:
    events = _nested_recovery_chain(
        tmp_path,
        recovery_terminal_type,
        attempt_terminal_type,
    )
    assert parse_collector_ledger(_raw(events)) == events


def test_parser_rejects_recovery_failed_before_attempt_completion(
    tmp_path: Path,
) -> None:
    events = _nested_recovery_chain(
        tmp_path, "SQLITE_RECOVERY_FAILED", "ATTEMPT_FAILED"
    )
    completed_detail = _detail("ATTEMPT_COMPLETED")
    completed_detail["process_launch_state"] = "handle_obtained"
    completed_attempt = _chain_event(
        events[4], "ATTEMPT_COMPLETED", completed_detail
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([*events[:5], completed_attempt]))


def test_parser_rejects_recovery_terminal_identity_mismatch_and_event_while_open(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    recovery_started = _build(
        (genesis, registration, attempt_started),
        "SQLITE_RECOVERY_STARTED",
        _recovery_start_detail(attempt_started),
    )
    mismatched_terminal = _chain_event(
        recovery_started,
        "SQLITE_RECOVERY_COMPLETED",
        {
            **_recovery_terminal_detail(recovery_started, "SQLITE_RECOVERY_COMPLETED"),
            "recovery_started_event_sha256": "f" * 64,
        },
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(
            _raw((genesis, registration, attempt_started, recovery_started, mismatched_terminal))
        )

    other_event = _chain_event(
        recovery_started,
        "ATTEMPT_STARTED",
        _detail("ATTEMPT_STARTED", "-other"),
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(
            _raw((genesis, registration, attempt_started, recovery_started, other_event))
        )


def test_append_preserves_bytes_on_invalid_event_and_accepts_nested_recovery(
    tmp_path: Path,
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    recovery_started = _build(
        (genesis, registration, attempt_started),
        "SQLITE_RECOVERY_STARTED",
        _recovery_start_detail(attempt_started),
    )
    with open_existing_regular_file(path) as opened:
        append_collector_genesis_event(opened, genesis)
        append_collector_ledger_event(
            opened,
            event_type=registration["event_type"],
            event=registration["event"],
        )
        with continuity.acquire_collector_phase_lease(path) as lease:
            continuity._append_collector_phase_event(
                lease,
                event_type=attempt_started["event_type"],
                event=attempt_started["event"],
            )
            before = path.read_bytes()
            with pytest.raises(CollectorContinuityError):
                continuity._append_collector_phase_event(
                    lease,
                    event_type="ATTEMPT_STARTED",
                    event=_detail("ATTEMPT_STARTED", "-other"),
                )
            assert path.read_bytes() == before
            continuity._append_collector_phase_event(
                lease,
                event_type=recovery_started["event_type"],
                event=recovery_started["event"],
            )
            continuity._append_collector_phase_event(
                lease,
                event_type="SQLITE_RECOVERY_COMPLETED",
                event=_recovery_terminal_detail(
                    recovery_started, "SQLITE_RECOVERY_COMPLETED"
                ),
            )
            continuity._append_collector_phase_event(
                lease,
                event_type="ATTEMPT_COMPLETED",
                event=_detail("ATTEMPT_COMPLETED"),
            )
    assert len(parse_collector_ledger(path.read_bytes())) == 6


def _raw(events: tuple[dict[str, object], ...] | list[dict[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(event) + b"\n" for event in events)


def _write_chain(
    path: Path, tmp_path: Path, event_type: str | None = None
) -> tuple[dict[str, object], ...]:
    genesis = _genesis_event(tmp_path)
    events = (genesis,)
    if event_type is not None:
        if event_type != "REGISTRATION_BOUND":
            events += (_build(events, "REGISTRATION_BOUND"),)
        if event_type.startswith("SQLITE_RECOVERY_"):
            attempt_started = _build(events, "ATTEMPT_STARTED")
            events += (attempt_started,)
            recovery_started = _build(
                events,
                "SQLITE_RECOVERY_STARTED",
                _recovery_start_detail(attempt_started),
            )
            events += (recovery_started,)
        if event_type in {
            "ATTEMPT_COMPLETED",
            "ATTEMPT_FAILED",
        }:
            events += (_build(events, "ATTEMPT_STARTED"),)
        if event_type != "REGISTRATION_BOUND":
            if event_type.startswith("SQLITE_RECOVERY_"):
                if event_type != "SQLITE_RECOVERY_STARTED":
                    events += (
                        _build(
                            events,
                            event_type,
                            _recovery_terminal_detail(recovery_started, event_type),
                        ),
                    )
            else:
                events += (_build(events, event_type),)
        elif len(events) == 1:
            events += (_build(events, event_type),)
    path.write_bytes(_raw(events))
    return events


def _rehash(event: dict[str, object]) -> dict[str, object]:
    event["event_sha256"] = canonical_json_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )
    return event


@pytest.mark.parametrize("ordinal", (12, 15, 5))
def test_ledger_event_rejects_out_of_range_and_global_step_ordinal(
    tmp_path: Path, ordinal: int
) -> None:
    genesis = _genesis_event(tmp_path)
    details = _detail("ATTEMPT_STARTED")
    details["step_ordinal"] = ordinal
    with pytest.raises(CollectorContinuityError):
        _build((genesis,), "ATTEMPT_STARTED", details)


def test_ledger_state_machine_rejects_genesis_to_start_without_registration(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    start = _build((genesis,), "ATTEMPT_STARTED")
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, start]))


def test_ledger_state_machine_rejects_duplicate_registration(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    duplicate = _build(
        (genesis, registration),
        "REGISTRATION_BOUND",
        _detail("REGISTRATION_BOUND", "-duplicate"),
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, registration, duplicate]))


def test_ledger_state_machine_allows_one_dangling_start_but_rejects_second(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    start_a = _build((genesis, registration), "ATTEMPT_STARTED")
    start_b = _build(
        (genesis, registration, start_a),
        "ATTEMPT_STARTED",
        _detail("ATTEMPT_STARTED", "-second"),
    )
    assert parse_collector_ledger(_raw([genesis, registration, start_a])) == (
        genesis,
        registration,
        start_a,
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, registration, start_a, start_b]))


def test_ledger_state_machine_rejects_terminal_for_non_current_open_attempt(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    start_a = _build((genesis, registration), "ATTEMPT_STARTED")
    start_b = _build(
        (genesis, registration, start_a),
        "ATTEMPT_STARTED",
        _detail("ATTEMPT_STARTED", "-second"),
    )
    terminal_b = _build(
        (genesis, registration, start_a, start_b),
        "ATTEMPT_COMPLETED",
        _detail("ATTEMPT_COMPLETED", "-second"),
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, registration, start_a, start_b, terminal_b]))


def test_ledger_limits_are_frozen() -> None:
    assert COLLECTOR_LEDGER_MAX_BYTES == 128 * 1024 * 1024
    assert COLLECTOR_LEDGER_MAX_LINES == 100_000
    assert COLLECTOR_LEDGER_MAX_LINE_BYTES == 64 * 1024


@pytest.mark.parametrize("limit_name", ["COLLECTOR_LEDGER_MAX_BYTES", "COLLECTOR_LEDGER_MAX_LINES"])
def test_parser_accepts_exact_byte_and_line_limits_and_rejects_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_name: str
) -> None:
    path = tmp_path / "ledger.jsonl"
    events = _write_chain(path, tmp_path, "REGISTRATION_BOUND")
    raw = path.read_bytes()
    monkeypatch.setattr(continuity, limit_name, len(raw) if limit_name.endswith("BYTES") else 2)
    assert parse_collector_ledger(raw) == events

    monkeypatch.setattr(
        continuity,
        limit_name,
        (len(raw) + 1) if limit_name.endswith("BYTES") else 3,
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(raw + raw[-1:])


def test_parser_accepts_exact_line_limit_and_rejects_longer_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    genesis = _genesis_event(tmp_path)
    base_short = _build((genesis,), "REGISTRATION_BOUND")
    suffix_length = max(
        1,
        len(canonical_json_bytes(genesis)) - len(canonical_json_bytes(base_short)) + 1,
    )
    short = _build(
        (genesis,),
        "REGISTRATION_BOUND",
        _detail("REGISTRATION_BOUND", "x" * suffix_length),
    )
    long_event = _build(
        (genesis,),
        "REGISTRATION_BOUND",
        _detail("REGISTRATION_BOUND", "x" * (suffix_length + 1)),
    )
    short_raw = _raw([genesis, short])
    long_raw = _raw([genesis, long_event])
    exact_limit = max(
        len(canonical_json_bytes(genesis)),
        len(canonical_json_bytes(short)),
    )
    monkeypatch.setattr(continuity, "COLLECTOR_LEDGER_MAX_LINE_BYTES", exact_limit)
    assert parse_collector_ledger(short_raw) == (genesis, short)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(long_raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        b"\n\n",
        b'{"a":1}\n',
        b'{"seq": 0, "event_type": "GENESIS"}\n',
    ],
    ids=["empty", "newline", "blank-lines", "noncanonical-ascii", "wrong-schema"],
)
def test_parser_rejects_empty_newline_blank_and_noncanonical_inputs(raw: bytes) -> None:
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(raw)


@pytest.mark.parametrize("mutation", ["whitespace", "unsorted", "duplicate-key"])
def test_parser_rejects_noncanonical_json_and_duplicate_keys(
    tmp_path: Path, mutation: str
) -> None:
    event = _genesis_event(tmp_path)
    canonical = canonical_json_bytes(event)
    if mutation == "whitespace":
        raw_line = json.dumps(event, ensure_ascii=True, sort_keys=True).encode("ascii")
    elif mutation == "unsorted":
        raw_line = json.dumps(event, ensure_ascii=True, sort_keys=False, separators=(",", ":")).encode(
            "ascii"
        )
        if raw_line == canonical:
            raw_line = b'{"event_sha256":' + canonical.split(b'"event_sha256":', 1)[1]
    else:
        marker = b'"seq":0'
        raw_line = canonical.replace(marker, b'"seq":0,"seq":0', 1)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(raw_line + b"\n")


@pytest.mark.parametrize("truncation", ["last-byte", "partial-line"])
def test_parser_rejects_truncated_legal_multiline_chain(
    tmp_path: Path, truncation: str
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED")
    raw = _raw((genesis, registration, started))
    truncated = raw[:-1] if truncation == "last-byte" else raw[:-17] + b"\n"

    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(truncated)


def test_parser_accepts_complete_last_event_deletion_as_legal_prefix(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED")
    del started

    assert parse_collector_ledger(_raw((genesis, registration))) == (
        genesis,
        registration,
    )


def test_parser_accepts_genesis_only_and_each_non_genesis_type_from_bytes_and_open_file(
    tmp_path: Path,
) -> None:
    for event_type in (None, *_NON_GENESIS_TYPES):
        path = tmp_path / f"{event_type or 'genesis-only'}.jsonl"
        expected = _write_chain(path, tmp_path, event_type)
        assert parse_collector_ledger(path.read_bytes()) == expected
        with open_existing_regular_file(path) as opened:
            assert parse_collector_ledger(opened) == expected


def test_builder_allocates_position_and_hash_metadata(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    first = _build((genesis,), "REGISTRATION_BOUND")
    second = _build((genesis, first), "ATTEMPT_STARTED")

    assert first["seq"] == 1
    assert first["previous_event_sha256"] == genesis["event_sha256"]
    assert second["seq"] == 2
    assert second["previous_event_sha256"] == first["event_sha256"]
    for event in (first, second):
        assert set(event) == {
            "schema_version",
            "seq",
            "event_type",
            "previous_event_sha256",
            "event_sha256",
            "event",
        }
        assert event["schema_version"] == COLLECTOR_LEDGER_EVENT_SCHEMA
        assert event["event_sha256"] == canonical_json_sha256(
            {key: value for key, value in event.items() if key != "event_sha256"}
        )


@pytest.mark.parametrize("bad_type", ["GENESIS", "UNKNOWN"])
def test_builder_rejects_genesis_reposition_and_unknown_type(
    tmp_path: Path, bad_type: str
) -> None:
    genesis = _genesis_event(tmp_path)
    details = _genesis(tmp_path) if bad_type == "GENESIS" else {}
    with pytest.raises(CollectorContinuityError):
        build_collector_ledger_event(
            previous_event=genesis,
            event_type=bad_type,
            event=details,
        )


def test_parser_rejects_sequence_and_genesis_position_errors(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")

    duplicate_seq = deepcopy(registration)
    duplicate_seq["seq"] = 0
    _rehash(duplicate_seq)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, duplicate_seq]))

    skipped_seq = deepcopy(registration)
    skipped_seq["seq"] = 2
    _rehash(skipped_seq)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, skipped_seq]))

    moved_genesis = deepcopy(genesis)
    moved_genesis["seq"] = 1
    moved_genesis["previous_event_sha256"] = genesis["event_sha256"]
    _rehash(moved_genesis)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([registration, moved_genesis]))


@pytest.mark.parametrize("mutation", ["previous", "self", "successor-previous"])
def test_parser_rejects_previous_and_self_hash_tampering(
    tmp_path: Path, mutation: str
) -> None:
    genesis = _genesis_event(tmp_path)
    first = _build((genesis,), "REGISTRATION_BOUND")
    second = _build((genesis, first), "ATTEMPT_STARTED")
    events = [deepcopy(genesis), deepcopy(first), deepcopy(second)]
    if mutation == "previous":
        events[1]["previous_event_sha256"] = "f" * 64
        _rehash(events[1])
    elif mutation == "self":
        events[1]["event_sha256"] = "f" * 64
    else:
        events[2]["previous_event_sha256"] = "f" * 64
        _rehash(events[2])
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw(events))


def _new_empty_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.jsonl"
    path.touch(mode=0o600)
    return path


def test_genesis_writer_accepts_only_empty_ledger_and_parser_loads_tail(
    tmp_path: Path,
) -> None:
    path = _new_empty_ledger(tmp_path)
    event = _genesis_event(tmp_path)
    with open_existing_regular_file(path) as opened:
        assert append_collector_genesis_event(opened, event) == event["event_sha256"]
    assert parse_collector_ledger(path.read_bytes()) == (event,)

    original = path.read_bytes()
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_genesis_event(opened, event)
    assert path.read_bytes() == original


def test_genesis_builder_and_loader_remain_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = tmp_path / "panel.json"
    symbols = (
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
    )
    panel.write_bytes(
        canonical_json_bytes(
            [f"{symbol}@{session}" for symbol in symbols for session in _SESSIONS]
        )
    )
    database = tmp_path / "evidence.sqlite"
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: continuity.datetime.fromisoformat("2026-08-23T09:00:00+08:00"),
    )
    from stockdata.collector_continuity import (
        default_collector_ledger_path,
        load_verified_prepared_collector,
    )
    from stockdata.future_panel_registration import prepare_future_collector_database

    prepare_future_collector_database(database_file=database, panel_file=panel)
    ledger = Path(default_collector_ledger_path(database))
    with open_existing_regular_file(ledger) as opened:
        parsed = parse_collector_ledger(opened)
    assert len(parsed) == 1
    assert load_verified_prepared_collector(database_path=database, ledger_path=ledger)[
        "ledger_genesis_event_sha256"
    ] == parsed[0]["event_sha256"]


def test_writer_appends_valid_event_and_persists_tail(tmp_path: Path) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    with open_existing_regular_file(path) as opened:
        append_collector_genesis_event(opened, genesis)
    event = _build((genesis,), "REGISTRATION_BOUND")
    before = path.read_bytes()
    with open_existing_regular_file(path) as opened:
        result = append_collector_ledger_event(
            opened,
            event_type=event["event_type"],
            event=event["event"],
        )
    assert result == event
    assert path.read_bytes() == before + canonical_json_bytes(event) + b"\n"
    assert parse_collector_ledger(path.read_bytes()) == (genesis, event)


def test_writer_rejects_duplicate_registration_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    with open_existing_regular_file(path) as opened:
        append_collector_genesis_event(opened, genesis)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    with open_existing_regular_file(path) as opened:
        append_collector_ledger_event(
            opened,
            event_type=registration["event_type"],
            event=registration["event"],
        )
    before = path.read_bytes()
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type="REGISTRATION_BOUND",
                event=_detail("REGISTRATION_BOUND", "-duplicate"),
            )
    assert path.read_bytes() == before


def test_writer_rejects_second_dangling_start_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    with open_existing_regular_file(path) as opened:
        append_collector_genesis_event(opened, genesis)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    with open_existing_regular_file(path) as opened:
        append_collector_ledger_event(
            opened,
            event_type=registration["event_type"],
            event=registration["event"],
        )
    with continuity.acquire_collector_phase_lease(path) as lease:
        continuity._append_collector_phase_event(
            lease,
            event_type="ATTEMPT_STARTED",
            event=_detail("ATTEMPT_STARTED"),
        )
    before = path.read_bytes()
    with continuity.acquire_collector_phase_lease(path) as lease:
        with pytest.raises(CollectorContinuityError):
            continuity._append_collector_phase_event(
                lease,
                event_type="ATTEMPT_STARTED",
                event=_detail("ATTEMPT_STARTED", "-second"),
            )
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["non-genesis-empty", "bad-seq", "bad-previous", "bad-self"])
def test_writer_rejects_bad_chain_without_writing(tmp_path: Path, bad: str) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    if bad == "non-genesis-empty":
        event = _build((genesis,), "REGISTRATION_BOUND")
        before = path.read_bytes()
        with open_existing_regular_file(path) as opened:
            with pytest.raises(CollectorContinuityError):
                append_collector_ledger_event(
                    opened,
                    event_type=event["event_type"],
                    event=event["event"],
                )
    else:
        malformed = deepcopy(genesis)
        if bad == "bad-seq":
            malformed["seq"] = 1
            _rehash(malformed)
        elif bad == "bad-previous":
            malformed["previous_event_sha256"] = "f" * 64
            _rehash(malformed)
        else:
            malformed["event_sha256"] = "f" * 64
        path.write_bytes(_raw([malformed]))
        event = _build((genesis,), "REGISTRATION_BOUND")
        before = path.read_bytes()
        with open_existing_regular_file(path) as opened:
            with pytest.raises(CollectorContinuityError):
                append_collector_ledger_event(
                    opened,
                    event_type=event["event_type"],
                    event=event["event"],
                )
    assert path.read_bytes() == before


@pytest.mark.parametrize("limit_name", ["COLLECTOR_LEDGER_MAX_BYTES", "COLLECTOR_LEDGER_MAX_LINES"])
def test_writer_rejects_limit_overflow_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_name: str
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    before = path.read_bytes()
    monkeypatch.setattr(continuity, limit_name, len(before) if limit_name.endswith("BYTES") else 1)
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert path.read_bytes() == before


def test_writer_rejects_line_limit_overflow_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    before = path.read_bytes()
    monkeypatch.setattr(continuity, "COLLECTOR_LEDGER_MAX_LINE_BYTES", len(canonical_json_bytes(event)) - 1)
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert path.read_bytes() == before


def test_writer_handles_partial_write_and_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    original_write = continuity.os.write
    original_fsync = continuity.os.fsync
    fsynced: list[int] = []

    def partial_write(fd: int, data: bytes) -> int:
        return original_write(fd, data[:1])

    def record_fsync(fd: int) -> None:
        fsynced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(continuity.os, "write", partial_write)
    monkeypatch.setattr(continuity.os, "fsync", record_fsync)
    with open_existing_regular_file(path) as opened:
        append_collector_ledger_event(
            opened,
            event_type=event["event_type"],
            event=event["event"],
        )
    assert fsynced
    assert parse_collector_ledger(path.read_bytes()) == (genesis, event)


def test_writer_zero_write_fails_without_persisting_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    before = path.read_bytes()
    monkeypatch.setattr(continuity.os, "write", lambda fd, data: 0)
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert path.read_bytes() == before


def test_writer_rejects_opened_file_identity_drift_without_writing(tmp_path: Path) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    with open_existing_regular_file(path) as opened:
        os.replace(replacement, path)
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert path.read_bytes() == _raw([genesis])


def test_writer_rejects_extra_append_after_read_without_appending_its_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    path.write_bytes(_raw([genesis]))
    event = _build((genesis,), "REGISTRATION_BOUND")
    injected = _build((genesis,), "ATTEMPT_STARTED")
    original_parse = continuity.parse_collector_ledger
    called = False

    def parse_then_append(source: object) -> tuple[dict[str, object], ...]:
        nonlocal called
        result = original_parse(source)
        if not called:
            called = True
            with path.open("ab") as handle:
                handle.write(canonical_json_bytes(injected) + b"\n")
        return result

    monkeypatch.setattr(continuity, "parse_collector_ledger", parse_then_append)
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert path.read_bytes() == _raw([genesis, injected])


def test_writer_fails_closed_when_canonical_path_is_replaced_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_empty_ledger(tmp_path)
    genesis = _genesis_event(tmp_path)
    baseline = _raw([genesis])
    path.write_bytes(baseline)
    event = _build((genesis,), "REGISTRATION_BOUND")
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(baseline)
    original_write = continuity.os.write
    replaced = False

    def replace_during_write(fd: int, data: bytes) -> int:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, path)
            replaced = True
        return original_write(fd, data)

    monkeypatch.setattr(continuity.os, "write", replace_during_write)
    with open_existing_regular_file(path) as opened:
        with pytest.raises(CollectorContinuityError):
            append_collector_ledger_event(
                opened,
                event_type=event["event_type"],
                event=event["event"],
            )
    assert replaced
    assert path.read_bytes() == baseline
    assert parse_collector_ledger(path.read_bytes()) == (genesis,)


def test_genesis_event_requires_exact_nested_schema(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    assert genesis["event"] == {"genesis": _genesis(tmp_path)}

    malformed = deepcopy(genesis)
    nested = dict(malformed["event"]["genesis"])
    nested["unexpected"] = True
    malformed["event"] = {"genesis": nested}
    _rehash(malformed)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([malformed]))


def test_parser_rejects_subsequent_foreign_database_uuid(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    details = _detail("ATTEMPT_STARTED")
    details["database_uuid"] = "b" * 64
    foreign_attempt = _unchecked_chain_event(
        registration,
        "ATTEMPT_STARTED",
        details,
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw([genesis, registration, foreign_attempt]))


@pytest.mark.parametrize(
    ("session", "step_id", "phase", "ordinal", "allowed_tables"),
    _STEP_MAPPING_CASES,
)
def test_parser_accepts_frozen_step_mapping_for_each_session(
    tmp_path: Path,
    session: str,
    step_id: str,
    phase: str,
    ordinal: int,
    allowed_tables: frozenset[str],
) -> None:
    details = _detail("ATTEMPT_STARTED")
    details.update(
        {
            "session": session,
            "step_id": step_id,
            "phase": phase,
            "step_ordinal": ordinal,
        }
    )
    details["step_state_before"] = _step_state_for_allowed_tables(
        "d" * 64,
        allowed_tables,
    )
    details["step_raw_before"] = _step_raw_before(tuple(allowed_tables))
    events = _attempt_chain(tmp_path, details)
    assert parse_collector_ledger(_raw(events)) == events


@pytest.mark.parametrize("mutation", ["step-id", "phase", "ordinal", "scope"])
def test_parser_rejects_step_identity_and_self_attested_scope(
    tmp_path: Path, mutation: str
) -> None:
    details = _detail("ATTEMPT_STARTED")
    if mutation == "step-id":
        details.update(
            {
                "session": "2099-01-06",
                "step_id": "post_close_context",
                "phase": "post_close",
                "step_ordinal": 4,
            }
        )
    elif mutation == "phase":
        details["phase"] = "post_close"
    elif mutation == "ordinal":
        details["session"] = "2099-01-05"
        details["step_ordinal"] = 4
    else:
        details["step_state_before"] = _step_state_for_allowed_tables(
            "d" * 64,
            frozenset({"collection_receipts"}),
        )
    events = _attempt_chain(tmp_path, details)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw(events))


@pytest.mark.parametrize(
    ("session", "step_id", "phase", "ordinal"),
    (
        ("2099-01-05", "pre_open_context", "pre_open", 4),
        ("2099-01-06", "pre_open_context", "pre_open", 8),
        ("2099-01-07", "pre_open_context", "pre_open", 0),
    ),
)
def test_parser_rejects_correct_ordinal_with_wrong_session(
    tmp_path: Path,
    session: str,
    step_id: str,
    phase: str,
    ordinal: int,
) -> None:
    details = _detail("ATTEMPT_STARTED")
    details.update(
        {
            "session": session,
            "step_id": step_id,
            "phase": phase,
            "step_ordinal": ordinal,
        }
    )
    events = _attempt_chain(tmp_path, details)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw(events))


@pytest.mark.parametrize(
    ("session", "step_id", "phase", "ordinal"),
    (
        ("2099-01-06", "post_close_context", "post_close", 4),
        ("2099-01-06", "pre_open_context", "post_close", 4),
    ),
)
def test_parser_rejects_correct_session_with_wrong_step_or_phase(
    tmp_path: Path,
    session: str,
    step_id: str,
    phase: str,
    ordinal: int,
) -> None:
    details = _detail("ATTEMPT_STARTED")
    details.update(
        {
            "session": session,
            "step_id": step_id,
            "phase": phase,
            "step_ordinal": ordinal,
        }
    )
    events = _attempt_chain(tmp_path, details)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw(events))


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "extra",
        "wrong-count",
        "duplicate",
        "out-of-order",
        "invalid-iso",
        "hash-mismatch",
    ),
)
def test_parser_rejects_registration_sessions_contract(
    tmp_path: Path, mutation: str
) -> None:
    genesis = _genesis_event(tmp_path)
    details = _detail("REGISTRATION_BOUND")
    if mutation == "missing":
        del details["sessions"]
    elif mutation == "extra":
        details["unexpected"] = True
    elif mutation == "wrong-count":
        details["sessions"] = list(_SESSIONS[:2])
        details["sessions_sha256"] = canonical_json_sha256(details["sessions"])
    elif mutation == "duplicate":
        details["sessions"] = [_SESSIONS[0], _SESSIONS[0], _SESSIONS[2]]
        details["sessions_sha256"] = canonical_json_sha256(details["sessions"])
    elif mutation == "out-of-order":
        details["sessions"] = list(reversed(_SESSIONS))
        details["sessions_sha256"] = canonical_json_sha256(details["sessions"])
    elif mutation == "invalid-iso":
        details["sessions"] = [_SESSIONS[0], "2099-1-06", _SESSIONS[2]]
        details["sessions_sha256"] = canonical_json_sha256(details["sessions"])
    else:
        details["sessions_sha256"] = "f" * 64
    registration = _unchecked_chain_event(genesis, "REGISTRATION_BOUND", details)
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw((genesis, registration)))


@pytest.mark.parametrize("field", ["database_uuid", "state_before_sha256", "registration_sha256"])
def test_parser_rejects_nested_recovery_foreign_identity(
    tmp_path: Path, field: str
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    details = _detail("SQLITE_RECOVERY_STARTED")
    details[field] = "8" * 64
    recovery_started = _unchecked_chain_event(
        attempt_started,
        "SQLITE_RECOVERY_STARTED",
        details,
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(
            _raw((genesis, registration, attempt_started, recovery_started))
        )


def test_parser_rejects_nonzero_attempt_completed(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    details = _detail("ATTEMPT_COMPLETED")
    details["returncode"] = 1
    completed = _unchecked_chain_event(
        attempt_started,
        "ATTEMPT_COMPLETED",
        details,
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw((genesis, registration, attempt_started, completed)))


def test_parser_rejects_new_attempt_after_nonretryable_failure(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    failed_details = _detail("ATTEMPT_FAILED")
    failed_details["failure_classification"] = "forbidden_drift"
    failed_details["retryable"] = False
    failed = _unchecked_chain_event(
        attempt_started,
        "ATTEMPT_FAILED",
        failed_details,
    )
    retry = _unchecked_chain_event(
        failed,
        "ATTEMPT_STARTED",
        _detail("ATTEMPT_STARTED", "-retry"),
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw((genesis, registration, attempt_started, failed, retry)))


def test_parser_rejects_reused_attempt_id_after_retryable_failure(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    attempt_started = _build((genesis, registration), "ATTEMPT_STARTED")
    failed = _unchecked_chain_event(
        attempt_started,
        "ATTEMPT_FAILED",
        _detail("ATTEMPT_FAILED"),
    )
    reused = _unchecked_chain_event(
        failed,
        "ATTEMPT_STARTED",
        _detail("ATTEMPT_STARTED"),
    )
    with pytest.raises(CollectorContinuityError):
        parse_collector_ledger(_raw((genesis, registration, attempt_started, failed, reused)))
