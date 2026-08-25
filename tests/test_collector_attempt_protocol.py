from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import CollectorContinuityError
from test_collector_ledger import (
    _build,
    _genesis_event,
)
from test_collector_step_state import (
    _bound_registration,
    _prepare_collector,
    _schedule,
    _step_state,
)


_ALLOWED = frozenset({"collection_receipts", "daily", "sync_coverage"})
_RAW_BEFORE_SCHEMA = "stockdata-forward-collector-step-raw-before/1"
_STARTED_AT = "2099-01-05T15:10:00+08:00"
_FINISHED_AT = "2099-01-05T15:20:00+08:00"


def _api(name: str) -> Any:
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing task 2.5 API: {name}")
    return value


def _raw_before(
    *, rows: dict[str, list[dict[str, object]]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": _RAW_BEFORE_SCHEMA,
        "selector_rows": (
            {table: [] for table in sorted(_ALLOWED)} if rows is None else rows
        ),
    }


def _start_detail(*, attempt_id: str = "attempt-0001") -> dict[str, object]:
    state = _step_state(_ALLOWED)
    return {
        "registration_sha256": "c" * 64,
        "database_uuid": "a" * 64,
        "session": "2099-01-05",
        "phase": "post_close",
        "step_id": "post_close_prices",
        "step_ordinal": 3,
        "attempt_id": attempt_id,
        "command_sha256": "2" * 64,
        "lease_nonce_sha256": "5" * 64,
        "started_at": _STARTED_AT,
        "state_before_sha256": state["collector_state_sha256"],
        "step_state_before": state,
        "step_raw_before": _raw_before(),
    }


def _terminal_detail(
    event_type: str = "ATTEMPT_COMPLETED",
    *,
    process_result_known: bool = True,
    recovered: bool = False,
    returncode: int | None = 0,
    stdout_sha256: str | None = "3" * 64,
    stdout_bytes: int | None = 0,
    stderr_sha256: str | None = "4" * 64,
    stderr_bytes: int | None = 0,
) -> dict[str, object]:
    start = _start_detail()
    start.pop("lease_nonce_sha256")
    state = _step_state(_ALLOWED)
    terminal = {
        **start,
        "state_after_sha256": state["collector_state_sha256"],
        "step_state_after": state,
        "process_result_known": process_result_known,
        "returncode": returncode,
        "stdout_sha256": stdout_sha256,
        "stdout_bytes": stdout_bytes,
        "stderr_sha256": stderr_sha256,
        "stderr_bytes": stderr_bytes,
    }
    if event_type == "ATTEMPT_COMPLETED":
        terminal.update(
            {
                "completed_at": _FINISHED_AT,
                "recovered": recovered,
                "verifier_id": "stockdata-forward-collector-raw-postcondition/1",
            }
        )
    else:
        terminal.update(
            {
                "failed_at": _FINISHED_AT,
                "failure_classification": "child_process_failed_after_complete",
                "retryable": False,
            }
        )
    return terminal


def _attempt_chain(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    return registration, _build((genesis, registration), "ATTEMPT_STARTED", _start_detail())


def _prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    registration = _bound_registration(database)
    return {
        "database": database,
        "ledger": ledger,
        "registration": registration,
        "schedule": _schedule(database),
    }


def _launch_field(launch: object, *names: str) -> object:
    for name in names:
        if isinstance(launch, dict) and name in launch:
            return launch[name]
        if hasattr(launch, name):
            return getattr(launch, name)
    pytest.fail(f"attempt launch has none of fields {names!r}")


def _execute_with_fake_spawn(
    executor: Any,
    lease: object,
    spec: object,
    fake_spawn: Any,
) -> object:
    parameters = inspect.signature(executor).parameters
    for name in ("popen_factory", "spawn", "spawn_child", "child_spawn"):
        if name in parameters:
            return executor(lease, spec, **{name: fake_spawn})
    pytest.fail("attempt executor must expose deterministic fake-child injection")


def test_attempt_started_requires_raw_before_selector_rows_and_complete_state() -> None:
    builder = continuity.build_collector_ledger_event
    genesis = _genesis_event(Path("/tmp/task-2-5-attempt-schema"))
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = builder(
        previous_event=registration,
        event_type="ATTEMPT_STARTED",
        event=_start_detail(),
    )
    assert started["event"]["step_raw_before"] == _raw_before()
    assert started["event"]["attempt_id"]
    assert len(started["event"]["lease_nonce_sha256"]) == 64


@pytest.mark.parametrize(
    "mutation",
    ["missing-raw", "missing-selector-rows", "raw-extra", "state-missing"],
)
def test_attempt_started_rejects_incomplete_before_image(mutation: str) -> None:
    detail = _start_detail()
    if mutation == "missing-raw":
        detail.pop("step_raw_before")
    elif mutation == "missing-selector-rows":
        detail["step_raw_before"] = {"schema_version": _RAW_BEFORE_SCHEMA}
    elif mutation == "raw-extra":
        detail["step_raw_before"] = _raw_before()
        detail["step_raw_before"]["unexpected"] = True
    else:
        detail["step_state_before"] = {"schema_version": continuity.COLLECTOR_STEP_STATE_SCHEMA}
    with pytest.raises(CollectorContinuityError):
        continuity.validate_collector_ledger_event(
            continuity.build_collector_ledger_event(
                previous_event=None,
                event_type="ATTEMPT_STARTED",
                event=detail,
            )
        )


def test_terminal_known_result_requires_all_output_metadata() -> None:
    registration, started = _attempt_chain(Path("/tmp/task-2-5-terminal-schema"))
    del registration
    terminal = continuity.build_collector_ledger_event(
        previous_event=started,
        event_type="ATTEMPT_COMPLETED",
        event=_terminal_detail(),
    )
    assert terminal["event"]["process_result_known"] is True
    assert terminal["event"]["returncode"] == 0
    assert terminal["event"]["stdout_bytes"] == 0


@pytest.mark.parametrize(
    "mutation",
    ["unknown-not-recovered", "unknown-returncode", "unknown-stdout", "unknown-stderr"],
)
def test_unknown_process_result_is_only_valid_for_recovered_terminal(mutation: str) -> None:
    detail = _terminal_detail(process_result_known=False, recovered=True)
    if mutation == "unknown-not-recovered":
        detail["recovered"] = False
    elif mutation == "unknown-returncode":
        detail["returncode"] = 0
    elif mutation == "unknown-stdout":
        detail["stdout_sha256"] = "3" * 64
        detail["stdout_bytes"] = 0
    else:
        detail["stderr_sha256"] = "4" * 64
        detail["stderr_bytes"] = 0
    with pytest.raises(CollectorContinuityError):
        continuity.validate_collector_ledger_event(
            continuity.build_collector_ledger_event(
                previous_event=None,
                event_type="ATTEMPT_COMPLETED",
                event=detail,
            )
        )


def test_phase_event_append_requires_held_lease() -> None:
    append = _api("_append_collector_phase_event")
    with pytest.raises(CollectorContinuityError):
        append(None, event_type="ATTEMPT_STARTED", event=_start_detail())


def test_attempt_start_fsync_failure_does_not_spawn_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    executor = _api("_execute_collector_step_attempt")
    calls: list[object] = []

    def fail_append(*args: object, **kwargs: object) -> object:
        raise OSError("injected attempt-start fsync failure")

    def fake_spawn(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("child was spawned after start fsync failure")

    monkeypatch.setattr(continuity, "_append_collector_phase_event", fail_append, raising=False)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        with pytest.raises(CollectorContinuityError):
            _execute_with_fake_spawn(executor, lease, prepared["schedule"][0], fake_spawn)
    assert calls == []


def test_attempt_nonce_is_32_bytes_and_fresh_per_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    begin = _api("_begin_collector_step_attempt")
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = _prepared(tmp_path / "first", monkeypatch)
    second = _prepared(tmp_path / "second", monkeypatch)
    with continuity.acquire_collector_phase_lease(first["ledger"]) as lease:
        launch_a = begin(lease, first["schedule"][0])
    with continuity.acquire_collector_phase_lease(second["ledger"]) as lease:
        launch_b = begin(lease, second["schedule"][0])
    nonce_a = _launch_field(launch_a, "nonce", "nonce_bytes", "lease_nonce")
    nonce_b = _launch_field(launch_b, "nonce", "nonce_bytes", "lease_nonce")
    assert isinstance(nonce_a, (bytes, bytearray)) and len(nonce_a) == 32
    assert isinstance(nonce_b, (bytes, bytearray)) and len(nonce_b) == 32
    assert nonce_a != nonce_b
    assert _launch_field(launch_a, "attempt_id") != _launch_field(launch_b, "attempt_id")


@pytest.mark.parametrize(
    ("nonce_payload", "message"),
    [
        (b"", "nonce pipe length is invalid"),
        (b"x" * 31, "nonce pipe length is invalid"),
        (b"x" * 33, "nonce pipe length is invalid"),
        (b"x" * 32 + b"y", "nonce pipe length is invalid"),
        (None, "nonce does not match active attempt"),
    ],
    ids=["empty", "short", "long", "trailing-byte", "wrong-32-byte"],
)
def test_invalid_nonce_stream_is_rejected_before_provider_or_write(
    tmp_path: Path,
    nonce_payload: bytes | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    open_writer = _api("open_collector_child_writer_authority")
    provider_calls: list[object] = []
    monkeypatch.setattr(continuity, "_provider_call", lambda *a, **k: provider_calls.append((a, k)), raising=False)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = _api("_begin_collector_step_attempt")(lease, spec)
        if nonce_payload is None:
            wrong_nonce = bytearray(launch.nonce)
            wrong_nonce[0] ^= 1
            nonce_payload = bytes(wrong_nonce)
            assert len(nonce_payload) == 32
            assert nonce_payload != launch.nonce
        with lease.child_handoff() as handoff:
            child_lease_fd = continuity.os.dup(handoff.fd)
            read_fd, write_fd = continuity.os.pipe()
            continuity.os.write(write_fd, nonce_payload)
            continuity.os.close(write_fd)
            environment = continuity._collector_attempt_child_environment(
                launch, lease_fd=child_lease_fd, nonce_fd=read_fd
            )
            with pytest.raises(CollectorContinuityError, match=message):
                open_writer(argv=list(spec.command), environ=environment)
    assert provider_calls == []


def test_child_spawn_arguments_cannot_contain_nonce_or_nonce_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    executor = _api("_execute_collector_step_attempt")
    seen: dict[str, object] = {}

    def fake_spawn(*args: object, **kwargs: object) -> object:
        seen["args"] = args
        seen["kwargs"] = kwargs
        raise RuntimeError("stop after inspecting fake spawn")

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        with pytest.raises(CollectorContinuityError):
            _execute_with_fake_spawn(executor, lease, prepared["schedule"][0], fake_spawn)
    assert seen
    events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
    nonce_hash = events[-1]["event"]["lease_nonce_sha256"]
    args = seen.get("args", ())
    kwargs = seen.get("kwargs", {})
    surfaces = [args]
    if isinstance(kwargs, dict):
        surfaces.extend(kwargs.get(name) for name in ("env", "stdout", "stderr"))
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            assert not any("nonce" in str(key).lower() for key in environment)
    assert all(nonce_hash not in repr(surface) for surface in surfaces)


def test_terminal_classification_is_exact_and_one_shot() -> None:
    classifier = _api("_classify_attempt_terminal")
    raw_result = _api("_raw_result")
    process_result = _api("_CollectorProcessResult")
    state = _step_state(_ALLOWED)
    cases = {
        "complete": ("complete", process_result(True, 0, "3" * 64, 0, "4" * 64, 0, False), None),
        "stdout-only": ("complete", process_result(False, None, None, None, None, None, True), "postflight_authority_failure"),
        "zero-missing-proof": ("partial_prices", process_result(True, 0, "3" * 64, 0, "4" * 64, 0, False), "child_partial_prices"),
        "nonzero-complete": ("complete", process_result(True, 1, "3" * 64, 0, "4" * 64, 0, False), "child_process_failed_after_complete"),
        "forbidden": ("forbidden", process_result(True, 0, "3" * 64, 0, "4" * 64, 0, False), "forbidden_drift"),
    }
    for name, (raw_class, process, expected) in cases.items():
        raw = raw_result(raw_class, name, state)
        assert classifier(raw, process) == expected, name
