from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable

import pytest

import stockdata.collector_continuity as continuity
import stockdata.provider_materializer as provider_materializer
import stockdata.registered_panel_capture as registered_panel_capture
from stockdata.collector_continuity import (
    CollectorAttemptOutcome,
    CollectorContinuityError,
)
from stockdata.provider_materializer import ProviderMaterializationError
from stockdata.registered_panel_capture import RegisteredPanelCaptureError
from test_collector_attempt_protocol import _prepared
from test_collector_recovery import _leave_hot_delete_journal
from test_provider_materializer import _inputs as _materializer_inputs
from test_registered_panel_capture import _registration as _capture_registration


_PRE_OPEN = "2099-01-05T08:45:00+08:00"
_POST_CLOSE = "2099-01-05T15:10:00+08:00"


@pytest.fixture(autouse=True)
def _stub_static_prerequisite_reverification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        continuity,
        "_reverify_registered_collector_static_prerequisites",
        lambda *args, **kwargs: None,
    )


def _phase_api() -> Callable[..., object]:
    value = getattr(continuity, "execute_registered_collector_phase", None)
    if value is None:
        pytest.fail(
            "missing task 2.3/3.3 API: execute_registered_collector_phase"
        )
    return value


def _call_phase(
    prepared: dict[str, object],
    *,
    effective_date: str = "2099-01-05",
    phase: str = "pre_open",
    observed_at: str = _PRE_OPEN,
    popen_factory: Callable[..., object] | None = None,
) -> object:
    if popen_factory is None:
        def popen_factory(*args: object, **kwargs: object) -> object:
            del args, kwargs
            pytest.fail("phase preflight unexpectedly invoked Popen")

    executor = continuity._execute_collector_step_attempt
    recover = continuity._recover_dangling_collector_attempt

    def injected_executor(
        lease: object, spec: object, **kwargs: object
    ) -> CollectorAttemptOutcome:
        del kwargs
        return executor(
            lease,
            spec,
            now=lambda: observed_at,
            popen_factory=popen_factory,
        )

    def injected_recovery(
        lease: object, spec: object, **kwargs: object
    ) -> CollectorAttemptOutcome:
        del kwargs
        return recover(lease, spec, now=lambda: observed_at)

    with pytest.MonkeyPatch.context() as phase_patch:
        phase_patch.setattr(
            continuity, "_collector_attempt_now", lambda: observed_at
        )
        phase_patch.setattr(
            continuity, "_execute_collector_step_attempt", injected_executor
        )
        phase_patch.setattr(
            continuity, "_recover_dangling_collector_attempt", injected_recovery
        )
        return _phase_api()(
            prepared["registration"],
            database=prepared["database"],
            effective_date=effective_date,
            phase=phase,
        )


def _outcome(
    spec: object,
    *,
    event_type: str = "ATTEMPT_COMPLETED",
    classification: str = "complete",
    retryable: bool = False,
    suffix: str = "0",
) -> CollectorAttemptOutcome:
    return CollectorAttemptOutcome(
        step_id=spec.step_id,
        step_ordinal=spec.step_ordinal,
        attempt_id=f"attempt-{spec.step_ordinal}-{suffix}",
        terminal_event_sha256=suffix * 64,
        terminal_event_type=event_type,
        classification=classification,
        retryable=retryable,
        process_result_known=True,
        returncode=0,
        raw_class="complete" if event_type == "ATTEMPT_COMPLETED" else "unchanged",
    )


def _patch_step_executor(
    monkeypatch: pytest.MonkeyPatch,
    implementation: Callable[..., CollectorAttemptOutcome],
) -> None:
    monkeypatch.setattr(
        continuity, "_execute_collector_step_attempt", implementation
    )
    monkeypatch.setattr(
        continuity, "execute_collector_step_attempt", implementation
    )


def _ledger_history(ledger: Path) -> tuple[dict[str, object], ...]:
    return continuity.parse_collector_ledger(ledger.read_bytes())


def _append_completed_attempt(
    lease: object, spec: object
) -> CollectorAttemptOutcome:
    started_at = (
        f"{spec.session}T08:45:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T15:10:00+08:00"
    )
    finished_at = (
        f"{spec.session}T08:46:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T15:11:00+08:00"
    )
    launch = continuity._begin_collector_step_attempt(
        lease, spec, now=lambda: started_at
    )
    try:
        empty_sha256 = hashlib.sha256(b"").hexdigest()
        raw = continuity._raw_result(
            "complete", "complete", launch.baseline.step_state
        )
        process = continuity._CollectorProcessResult(
            True, 0, empty_sha256, 0, empty_sha256, 0, False
        )
        event_type, detail = continuity._terminal_attempt_event(
            launch,
            raw,
            process,
            process_launch_state="handle_obtained",
            finished_at=finished_at,
            failure_classification=None,
        )
        terminal = continuity._append_terminal_once(
            lease, launch, event_type=event_type, event=detail
        )
        return CollectorAttemptOutcome(
            step_id=spec.step_id,
            step_ordinal=spec.step_ordinal,
            attempt_id=launch.attempt_id,
            terminal_event_sha256=str(terminal["event_sha256"]),
            terminal_event_type="ATTEMPT_COMPLETED",
            classification="complete",
            retryable=False,
            process_result_known=True,
            returncode=0,
            raw_class="complete",
        )
    finally:
        continuity._clear_nonce(launch._nonce_buffer)
        launch.nonce = b""


def test_frozen_schedule_is_three_sessions_four_stable_ids_and_global_ordinals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    schedule = prepared["schedule"]
    expected = (
        ("pre_open_context", "pre_open"),
        ("pre_open_corporate_actions", "pre_open"),
        ("post_close_context", "post_close"),
        ("post_close_prices", "post_close"),
    )

    assert len(schedule) == 12
    assert [spec.step_ordinal for spec in schedule] == list(range(12))
    assert [(spec.step_id, spec.phase) for spec in schedule] == list(expected) * 3
    assert [spec.session for spec in schedule] == [
        session
        for session in ("2099-01-05", "2099-01-06", "2099-01-07")
        for _ in range(4)
    ]


def test_competing_capture_fails_before_schedule_sqlite_append_or_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before = prepared["ledger"].read_bytes()
    calls: list[str] = []

    monkeypatch.setattr(
        continuity,
        "freeze_collector_step_schedule",
        lambda **kwargs: calls.append("schedule") or pytest.fail(
            "schedule/SQLite authority opened before the nonblocking lease"
        ),
    )
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        with pytest.raises(CollectorContinuityError):
            _call_phase(prepared)

    assert calls == []
    assert prepared["ledger"].read_bytes() == before


def test_phase_holds_one_lease_across_both_steps_and_releases_after_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    calls: list[tuple[int, int]] = []
    before_fds = set(os.listdir("/dev/fd"))

    def execute(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del kwargs
        identity = lease.verify()
        calls.append((lease.ledger.file_fd, spec.step_ordinal))
        with pytest.raises(CollectorContinuityError):
            continuity.acquire_collector_phase_lease(prepared["ledger"])
        if spec.step_ordinal == 1:
            raise RuntimeError("injected parent abort")
        assert identity == lease.verify()
        return _outcome(spec)

    _patch_step_executor(monkeypatch, execute)
    monkeypatch.setattr(
        continuity,
        "_completed_collector_ordinals",
        lambda history: {ordinal for _, ordinal in calls},
    )
    with pytest.raises(RuntimeError, match="parent abort"):
        _call_phase(prepared)

    assert [ordinal for _, ordinal in calls] == [0, 1]
    assert len({descriptor for descriptor, _ in calls}) == 1
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass
    assert set(os.listdir("/dev/fd")) == before_fds


@pytest.mark.parametrize(
    ("effective_date", "phase", "observed_at"),
    [
        ("2099-01-08", "pre_open", "2099-01-08T08:45:00+08:00"),
        ("2099-01-05", "pre_open", "2099-01-05T08:29:59+08:00"),
        ("2099-01-05", "pre_open", "2099-01-05T09:25:00+08:00"),
        ("2099-01-05", "post_close", "2099-01-05T14:59:59+08:00"),
    ],
)
def test_wrong_date_or_shanghai_phase_window_is_zero_append_zero_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effective_date: str,
    phase: str,
    observed_at: str,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before = prepared["ledger"].read_bytes()
    popen_calls: list[object] = []

    with pytest.raises(CollectorContinuityError):
        _call_phase(
            prepared,
            effective_date=effective_date,
            phase=phase,
            observed_at=observed_at,
            popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )

    assert popen_calls == []
    assert prepared["ledger"].read_bytes() == before


def test_phase_boundaries_and_exact_two_step_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = (
        ("pre_open", "2099-01-05T08:30:00+08:00", [0, 1]),
        ("pre_open", "2099-01-05T09:24:59.999999+08:00", [0, 1]),
        ("post_close", "2099-01-05T15:00:00+08:00", [2, 3]),
    )
    for index, (phase, observed_at, expected) in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        prepared = _prepared(case_dir, monkeypatch)
        calls: list[int] = []

        def execute(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
            del kwargs
            lease.verify()
            calls.append(spec.step_ordinal)
            return _outcome(spec)

        _patch_step_executor(monkeypatch, execute)
        predecessor = {0, 1} if phase == "post_close" else set()
        monkeypatch.setattr(
            continuity,
            "_completed_collector_ordinals",
            lambda history: predecessor | set(calls),
        )
        monkeypatch.setattr(
            continuity,
            "_verify_registered_collector_tail_state",
            lambda *args, **kwargs: None,
        )
        _call_phase(prepared, phase=phase, observed_at=observed_at)
        assert calls == expected


def test_skipped_predecessor_and_completed_phase_are_rejected_without_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before = prepared["ledger"].read_bytes()
    calls: list[object] = []

    with pytest.raises(CollectorContinuityError, match="earlier|predecessor|order"):
        _call_phase(
            prepared,
            phase="post_close",
            observed_at=_POST_CLOSE,
            popen_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []
    assert prepared["ledger"].read_bytes() == before


def test_partial_phase_runs_only_the_missing_step_and_complete_phase_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    calls: list[int] = []

    def execute(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del kwargs
        lease.verify()
        calls.append(spec.step_ordinal)
        return _outcome(spec)

    _patch_step_executor(monkeypatch, execute)
    monkeypatch.setattr(
        continuity,
        "_completed_collector_ordinals",
        lambda history: {0} | set(calls),
    )
    monkeypatch.setattr(
        continuity,
        "_verify_registered_collector_tail_state",
        lambda *args, **kwargs: None,
    )
    _call_phase(prepared)
    assert calls == [1]

    calls.clear()
    monkeypatch.setattr(
        continuity,
        "_completed_collector_ordinals",
        lambda history: {0, 1},
    )
    with pytest.raises(CollectorContinuityError, match="already complete"):
        _call_phase(prepared)
    assert calls == []


def test_real_completed_phase_repeat_is_zero_child_provider_and_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    _patch_step_executor(
        monkeypatch,
        lambda lease, spec, **kwargs: _append_completed_attempt(lease, spec),
    )
    first = _call_phase(prepared)
    assert [outcome.step_ordinal for outcome in first] == [0, 1]
    assert [
        event["event"]["step_ordinal"]
        for event in _ledger_history(prepared["ledger"])
        if event["event_type"] == "ATTEMPT_COMPLETED"
    ] == [0, 1]

    before = prepared["ledger"].read_bytes()
    calls = {"child": 0, "provider": 0, "append": 0, "popen": 0}

    def forbidden(name):
        def record(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError(f"duplicate phase reached {name}")

        return record

    _patch_step_executor(monkeypatch, forbidden("child"))
    monkeypatch.setattr(
        continuity, "_provider_call", forbidden("provider"), raising=False
    )
    monkeypatch.setattr(
        continuity, "_append_verified_ledger_payload", forbidden("append")
    )
    with pytest.raises(CollectorContinuityError, match="already complete"):
        _call_phase(prepared, popen_factory=forbidden("popen"))

    assert calls == {"child": 0, "provider": 0, "append": 0, "popen": 0}
    assert prepared["ledger"].read_bytes() == before


def test_second_session_rejects_cross_date_gap_before_child_provider_or_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    real_executor = continuity._execute_collector_step_attempt
    _patch_step_executor(
        monkeypatch,
        lambda lease, spec, **kwargs: _append_completed_attempt(lease, spec),
    )
    _call_phase(prepared)
    before = prepared["ledger"].read_bytes()
    calls = {"provider": 0, "append": 0, "popen": 0}

    def forbidden(name):
        def record(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError(f"cross-date gap reached {name}")

        return record

    _patch_step_executor(monkeypatch, real_executor)
    monkeypatch.setattr(
        continuity, "_provider_call", forbidden("provider"), raising=False
    )
    monkeypatch.setattr(
        continuity, "_append_verified_ledger_payload", forbidden("append")
    )
    with pytest.raises(CollectorContinuityError, match="earlier step is incomplete"):
        _call_phase(
            prepared,
            effective_date="2099-01-06",
            observed_at="2099-01-06T08:45:00+08:00",
            popen_factory=forbidden("popen"),
        )

    assert calls == {"provider": 0, "append": 0, "popen": 0}
    assert prepared["ledger"].read_bytes() == before


@pytest.mark.parametrize(
    ("classification", "retryable"),
    [("interrupted_no_commit", True), ("forbidden_drift", False)],
)
def test_recovery_failure_stops_current_phase_without_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    classification: str,
    retryable: bool,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    recovery_calls: list[int] = []
    execute_calls: list[int] = []

    def recover(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del lease, kwargs
        recovery_calls.append(spec.step_ordinal)
        return _outcome(
            spec,
            event_type="ATTEMPT_FAILED",
            classification=classification,
            retryable=retryable,
            suffix="f",
        )

    monkeypatch.setattr(
        continuity,
        "_open_attempt_spec",
        lambda history, schedule: prepared["schedule"][0],
    )
    monkeypatch.setattr(continuity, "_recover_dangling_collector_attempt", recover)
    _patch_step_executor(
        monkeypatch,
        lambda lease, spec, **kwargs: execute_calls.append(spec.step_ordinal)
        or _outcome(spec),
    )
    with pytest.raises(CollectorContinuityError, match="recovered attempt failed"):
        _call_phase(prepared)

    assert recovery_calls == [0]
    assert execute_calls == []


def test_recovered_complete_continues_with_the_remaining_phase_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    recovered = _outcome(prepared["schedule"][0], suffix="a")
    recovery_calls: list[int] = []
    execute_calls: list[int] = []

    monkeypatch.setattr(
        continuity,
        "_open_attempt_spec",
        lambda history, schedule: prepared["schedule"][0],
    )

    def recover(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del lease, kwargs
        recovery_calls.append(spec.step_ordinal)
        return recovered

    def execute(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del kwargs
        lease.verify()
        execute_calls.append(spec.step_ordinal)
        return _outcome(spec, suffix="b")

    monkeypatch.setattr(continuity, "_recover_dangling_collector_attempt", recover)
    _patch_step_executor(monkeypatch, execute)
    monkeypatch.setattr(
        continuity,
        "_completed_collector_ordinals",
        lambda history: {0} | set(execute_calls),
    )
    monkeypatch.setattr(
        continuity,
        "_verify_registered_collector_tail_state",
        lambda *args, **kwargs: None,
    )
    result = _call_phase(prepared)

    assert recovery_calls == [0]
    assert execute_calls == [1]
    assert [item.step_ordinal for item in result] == [0, 1]


def test_dangling_attempt_is_recovered_before_any_new_attempt_or_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        continuity._begin_collector_step_attempt(
            lease, spec, now=lambda: "2099-01-05T08:40:00+08:00"
        )
    starts_before = sum(
        event["event_type"] == "ATTEMPT_STARTED"
        for event in _ledger_history(prepared["ledger"])
    )
    popen_calls: list[object] = []

    with pytest.raises(CollectorContinuityError, match="recovered attempt failed"):
        _call_phase(
            prepared,
            popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )
    history = _ledger_history(prepared["ledger"])

    assert popen_calls == []
    assert sum(event["event_type"] == "ATTEMPT_STARTED" for event in history) == starts_before
    assert history[-1]["event_type"] == "ATTEMPT_FAILED"
    assert history[-1]["event"]["failure_classification"] == "interrupted_no_commit"
    assert history[-1]["event"]["retryable"] is True


def test_hot_journal_restart_records_controlled_recovery_before_phase_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        continuity._begin_collector_step_attempt(
            lease, spec, now=lambda: "2099-01-05T08:40:00+08:00"
        )
    journal = _leave_hot_delete_journal(prepared["database"])
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")

    with pytest.raises(CollectorContinuityError, match="recovered attempt failed"):
        _call_phase(prepared)

    history = _ledger_history(prepared["ledger"])
    assert [event["event_type"] for event in history[-3:]] == [
        "SQLITE_RECOVERY_STARTED",
        "SQLITE_RECOVERY_COMPLETED",
        "ATTEMPT_FAILED",
    ]
    assert not journal.exists()


@pytest.mark.parametrize("mutation", ["registration", "prerequisite"])
def test_registration_or_static_prerequisite_replacement_is_ledger_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    registration = Path(prepared["registration"])
    payload = json.loads(registration.read_bytes())
    if mutation == "registration":
        payload["status"] = "REPLACED"
    else:
        payload["prerequisites_sha256"] = "f" * 64
    registration.write_bytes(continuity.canonical_json_bytes(payload))
    before = prepared["ledger"].read_bytes()
    popen_calls: list[object] = []

    with pytest.raises(CollectorContinuityError):
        _call_phase(
            prepared,
            popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )

    assert popen_calls == []
    assert prepared["ledger"].read_bytes() == before


def test_live_static_prerequisite_drift_is_zero_append_zero_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before = prepared["ledger"].read_bytes()
    calls: list[object] = []
    monkeypatch.setattr(
        continuity,
        "_reverify_registered_collector_static_prerequisites",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CollectorContinuityError("collector static prerequisites drifted")
        ),
    )

    with pytest.raises(CollectorContinuityError, match="static prerequisites drifted"):
        _call_phase(
            prepared,
            popen_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []
    assert prepared["ledger"].read_bytes() == before


def test_static_prerequisites_are_reverified_between_phase_steps_before_attempt_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before_fds = set(os.listdir("/dev/fd"))
    real_executor = continuity._execute_collector_step_attempt
    ordinal_zero_completed = False
    reverify_states: list[bool] = []
    popen_calls: list[object] = []
    provider_calls: list[object] = []

    def reverify(*args: object, **kwargs: object) -> None:
        del args, kwargs
        reverify_states.append(ordinal_zero_completed)
        if ordinal_zero_completed:
            raise CollectorContinuityError(
                "collector static prerequisites drifted before ordinal 1"
            )

    def fake_popen(*args: object, **kwargs: object) -> object:
        popen_calls.append((args, kwargs))
        raise RuntimeError("ordinal 1 Popen must not be reached")

    def provider_must_not_run(*args: object, **kwargs: object) -> object:
        provider_calls.append((args, kwargs))
        raise AssertionError("ordinal 1 provider must not be reached")

    def execute(
        lease: object, spec: object, **kwargs: object
    ) -> CollectorAttemptOutcome:
        nonlocal ordinal_zero_completed
        if spec.step_ordinal != 0:
            return real_executor(lease, spec, **kwargs)
        launch = continuity._begin_collector_step_attempt(
            lease,
            spec,
            now=lambda: _PRE_OPEN,
        )
        try:
            empty_sha256 = hashlib.sha256(b"").hexdigest()
            raw = continuity._raw_result(
                "complete",
                "complete",
                launch.baseline.step_state,
            )
            process = continuity._CollectorProcessResult(
                True,
                0,
                empty_sha256,
                0,
                empty_sha256,
                0,
                False,
            )
            event_type, detail = continuity._terminal_attempt_event(
                launch,
                raw,
                process,
                process_launch_state="handle_obtained",
                finished_at="2099-01-05T08:46:00+08:00",
                failure_classification=None,
            )
            terminal = continuity._append_terminal_once(
                lease,
                launch,
                event_type=event_type,
                event=detail,
            )
            ordinal_zero_completed = True
            return CollectorAttemptOutcome(
                step_id=spec.step_id,
                step_ordinal=spec.step_ordinal,
                attempt_id=launch.attempt_id,
                terminal_event_sha256=str(terminal["event_sha256"]),
                terminal_event_type="ATTEMPT_COMPLETED",
                classification="complete",
                retryable=False,
                process_result_known=True,
                returncode=0,
                raw_class="complete",
            )
        finally:
            continuity._clear_nonce(launch._nonce_buffer)
            launch.nonce = b""

    monkeypatch.setattr(
        continuity,
        "_reverify_registered_collector_static_prerequisites",
        reverify,
    )
    monkeypatch.setattr(
        continuity,
        "_provider_call",
        provider_must_not_run,
        raising=False,
    )
    _patch_step_executor(monkeypatch, execute)

    error: BaseException | None = None
    try:
        _call_phase(prepared, popen_factory=fake_popen)
    except BaseException as exc:
        error = exc

    history = _ledger_history(prepared["ledger"])
    ordinal_zero_terminals = [
        event
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
        and event["event"]["step_ordinal"] == 0
    ]
    ordinal_one_starts = [
        event
        for event in history
        if event["event_type"] == "ATTEMPT_STARTED"
        and event["event"]["step_ordinal"] == 1
    ]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass
    after_fds = set(os.listdir("/dev/fd"))

    assert (
        reverify_states
        and reverify_states[0] is False
        and reverify_states.count(True) == 1
        and reverify_states[-1] is True
        and isinstance(error, CollectorContinuityError)
        and "static prerequisites drifted before ordinal 1" in str(error)
        and popen_calls == []
        and provider_calls == []
        and len(ordinal_zero_terminals) == 1
        and ordinal_one_starts == []
        and after_fds == before_fds
    ), {
        "reverify_states": reverify_states,
        "error": repr(error),
        "popen_calls": len(popen_calls),
        "provider_calls": len(provider_calls),
        "ordinal_zero_terminals": len(ordinal_zero_terminals),
        "ordinal_one_starts": len(ordinal_one_starts),
        "fd_added": sorted(after_fds - before_fds),
        "fd_removed": sorted(before_fds - after_fds),
    }


@pytest.mark.parametrize("drift_at", ["initial", "terminal"])
def test_initial_or_terminal_state_drift_stops_before_further_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift_at: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    before = prepared["ledger"].read_bytes()
    verification_calls = 0
    execute_calls: list[int] = []

    def verify(*args: object, **kwargs: object) -> None:
        nonlocal verification_calls
        del args, kwargs
        verification_calls += 1
        if drift_at == "initial" or verification_calls == 2:
            raise CollectorContinuityError(f"collector {drift_at} state drifted")

    def execute(lease: object, spec: object, **kwargs: object) -> CollectorAttemptOutcome:
        del kwargs
        lease.verify()
        execute_calls.append(spec.step_ordinal)
        return _outcome(spec)

    monkeypatch.setattr(continuity, "_verify_registered_collector_tail_state", verify)
    _patch_step_executor(monkeypatch, execute)
    monkeypatch.setattr(
        continuity,
        "_completed_collector_ordinals",
        lambda history: set(execute_calls),
    )

    with pytest.raises(CollectorContinuityError, match=f"{drift_at} state drifted"):
        _call_phase(prepared)
    assert execute_calls == ([] if drift_at == "initial" else [0, 1])
    assert prepared["ledger"].read_bytes() == before


def test_registered_capture_v4_uses_only_continuity_orchestrator_and_safe_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registration = _capture_registration(tmp_path)
    outcome = CollectorAttemptOutcome(
        step_id="pre_open_context",
        step_ordinal=0,
        attempt_id="attempt-safe",
        terminal_event_sha256="a" * 64,
        terminal_event_type="ATTEMPT_COMPLETED",
        classification="complete",
        retryable=False,
        process_result_known=True,
        returncode=0,
        raw_class="complete",
    )
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        registered_panel_capture,
        "capture_phase",
        lambda *args, **kwargs: pytest.fail("registered /4 called legacy capture_phase"),
    )
    def execute(*args: object, **kwargs: object) -> tuple[CollectorAttemptOutcome, ...]:
        calls.append({"args": args, **kwargs})
        return (outcome,)

    monkeypatch.setattr(
        registered_panel_capture,
        "execute_registered_collector_phase",
        execute,
        raising=False,
    )
    result = registered_panel_capture.capture_registered_panel(
        registration,
        database=tmp_path / "evidence.sqlite",
        effective_date="2026-08-12",
        phase="pre_open",
    )

    assert len(calls) == 1
    assert calls[0]["database"] == tmp_path / "evidence.sqlite"
    assert calls[0]["effective_date"] == "2026-08-12"
    assert calls[0]["phase"] == "pre_open"
    assert result == [asdict(outcome)]
    assert not ({"nonce", "lease_fd", "stdout", "stderr"} & set(result[0]))


def test_registered_capture_maps_continuity_error_without_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registered_panel_capture,
        "capture_phase",
        lambda *args, **kwargs: pytest.fail("continuity failure used legacy fallback"),
    )
    monkeypatch.setattr(
        registered_panel_capture,
        "execute_registered_collector_phase",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CollectorContinuityError("phase authority rejected")
        ),
        raising=False,
    )
    with pytest.raises(RegisteredPanelCaptureError) as exc_info:
        registered_panel_capture.capture_registered_panel(
            _capture_registration(tmp_path),
            database=tmp_path / "evidence.sqlite",
            effective_date="2026-08-12",
            phase="pre_open",
        )
    assert isinstance(exc_info.value.__cause__, CollectorContinuityError)


def test_busy_collector_materializer_fails_before_reads_mkdir_readiness_or_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector_dir = tmp_path / "collector"
    collector_dir.mkdir()
    prepared = _prepared(collector_dir, monkeypatch)
    inputs = _materializer_inputs(tmp_path / "inputs")
    inputs["database_file"] = prepared["database"]
    inputs["registration_file"] = prepared["registration"]
    snapshot_staging = tmp_path / "snapshots"
    snapshot_staging.mkdir()
    inputs["snapshot_staging_directory"] = snapshot_staging
    destination = tmp_path / "closure"
    calls: list[str] = []

    monkeypatch.setattr(
        provider_materializer,
        "_read_regular",
        lambda *args, **kwargs: calls.append("read") or pytest.fail(
            "collector materialization read before task 4.1 gate"
        ),
    )
    monkeypatch.setattr(
        provider_materializer,
        "check_full_execution_readiness",
        lambda *args, **kwargs: calls.append("readiness") or pytest.fail(
            "collector materialization reached readiness"
        ),
    )
    monkeypatch.setattr(
        provider_materializer,
        "export_verified_provider_receipt",
        lambda *args, **kwargs: calls.append("export") or pytest.fail(
            "collector materialization reached export"
        ),
    )

    lease = continuity.acquire_collector_phase_lease(prepared["ledger"])
    try:
        with pytest.raises(
            (ProviderMaterializationError, CollectorContinuityError),
            match=r"continuity|lease|busy|lock",
        ):
            provider_materializer.materialize_provider_bundle(
                output_dir=destination, **inputs
            )
    finally:
        lease.close()

    assert calls == []
    assert not destination.exists()


def test_snapshot_holds_collector_lease_until_backup_release_then_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_api = getattr(
        continuity, "create_registered_collector_materialization_snapshot", None
    )
    if not callable(snapshot_api):
        pytest.fail(
            "missing task 4.1 API: create_registered_collector_materialization_snapshot"
        )
    collector_dir = tmp_path / "collector"
    collector_dir.mkdir()
    prepared = _prepared(collector_dir, monkeypatch)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        for spec in prepared["schedule"]:
            _append_completed_attempt(lease, spec)
    original_connect = sqlite3.connect
    backup_entered = threading.Event()
    backup_release = threading.Event()
    errors: list[BaseException] = []

    def connect(*args, **kwargs):
        base_factory = kwargs.get("factory", sqlite3.Connection)

        class BlockingConnection(base_factory):
            def backup(self, target, *backup_args, **backup_kwargs):
                backup_entered.set()
                if not backup_release.wait(timeout=5):
                    raise RuntimeError("snapshot backup release timed out")
                return super().backup(target, *backup_args, **backup_kwargs)

        kwargs["factory"] = BlockingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    staging_parent = tmp_path / "snapshot"
    staging_parent.mkdir()

    def snapshot() -> None:
        try:
            snapshot_api(
                registration_file=prepared["registration"],
                database=prepared["database"],
                staging_directory=staging_parent,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=snapshot)
    worker.start()
    try:
        assert backup_entered.wait(timeout=5), errors
        with pytest.raises(CollectorContinuityError):
            _call_phase(prepared)
    finally:
        backup_release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass
