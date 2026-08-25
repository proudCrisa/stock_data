from __future__ import annotations

from io import BytesIO
import gc
import hashlib
import json
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

import stockdata.cli as cli
import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import CollectorContinuityError
from test_collector_attempt_protocol import (
    _attempt_chain,
    _prepared,
    _terminal_detail,
)
from test_collector_step_state import _step_state


_CHILD_ENVIRONMENT = (
    "STOCKDATA_COLLECTOR_REGISTRATION_FILE",
    "STOCKDATA_COLLECTOR_LEDGER_FILE",
    "STOCKDATA_COLLECTOR_LEDGER_IDENTITY",
    "STOCKDATA_COLLECTOR_ATTEMPT_ID",
    "STOCKDATA_COLLECTOR_SESSION",
    "STOCKDATA_COLLECTOR_PHASE",
    "STOCKDATA_COLLECTOR_STEP_ID",
    "STOCKDATA_COLLECTOR_STEP_ORDINAL",
    "STOCKDATA_COLLECTOR_COMMAND_SHA256",
    "STOCKDATA_COLLECTOR_REGISTRATION_SHA256",
    "STOCKDATA_COLLECTOR_DATABASE_UUID",
    "STOCKDATA_COLLECTOR_LEASE_FD",
    "STOCKDATA_COLLECTOR_PIPE_FD",
)


def _complete_child_environment() -> dict[str, str]:
    return {
        "STOCKDATA_COLLECTOR_REGISTRATION_FILE": "/tmp/registration.json",
        "STOCKDATA_COLLECTOR_LEDGER_FILE": "/tmp/collector-ledger.jsonl",
        "STOCKDATA_COLLECTOR_LEDGER_IDENTITY": "identity",
        "STOCKDATA_COLLECTOR_ATTEMPT_ID": "attempt-0001",
        "STOCKDATA_COLLECTOR_SESSION": "2099-01-05",
        "STOCKDATA_COLLECTOR_PHASE": "post_close",
        "STOCKDATA_COLLECTOR_STEP_ID": "post_close_prices",
        "STOCKDATA_COLLECTOR_STEP_ORDINAL": "3",
        "STOCKDATA_COLLECTOR_COMMAND_SHA256": "1" * 64,
        "STOCKDATA_COLLECTOR_REGISTRATION_SHA256": "2" * 64,
        "STOCKDATA_COLLECTOR_DATABASE_UUID": "3" * 64,
        "STOCKDATA_COLLECTOR_LEASE_FD": "3",
        "STOCKDATA_COLLECTOR_PIPE_FD": "4",
    }


def _api(name: str):
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing task 2.5 API: {name}")
    return value


def _process(*, state: str, known: bool, returncode: int | None, plumbing: bool):
    return SimpleNamespace(
        process_launch_state=state,
        process_result_known=known,
        returncode=returncode,
        stdout_sha256=None if not known else "3" * 64,
        stdout_bytes=None if not known else 0,
        stderr_sha256=None if not known else "4" * 64,
        stderr_bytes=None if not known else 0,
        plumbing_failed=plumbing,
    )


def _fd_set() -> set[int]:
    return {int(name) for name in os.listdir("/dev/fd") if name.isdigit()}


def _capture_launches(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    launches: list[object] = []
    original = continuity._begin_collector_step_attempt

    def capture(*args: object, **kwargs: object) -> object:
        launch = original(*args, **kwargs)
        launches.append(launch)
        return launch

    monkeypatch.setattr(continuity, "_begin_collector_step_attempt", capture)
    return launches


def _assert_launch_nonce_cleared(launch: object) -> None:
    assert launch.nonce == b""
    assert bytes(launch._nonce_buffer) == b"\x00" * 32


def _clear_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CHILD_ENVIRONMENT + ("STOCKDATA_COLLECTOR_EXTRA",):
        monkeypatch.delenv(name, raising=False)


def test_process_launch_state_matrix_is_exact() -> None:
    cases = (
        ("not_invoked", False, None, "child_launch_failed", True),
        ("handle_obtained", True, 1, "child_process_failed_after_complete", False),
        ("indeterminate", False, None, "interrupted_no_commit", True),
    )
    for state, known, returncode, classification, retryable in cases:
        _, started = _attempt_chain(Path("/tmp/task-2-5-launch-state"))
        detail = _terminal_detail(
            "ATTEMPT_FAILED",
            process_result_known=known,
            recovered=False,
            returncode=returncode,
            stdout_sha256=None if not known else "3" * 64,
            stdout_bytes=None if not known else 0,
            stderr_sha256=None if not known else "4" * 64,
            stderr_bytes=None if not known else 0,
        )
        detail["process_launch_state"] = state
        detail["recovered"] = state == "indeterminate"
        detail["verifier_id"] = "stockdata-forward-collector-raw-postcondition/1"
        detail["failure_classification"] = classification
        detail["retryable"] = retryable
        event = continuity.build_collector_ledger_event(
            previous_event=started,
            event_type="ATTEMPT_FAILED",
            event=detail,
        )
        continuity.validate_collector_ledger_event(event)

    _, started = _attempt_chain(Path("/tmp/task-2-5-launch-state-invalid"))
    invalid = _terminal_detail(
        "ATTEMPT_FAILED",
        process_result_known=True,
        returncode=0,
        stdout_sha256="3" * 64,
        stdout_bytes=0,
        stderr_sha256="4" * 64,
        stderr_bytes=0,
    )
    invalid["process_launch_state"] = "not_invoked"
    invalid["recovered"] = False
    invalid["verifier_id"] = "stockdata-forward-collector-raw-postcondition/1"
    invalid["failure_classification"] = "child_launch_failed"
    invalid["retryable"] = True
    with pytest.raises(CollectorContinuityError):
        continuity.validate_collector_ledger_event(
            continuity.build_collector_ledger_event(
                previous_event=started,
                event_type="ATTEMPT_FAILED",
                event=invalid,
            )
        )


def test_pre_popen_failure_appends_exact_child_launch_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    execute = _api("_execute_collector_step_attempt")
    launches = _capture_launches(monkeypatch)
    popen_calls: list[object] = []
    gc.collect()
    before_fds = _fd_set()

    def fail_before_popen(*args: object, **kwargs: object):
        popen_calls.append((args, kwargs))
        raise AssertionError("Popen must not be reached")

    def fail_pipe() -> object:
        raise OSError("child plumbing failed before Popen")

    monkeypatch.setattr(continuity.os, "pipe", fail_pipe)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = execute(
            lease,
            prepared["schedule"][0],
            popen_factory=fail_before_popen,
        )
        events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
    assert _fd_set() == before_fds
    assert len(launches) == 1
    _assert_launch_nonce_cleared(launches[0])
    assert popen_calls == []
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "child_launch_failed"
    terminal = events[-1]
    assert terminal["event_type"] == "ATTEMPT_FAILED"
    assert terminal["event"]["process_launch_state"] == "not_invoked"
    assert terminal["event"]["failure_classification"] == "child_launch_failed"


def test_popen_exception_is_not_encoded_as_not_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    execute = _api("_execute_collector_step_attempt")
    launches = _capture_launches(monkeypatch)
    calls: list[object] = []
    before_fds = _fd_set()

    def popen_raises(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        raise OSError("custom Popen failure")

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        with pytest.raises(CollectorContinuityError, match="indeterminate"):
            execute(lease, prepared["schedule"][0], popen_factory=popen_raises)
    assert _fd_set() == before_fds
    assert len(calls) == 1
    assert len(launches) == 1
    _assert_launch_nonce_cleared(launches[0])


def test_child_handle_exception_is_spawned_and_leaves_dangling_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStream:
        def read(self, size: int) -> bytes:
            del size
            raise OSError("child handle read failed")

    class Process:
        stdout = BrokenStream()
        stderr = BrokenStream()

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return 1

    prepared = _prepared(tmp_path, monkeypatch)
    execute = _api("_execute_collector_step_attempt")
    launches = _capture_launches(monkeypatch)
    before_fds = _fd_set()
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        with pytest.raises(CollectorContinuityError, match="indeterminate"):
            execute(
                lease,
                prepared["schedule"][0],
                popen_factory=lambda *args, **kwargs: Process(),
            )
        events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
    assert _fd_set() == before_fds
    assert len(launches) == 1
    _assert_launch_nonce_cleared(launches[0])
    assert events[-1]["event_type"] == "ATTEMPT_STARTED"


def test_real_stdout_and_zero_exit_without_raw_evidence_cannot_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = b"collector claims success\n"

    class Process:
        def __init__(self) -> None:
            self.stdout = BytesIO(stdout)
            self.stderr = BytesIO(b"")
            self.returncode = 0

        def wait(self) -> int:
            return self.returncode

    prepared = _prepared(tmp_path, monkeypatch)
    launches = _capture_launches(monkeypatch)
    before_fds = _fd_set()
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = _api("_execute_collector_step_attempt")(
            lease,
            prepared["schedule"][0],
            popen_factory=lambda *args, **kwargs: Process(),
        )
        events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())

    assert _fd_set() == before_fds
    assert len(launches) == 1
    _assert_launch_nonce_cleared(launches[0])
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "child_no_commit"
    terminal = events[-1]
    assert terminal["event_type"] == "ATTEMPT_FAILED"
    assert terminal["event"]["returncode"] == 0
    assert terminal["event"]["stdout_bytes"] == len(stdout)
    assert terminal["event"]["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()


def test_terminal_written_then_exception_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    execute = _api("_execute_collector_step_attempt")
    raw = _api("_raw_result")(
        "forbidden",
        "forbidden_evidence",
        _step_state(prepared["schedule"][0].allowed_tables),
    )
    process = _process(state="spawned", known=True, returncode=0, plumbing=False)
    monkeypatch.setattr(
        continuity,
        "_run_collector_child",
        lambda *a, **k: SimpleNamespace(
            launch_state="handle_obtained", process=process
        ),
    )
    monkeypatch.setattr(
        continuity,
        "verify_collector_raw_postcondition",
        lambda *a, **k: raw,
    )
    original_append = continuity._append_verified_ledger_payload
    append_calls = 0

    def append_then_raise(*args: object, **kwargs: object):
        nonlocal append_calls
        result = original_append(*args, **kwargs)
        append_calls += 1
        if append_calls == 2:
            raise OSError("terminal append acknowledgement lost")
        return result

    monkeypatch.setattr(continuity, "_append_verified_ledger_payload", append_then_raise)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = execute(lease, prepared["schedule"][0])
        events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
        assert append_calls == 2
        assert outcome.terminal_event_type == "ATTEMPT_FAILED"
        assert events[-1]["event_type"] == "ATTEMPT_FAILED"
        assert sum(event["event_type"] == "ATTEMPT_FAILED" for event in events) == 1
        with pytest.raises(CollectorContinuityError):
            _api("_attempt_history_for_spec")(
                lease, prepared["schedule"][0], enforce_phase_order=True
            )


@pytest.mark.parametrize(
    ("label", "environment", "marker_allowed", "writer_allowed"),
    [
        (
            "complete",
            _complete_child_environment(),
            False,
            True,
        ),
        (
            "partial",
            {
                "STOCKDATA_COLLECTOR_REGISTRATION_FILE": "/tmp/registration.json",
                "STOCKDATA_COLLECTOR_ATTEMPT_ID": "attempt-0001",
                "STOCKDATA_COLLECTOR_LEASE_FD": "3",
                "STOCKDATA_COLLECTOR_PIPE_FD": "4",
            },
            False,
            False,
        ),
        (
            "extra",
            {**_complete_child_environment(), "STOCKDATA_COLLECTOR_EXTRA": "forbidden"},
            False,
            False,
        ),
    ],
)
def test_cli_child_environment_is_checked_before_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    environment: dict[str, str],
    marker_allowed: bool,
    writer_allowed: bool,
) -> None:
    del label
    _clear_child_environment(monkeypatch)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    marker_calls: list[Path] = []
    writer_calls: list[object] = []
    dispatch_calls: list[object] = []
    monkeypatch.setattr(
        continuity,
        "database_has_collector_genesis",
        lambda database: marker_calls.append(database) or True,
    )

    def stop_after_writer_validation(*args: object, **kwargs: object):
        writer_calls.append((args, kwargs))
        raise CollectorContinuityError("stop after child validation")

    monkeypatch.setattr(
        continuity,
        "open_collector_child_writer_authority",
        stop_after_writer_validation,
    )
    monkeypatch.setattr(
        cli,
        "_run_cache_command",
        lambda *args, **kwargs: dispatch_calls.append((args, kwargs)),
    )
    argv = [
        "forward-capture",
        "--database",
        str(tmp_path / "collector.sqlite"),
        "--codes",
        "000001.SZ",
        "--start",
        "2099-01-05",
        "--source",
        "tencent",
        "--adjustment-version",
        "tencent-qt-daily-v1",
    ]
    with pytest.raises(CollectorContinuityError):
        cli.main(argv)
    assert bool(marker_calls) is marker_allowed
    assert bool(writer_calls) is writer_allowed
    assert dispatch_calls == []


@pytest.mark.parametrize("schedule_index", [0, 1, 3])
def test_marked_standalone_collector_commands_fail_before_cache_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_index: int,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][schedule_index]
    _clear_child_environment(monkeypatch)
    before_database = prepared["database"].read_bytes()
    before_ledger = prepared["ledger"].read_bytes()
    dispatch_calls: list[object] = []

    def forbidden_dispatch(*args: object, **kwargs: object) -> object:
        dispatch_calls.append((args, kwargs))
        raise AssertionError("collector command reached Cache/provider dispatch")

    monkeypatch.setattr(cli, "_run_cache_command", forbidden_dispatch)
    with pytest.raises(
        CollectorContinuityError, match="child attempt environment is invalid"
    ):
        cli.main(list(spec.command[3:]))

    assert dispatch_calls == []
    assert prepared["database"].read_bytes() == before_database
    assert prepared["ledger"].read_bytes() == before_ledger


@pytest.mark.parametrize(
    ("command", "provider_target"),
    [
        (
            "forward-context-capture",
            "stockdata.forward_context.capture_forward_context",
        ),
        (
            "forward-corporate-actions-capture",
            "stockdata.forward_corporate_actions.capture_forward_corporate_actions",
        ),
        ("forward-capture", "stockdata.forward_capture.capture_forward_evidence"),
    ],
)
def test_noncollector_forward_commands_keep_original_local_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    provider_target: str,
) -> None:
    from stockdata.cache import Cache

    _clear_child_environment(monkeypatch)
    database = tmp_path / "ordinary.sqlite"
    cache = Cache(database)
    cache.close()
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def local_provider(cache: object, *args: object, **kwargs: object) -> dict[str, object]:
        calls.append((cache, args, kwargs))
        cache.close()
        return {"command": command, "local_fake": True}

    monkeypatch.setattr(provider_target, local_provider)
    if command == "forward-capture":
        argv = [
            command,
            "--database",
            str(database),
            "--codes",
            "000001.SZ",
            "--start",
            "2099-01-05",
            "--source",
            "tencent",
            "--adjustment-version",
            "tencent-qt-daily-v1",
        ]
    else:
        argv = [command, "--database", str(database), "--date", "2099-01-05"]

    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"command": command, "local_fake": True}
    assert captured.err == ""
    assert len(calls) == 1


def test_valid_active_attempt_cli_passes_writer_token_to_cache_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import stockdata.cache as cache_module
    import stockdata.forward_context as forward_context

    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    begin = _api("_begin_collector_step_attempt")
    original_close = continuity.close_collector_writer_authority
    cache_tokens: list[object] = []
    closed_nonces: list[bytearray] = []
    provider_calls: list[object] = []

    class FakeCache:
        def __init__(self, database: Path, *, writer_token: object | None = None):
            assert Path(database) == prepared["database"]
            continuity.require_collector_writer(
                writer_token,
                database_path=database,
                step_id=spec.step_id,
                session=spec.session,
            )
            cache_tokens.append(writer_token)

    def local_provider(cache: object, effective_date: str) -> dict[str, object]:
        provider_calls.append(cache)
        assert effective_date == spec.session
        return {"local_fake": True, "step_id": spec.step_id}

    def record_close(token: object) -> None:
        binding = continuity._COLLECTOR_WRITE_BINDINGS[token]
        closed_nonces.append(binding.nonce)
        original_close(token)

    monkeypatch.setattr(cache_module, "Cache", FakeCache)
    monkeypatch.setattr(forward_context, "capture_forward_context", local_provider)
    monkeypatch.setattr(continuity, "close_collector_writer_authority", record_close)
    _clear_child_environment(monkeypatch)

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = begin(lease, spec)
        raw_nonce = bytes(launch.nonce)
        nonce_sha256 = hashlib.sha256(raw_nonce).hexdigest()
        before_fds = _fd_set()
        with lease.child_handoff() as handoff:
            child_lease_fd = os.dup(handoff.fd)
            nonce_read, nonce_write = os.pipe()
            os.write(nonce_write, raw_nonce)
            os.close(nonce_write)
            child_environment = continuity._collector_attempt_child_environment(
                launch, lease_fd=child_lease_fd, nonce_fd=nonce_read
            )
            for name, value in child_environment.items():
                monkeypatch.setenv(name, value)
            assert cli.main(list(spec.command[3:])) == 0
        assert _fd_set() == before_fds

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"local_fake": True, "step_id": spec.step_id}
    assert captured.err == ""
    assert len(cache_tokens) == 1
    assert len(provider_calls) == 1
    assert len(closed_nonces) == 1
    assert bytes(closed_nonces[0]) == b"\x00" * 32
    assert all(name not in os.environ for name in _CHILD_ENVIRONMENT)
    visible = "\n".join(
        [*map(str, spec.command), *child_environment.keys(), *child_environment.values(), captured.out, captured.err]
    )
    assert raw_nonce.hex() not in visible
    assert nonce_sha256 not in visible
    continuity._clear_nonce(launch._nonce_buffer)
    launch.nonce = b""


@pytest.mark.parametrize("mutation", ["wrong-command", "reordered", "wrong-kind"])
def test_active_attempt_rejects_nonexact_cli_argv_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    begin = _api("_begin_collector_step_attempt")
    dispatch_calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "_run_cache_command",
        lambda *args, **kwargs: dispatch_calls.append((args, kwargs)),
    )
    _clear_child_environment(monkeypatch)

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = begin(lease, spec)
        before_fds = _fd_set()
        with lease.child_handoff() as handoff:
            child_lease_fd = os.dup(handoff.fd)
            nonce_read, nonce_write = os.pipe()
            os.write(nonce_write, bytes(launch.nonce))
            os.close(nonce_write)
            child_environment = continuity._collector_attempt_child_environment(
                launch, lease_fd=child_lease_fd, nonce_fd=nonce_read
            )
            for name, value in child_environment.items():
                monkeypatch.setenv(name, value)
            if mutation == "wrong-command":
                argv = [*spec.command[3:-1], "2099-01-06"]
            elif mutation == "reordered":
                argv = [
                    "forward-context-capture",
                    "--date",
                    spec.session,
                    "--database",
                    spec.database_path,
                ]
            else:
                argv = list(prepared["schedule"][1].command[3:])
            with pytest.raises(CollectorContinuityError, match="command hash is invalid"):
                cli.main(argv)
        assert _fd_set() == before_fds

    assert dispatch_calls == []
    assert all(name not in os.environ for name in _CHILD_ENVIRONMENT)
    continuity._clear_nonce(launch._nonce_buffer)
    launch.nonce = b""


def test_parent_popen_uses_only_frozen_command_lease_and_nonce_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        stdout = BytesIO(b"")
        stderr = BytesIO(b"")
        returncode = 0

        def wait(self) -> int:
            return self.returncode

    prepared = _prepared(tmp_path, monkeypatch)
    launches = _capture_launches(monkeypatch)
    popen_calls: list[tuple[tuple[object, ...], dict[str, object], bytes]] = []
    _clear_child_environment(monkeypatch)
    before_fds = _fd_set()

    def popen(*args: object, **kwargs: object) -> Process:
        environment = kwargs["env"]
        nonce_fd = int(environment["STOCKDATA_COLLECTOR_PIPE_FD"])
        nonce = os.read(nonce_fd, 32)
        assert os.read(nonce_fd, 1) == b""
        popen_calls.append((args, kwargs, nonce))
        return Process()

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        spec = prepared["schedule"][0]
        outcome = _api("_execute_collector_step_attempt")(
            lease, spec, popen_factory=popen
        )

    assert _fd_set() == before_fds
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "child_no_commit"
    assert len(launches) == 1
    _assert_launch_nonce_cleared(launches[0])
    assert len(popen_calls) == 1
    args, kwargs, raw_nonce = popen_calls[0]
    environment = kwargs["env"]
    assert args == (list(spec.command),)
    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (
        int(environment["STOCKDATA_COLLECTOR_LEASE_FD"]),
        int(environment["STOCKDATA_COLLECTOR_PIPE_FD"]),
    )
    assert set(environment) == set(_CHILD_ENVIRONMENT)
    assert len(raw_nonce) == 32
    nonce_sha256 = hashlib.sha256(raw_nonce).hexdigest()
    visible = "\n".join(
        [*map(str, spec.command), *environment.keys(), *environment.values(), "", ""]
    )
    assert raw_nonce.hex() not in visible
    assert nonce_sha256 not in visible
    assert all(name not in os.environ for name in _CHILD_ENVIRONMENT)


def test_child_handoff_proves_lock_ledger_tail_argv_nonce_before_first_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    begin = _api("_begin_collector_step_attempt")
    open_writer = _api("open_collector_child_writer_authority")
    open_database = _api("open_collector_writer_database")
    close_writer = _api("close_collector_writer_authority")
    events: list[str] = []
    original_lock = continuity.verify_locked_collector_lease
    original_ledger = continuity._parse_retained_bound_collector_ledger
    original_argv = continuity._child_environment_matches_active_attempt
    original_nonce = continuity._read_exact_child_nonce
    original_connect = continuity.sqlite3.connect

    def record_lock(*args: object, **kwargs: object):
        events.append("lock")
        return original_lock(*args, **kwargs)

    def record_ledger(*args: object, **kwargs: object):
        events.append("ledger_tail")
        return original_ledger(*args, **kwargs)

    def record_argv(*args: object, **kwargs: object):
        events.append("argv")
        return original_argv(*args, **kwargs)

    def record_nonce(*args: object, **kwargs: object):
        events.append("nonce")
        return original_nonce(*args, **kwargs)

    def record_connect(*args: object, **kwargs: object):
        events.append("sqlite")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(continuity, "verify_locked_collector_lease", record_lock)
    monkeypatch.setattr(
        continuity, "_parse_retained_bound_collector_ledger", record_ledger
    )
    monkeypatch.setattr(
        continuity, "_child_environment_matches_active_attempt", record_argv
    )
    monkeypatch.setattr(continuity, "_read_exact_child_nonce", record_nonce)
    monkeypatch.setattr(continuity.sqlite3, "connect", record_connect)

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        spec = prepared["schedule"][0]
        launch = begin(lease, spec)
        events.clear()
        with lease.child_handoff() as handoff:
            child_lease_fd = continuity.os.dup(handoff.fd)
            read_fd, write_fd = continuity.os.pipe()
            continuity.os.write(write_fd, bytes(launch.nonce))
            continuity.os.close(write_fd)
            token = open_writer(
                argv=list(spec.command),
                environ=continuity._collector_attempt_child_environment(
                    launch, lease_fd=child_lease_fd, nonce_fd=read_fd
                ),
            )
        try:
            cache = open_database(
                database_path=prepared["database"], writer_token=token
            )
            cache.close()
        finally:
            close_writer(token)

    first_sqlite = events.index("sqlite")
    assert events[:first_sqlite].count("lock") >= 1, events
    assert events[:first_sqlite].count("ledger_tail") >= 1, events
    assert events[:first_sqlite].count("argv") >= 1, events
    assert events[:first_sqlite].count("nonce") >= 1, events
    assert max(events.index(name) for name in ("lock", "ledger_tail", "argv", "nonce")) < first_sqlite, events
