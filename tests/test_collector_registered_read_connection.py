from __future__ import annotations

import builtins
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import CollectorContinuityError
from test_collector_step_state import (
    _bound_registration,
    _prepare_collector,
    _schedule,
)


def _api(name: str):
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing controlled read API: {name}")
    return value


@contextmanager
def _read_token(spec: object):
    with _api("open_registered_collector_read_connection")(spec) as token:
        assert not isinstance(token, sqlite3.Connection)
        yield token


def _prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    registration = _bound_registration(database)
    return {
        "database": database,
        "ledger": ledger,
        "registration": registration,
        "schedule": _schedule(database),
    }


def _seed_and_close(prepared: dict[str, object]) -> None:
    with continuity.open_exact_collector_sqlite(
        database_path=prepared["database"],
        ledger_path=prepared["ledger"],
    ) as (connection, _):
        connection.commit()


def _fd_set() -> set[int]:
    result: set[int] = set()
    for name in os.listdir("/dev/fd"):
        try:
            result.add(int(name))
        except ValueError:
            continue
    return result


def _assert_rejected(error: BaseException | None) -> None:
    assert error is not None
    assert isinstance(error, (CollectorContinuityError, sqlite3.ProgrammingError, sqlite3.OperationalError))


def test_read_api_has_no_private_sqlite_abi_dependency() -> None:
    source = Path(continuity.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import ctypes",
        "ctypes.",
        "import _sqlite3",
        "_sqlite3",
        "FILE_POINTER",
        "sqlite3_file",
        "sqlite3_vfs",
        "from_address",
        "PyObject",
        "ob_type",
    )
    assert not [token for token in forbidden if token in source]


def test_ordinary_sqlite_connection_fails_closed_for_all_step_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    ordinary = sqlite3.connect(str(prepared["database"]))
    try:
        with pytest.raises(CollectorContinuityError):
            continuity.require_bound_collector_read_connection(ordinary, spec)
        with pytest.raises(CollectorContinuityError):
            continuity.snapshot_collector_step_state(ordinary, spec)
        with pytest.raises(CollectorContinuityError):
            continuity.capture_collector_step_baseline(ordinary, spec)
        with pytest.raises(CollectorContinuityError):
            continuity.verify_collector_raw_postcondition(
                ordinary,
                spec,
                None,
                attempt_started_at="2099-01-05T08:40:00+08:00",
                attempt_finished_at="2099-01-05T09:20:00+08:00",
            )
    finally:
        ordinary.close()


def test_bound_read_token_exposes_only_the_opaque_read_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    _seed_and_close(prepared)
    spec = prepared["schedule"][3]
    with _read_token(spec) as token:
        assert continuity.require_bound_collector_read_connection(token, spec) is None
        for name in (
            "execute",
            "cursor",
            "set_authorizer",
            "control_fd",
            "fd_locator",
            "fileno",
            "connection",
            "_connection",
        ):
            assert not hasattr(token, name)
        with pytest.raises(TypeError):
            sqlite3.Connection.execute(token, "SELECT 1")
        with pytest.raises(TypeError):
            sqlite3.Connection.cursor(token)
        with pytest.raises(TypeError):
            sqlite3.Connection.set_authorizer(token, None)
        state = continuity.snapshot_collector_step_state(token, spec)
        baseline = continuity.capture_collector_step_baseline(token, spec)
        result = continuity.verify_collector_raw_postcondition(
            token,
            spec,
            baseline,
            attempt_started_at="2099-01-05T15:10:00+08:00",
            attempt_finished_at="2099-01-05T16:20:00+08:00",
        )
        assert state["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA
        assert result is not None


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE main.read_connection_write_probe(value INTEGER)",
        "INSERT INTO main.daily(code,date,is_final) VALUES ('000001.SZ','2099-01-05',1)",
        "ATTACH ':memory:' AS extra",
        "PRAGMA main.user_version=17",
        "PRAGMA query_only=0",
        "CREATE TEMP TABLE temp_read_connection_probe(value INTEGER)",
        "INSERT INTO temp_read_connection_probe(value) VALUES (1)",
    ),
)
def test_bound_read_token_rejects_all_sqlite_write_surfaces_at_type_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        with pytest.raises(TypeError):
            sqlite3.Connection.execute(token, statement)


def test_readonly_legacy_open_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    with pytest.raises(CollectorContinuityError):
        continuity.open_collector_connection(prepared["database"], readonly=True)


def test_preopen_wrong_inode_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    database = Path(prepared["database"])
    foreign = tmp_path / "foreign.sqlite"
    shutil.copyfile(database, foreign)
    registered = tmp_path / "registered.sqlite"
    os.replace(database, registered)
    os.replace(foreign, database)
    try:
        with pytest.raises(CollectorContinuityError):
            with _read_token(prepared["schedule"][0]):
                pass
    finally:
        database.unlink()
        os.replace(registered, database)


@pytest.mark.parametrize("replacement", ("copy", "symlink"))
def test_control_fd_remains_bound_while_canonical_path_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    database = Path(prepared["database"])
    foreign = tmp_path / "foreign.sqlite"
    shutil.copyfile(database, foreign)
    registered = tmp_path / "registered.sqlite"
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA
        os.replace(database, registered)
        if replacement == "copy":
            shutil.copyfile(foreign, database)
        else:
            os.symlink(registered, database)
        try:
            with pytest.raises(CollectorContinuityError):
                continuity.require_bound_collector_read_connection(token, spec)
            with pytest.raises(CollectorContinuityError):
                continuity.snapshot_collector_step_state(token, spec)
        finally:
            database.unlink()
            os.replace(registered, database)
        with pytest.raises(CollectorContinuityError):
            continuity.require_bound_collector_read_connection(token, spec)


def test_failed_read_connect_does_not_leak_file_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    bad = replace(prepared["schedule"][0], database_path=str(tmp_path / "missing.sqlite"))
    before = _fd_set()
    for _ in range(5):
        with pytest.raises(CollectorContinuityError):
            with _read_token(bad):
                pass
    assert _fd_set() == before


def test_opaque_read_token_cannot_be_reused_after_context_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    before = _fd_set()
    with _read_token(spec) as token:
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA
    assert _fd_set() == before
    with pytest.raises(CollectorContinuityError):
        continuity.require_bound_collector_read_connection(token, spec)


def _assert_fd_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("drift_kind", ["canonical", "authority"])
def test_poisoned_binding_context_exit_closes_all_resources_and_forgets_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift_kind: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    database = Path(prepared["database"])
    registration = Path(prepared["registration"])

    for churn in range(3):
        before = _fd_set()
        displaced = database.with_name(f"collector-displaced-{drift_kind}-{churn}.sqlite")
        registration_bytes = registration.read_bytes()
        binding = None
        control_fd = guard_fd = -1
        try:
            with _read_token(spec) as token:
                binding = continuity._REGISTERED_COLLECTOR_READ_BINDINGS[token]
                control_fd, guard_fd = binding.control_fd, binding.guard_fd
                if drift_kind == "canonical":
                    os.replace(database, displaced)
                else:
                    registration.write_bytes(registration_bytes + b"\n")
                try:
                    continuity.require_bound_collector_read_connection(token, spec)
                except CollectorContinuityError:
                    pass
                else:
                    pytest.fail("authority drift must poison the binding")
                assert binding.state == "POISONED"
        finally:
            if drift_kind == "canonical" and displaced.exists():
                os.replace(displaced, database)
            elif drift_kind == "authority":
                registration.write_bytes(registration_bytes)

        assert binding is not None
        assert token not in continuity._REGISTERED_COLLECTOR_READ_BINDINGS
        assert binding.connection is None
        _assert_fd_closed(control_fd)
        _assert_fd_closed(guard_fd)
        assert _fd_set() == before


def test_probe_close_error_after_actual_close_never_retries_reused_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    real_open = continuity.os.open
    real_close = continuity.os.close
    probe_fds: set[int] = set()
    foreign_fd: int | None = None
    armed = {"value": True}

    def record_probe(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith("/dev/fd/"):
            probe_fds.add(fd)
        return fd

    def close_after_reuse(fd: int) -> None:
        nonlocal foreign_fd
        if armed["value"] and fd in probe_fds:
            armed["value"] = False
            real_close(fd)
            foreign_fd = real_open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            assert foreign_fd == fd
            raise OSError("descriptor was closed before close reported failure")
        real_close(fd)

    with _read_token(spec) as token:
        binding = continuity._REGISTERED_COLLECTOR_READ_BINDINGS[token]
        monkeypatch.setattr(continuity.os, "open", record_probe)
        monkeypatch.setattr(continuity.os, "close", close_after_reuse)
        with pytest.raises(CollectorContinuityError):
            continuity.require_bound_collector_read_connection(token, spec)
        assert binding.state == "POISONED"
        assert foreign_fd is not None
        os.fstat(foreign_fd)
    try:
        assert token not in continuity._REGISTERED_COLLECTOR_READ_BINDINGS
        assert binding.connection is None
        os.fstat(foreign_fd)
    finally:
        if foreign_fd is not None:
            try:
                real_close(foreign_fd)
            except OSError:
                pass


def test_ownership_challenge_failure_isolated_process_fails_closed_without_closing_unproven_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    child_script = r'''
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[2])
import stockdata.collector_continuity as continuity
from test_collector_step_state import _schedule

database = Path(sys.argv[1])
spec = _schedule(database)[0]
real_open = continuity.os.open
real_close = continuity.os.close
foreign_fd = None
manager = continuity.open_registered_collector_read_connection(spec)
token = manager.__enter__()
try:
    binding = continuity._REGISTERED_COLLECTOR_READ_BINDINGS[token]
    guard_fd = binding.guard_fd
    real_close(binding.control_fd)
    foreign_fd = real_open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    assert foreign_fd == binding.control_fd
    try:
        continuity.require_bound_collector_read_connection(token, spec)
        require_rejected = False
    except BaseException:
        require_rejected = True
    try:
        continuity.snapshot_collector_step_state(token, spec)
        snapshot_rejected = False
    except BaseException:
        snapshot_rejected = True
    try:
        manager.__exit__(None, None, None)
        cleanup_rejected = False
    except BaseException:
        cleanup_rejected = True
    after_registry_removed = token not in continuity._REGISTERED_COLLECTOR_READ_BINDINGS
    connection_cleared = binding.connection is None
    stale_token_rejected = False
    try:
        continuity.require_bound_collector_read_connection(token, spec)
    except BaseException:
        stale_token_rejected = True
    fatal_stage = getattr(continuity, "_COLLECTOR_CONTINUITY_FATAL_STAGE", None)
    fatal = isinstance(fatal_stage, str) and fatal_stage.startswith(
        "registered-read-retirement-"
    )
    try:
        continuity.require_collector_continuity_health()
        health_rejected = False
    except BaseException:
        health_rejected = True
    try:
        os.fstat(guard_fd)
        guard_open = True
    except OSError:
        guard_open = False
    try:
        os.fstat(foreign_fd)
        foreign_open = True
    except OSError:
        foreign_open = False
    print("%d:%d:%d:%d:%d:%d:%d:%d:%d:%d" % (
        require_rejected,
        snapshot_rejected,
        cleanup_rejected,
        after_registry_removed,
        stale_token_rejected,
        connection_cleared,
        fatal,
        health_rejected,
        guard_open,
        foreign_open,
    ))
finally:
    if token in continuity._REGISTERED_COLLECTOR_READ_BINDINGS:
        continuity._REGISTERED_COLLECTOR_READ_BINDINGS.pop(token, None)
try:
    real_close(foreign_fd)
except OSError:
    pass
'''
    with _read_token(spec) as parent_token:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child_script,
                str(prepared["database"]),
                str(Path(__file__).parent),
            ],
            cwd=str(Path(__file__).parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "1:1:1:1:1:1:1:1:1:1"
        continuity.require_bound_collector_read_connection(parent_token, spec)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_close_failure_is_quarantined_and_parent_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    parent_pid = os.getpid()
    real_close = continuity.os.close
    inherited_fds: set[int] = set()

    with _read_token(spec) as token:
        binding = continuity._REGISTERED_COLLECTOR_READ_BINDINGS[token]
        inherited_fds.update((binding.control_fd, binding.guard_fd))

        def close_child_failure(fd: int) -> None:
            if os.getpid() != parent_pid and fd in inherited_fds:
                raise OSError("deterministic child close failure")
            real_close(fd)

        monkeypatch.setattr(continuity.os, "close", close_child_failure)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            real_close(read_fd)
            try:
                continuity.require_bound_collector_read_connection(token, spec)
                require_rejected = False
            except BaseException:
                require_rejected = True
            try:
                continuity.snapshot_collector_step_state(token, spec)
                snapshot_rejected = False
            except BaseException:
                snapshot_rejected = True
            quarantined = bool(
                getattr(continuity, "_REGISTERED_COLLECTOR_READ_FORK_QUARANTINED", False)
            )
            payload = f"{int(require_rejected)}:{int(snapshot_rejected)}:{int(quarantined)}".encode()
            os.write(write_fd, payload)
            real_close(write_fd)
            os._exit(0)
        real_close(write_fd)
        payload = os.read(read_fd, 64)
        real_close(read_fd)
        _, wait_status = os.waitpid(pid, 0)
        assert os.WIFEXITED(wait_status)
        assert payload == b"1:1:1"
        continuity.require_bound_collector_read_connection(token, spec)
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA


def test_stale_token_cannot_close_a_later_reused_process_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        active_fds = _fd_set()
    released = sorted(active_fds - _fd_set())
    assert released, "the controlled read must own a process fd while active"
    held: list[int] = []
    reused: int | None = None
    try:
        for _ in range(256):
            fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            held.append(fd)
            if fd in released:
                reused = fd
                break
        assert reused is not None, "the released fd was not deterministically reused"
        with pytest.raises(CollectorContinuityError):
            continuity.require_bound_collector_read_connection(token, spec)
        os.fstat(reused)
    finally:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass


def test_non_owner_thread_cannot_use_or_close_bound_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        outcomes: queue.Queue[BaseException | None] = queue.Queue()

        def query() -> None:
            try:
                continuity.require_bound_collector_read_connection(token, spec)
            except BaseException as error:
                outcomes.put(error)
            else:
                outcomes.put(None)

        thread = threading.Thread(target=query)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        _assert_rejected(outcomes.get_nowait())
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA

        close_outcome: queue.Queue[BaseException | None] = queue.Queue()

        def close() -> None:
            try:
                continuity.require_bound_collector_read_connection(token, spec)
            except BaseException as error:
                close_outcome.put(error)
            else:
                close_outcome.put(None)

        thread = threading.Thread(target=close)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        _assert_rejected(close_outcome.get_nowait())
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_cannot_obtain_or_use_inherited_read_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                continuity.require_bound_collector_read_connection(token, spec)
            except BaseException:
                os.write(write_fd, b"rejected")
            else:
                os.write(write_fd, b"accepted")
            try:
                sqlite3.Connection.execute(token, "SELECT 1")
            except BaseException:
                pass
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        status = os.read(read_fd, 8)
        os.close(read_fd)
        _, wait_status = os.waitpid(pid, 0)
        assert os.WIFEXITED(wait_status)
        assert status == b"rejected"
        assert continuity.snapshot_collector_step_state(token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_clean_fork_child_can_establish_independent_lease_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            continuity.require_collector_continuity_health()
            with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
                lease.verify()
            payload = b"healthy:lease"
        except BaseException as error:
            payload = f"error:{type(error).__name__}:{error}".encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        payload = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
    _, wait_status = os.waitpid(pid, 0)
    assert os.WIFEXITED(wait_status)
    assert payload == b"healthy:lease"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_inherited_read_child_quarantines_old_token_but_can_open_new_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as parent_token:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                try:
                    continuity.require_bound_collector_read_connection(parent_token, spec)
                except BaseException:
                    old_token_rejected = True
                else:
                    old_token_rejected = False
                continuity.require_collector_continuity_health()
                with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
                    lease.verify()
                payload = b"rejected:healthy:lease" if old_token_rejected else b"accepted"
            except BaseException as error:
                payload = f"error:{type(error).__name__}:{error}".encode()
            os.write(write_fd, payload)
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        try:
            payload = os.read(read_fd, 4096)
        finally:
            os.close(read_fd)
        _, wait_status = os.waitpid(pid, 0)
        assert os.WIFEXITED(wait_status)
        assert payload == b"rejected:healthy:lease"
        assert continuity.snapshot_collector_step_state(parent_token, spec)["schema_version"] == continuity.COLLECTOR_STEP_STATE_SCHEMA


def test_opaque_token_has_no_transferable_subprocess_fd_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        assert continuity.require_bound_collector_read_connection(token, spec) is None
        assert not any(name in dir(token) for name in ("control_fd", "fd_locator", "fileno"))


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_sidecars_are_rejected_at_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    sidecar = Path(f"{prepared['database']}{suffix}")
    sidecar.write_bytes(b"sidecar")
    try:
        with pytest.raises(CollectorContinuityError):
            with _read_token(prepared["schedule"][0]):
                pass
    finally:
        sidecar.unlink()


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_sidecars_are_rejected_at_transaction_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    database = Path(prepared["database"])
    spec = prepared["schedule"][0]
    with _read_token(spec) as token:
        sidecar = Path(f"{database}{suffix}")
        sidecar.write_bytes(b"appeared")
        try:
            with pytest.raises(CollectorContinuityError):
                continuity.require_bound_collector_read_connection(token, spec)
        finally:
            sidecar.unlink()


@contextmanager
def _swap_ledger_before_parse(
    monkeypatch: pytest.MonkeyPatch, ledger: Path
):
    displaced = ledger.with_name("ledger-original.jsonl")
    replacement = ledger.with_name("ledger-replacement.jsonl")
    shutil.copyfile(ledger, replacement)
    state = {"swapped": False}

    def swap() -> None:
        if not state["swapped"]:
            state["swapped"] = True
            os.replace(ledger, displaced)
            os.replace(replacement, ledger)

    original_parse = continuity.parse_collector_ledger

    def parse(source: object) -> object:
        source_path = source if isinstance(source, (str, os.PathLike)) else None
        if source_path is None:
            identity = getattr(source, "identity", None)
            source_path = getattr(identity, "canonical_path", None)
        if source_path is not None and Path(source_path) == ledger:
            swap()
        return original_parse(source)

    monkeypatch.setattr(continuity, "parse_collector_ledger", parse)
    try:
        yield state
    finally:
        if displaced.exists():
            os.replace(ledger, replacement)
            os.replace(displaced, ledger)
        if replacement.exists():
            replacement.unlink()


@pytest.mark.parametrize("entry", ("registration", "snapshot", "baseline", "raw"))
def test_ledger_replacement_between_identity_check_and_parse_is_bound_or_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    baseline = None
    if entry == "raw":
        with _read_token(spec) as token:
            baseline = continuity.capture_collector_step_baseline(token, spec)
    parsed_sources: list[object] = []
    original_parse = continuity.parse_collector_ledger

    def record_parse(source: object) -> object:
        parsed_sources.append(source)
        return original_parse(source)

    monkeypatch.setattr(continuity, "parse_collector_ledger", record_parse)
    with _swap_ledger_before_parse(monkeypatch, Path(prepared["ledger"])) as state:
        try:
            if entry == "registration":
                continuity._read_bound_registration(prepared["registration"])
            else:
                with _read_token(spec) as token:
                    if entry == "snapshot":
                        continuity.snapshot_collector_step_state(token, spec)
                    elif entry == "baseline":
                        continuity.capture_collector_step_baseline(token, spec)
                    else:
                        continuity.verify_collector_raw_postcondition(
                            token,
                            spec,
                            baseline,
                            attempt_started_at="2099-01-05T08:40:00+08:00",
                            attempt_finished_at="2099-01-05T09:20:00+08:00",
                        )
        except CollectorContinuityError:
            pass
        assert state["swapped"] is True
    ledger_sources = [
        source
        for source in parsed_sources
        if isinstance(source, (str, os.PathLike)) and Path(source) == Path(prepared["ledger"])
    ]
    assert not ledger_sources, "ledger parsing must use the retained no-follow file or reject before parsing"


def test_locator_close_error_is_single_attempt_and_does_not_close_unrelated_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    child_script = r'''
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[2])
import stockdata.collector_continuity as continuity
from test_collector_step_state import _schedule

database = Path(sys.argv[1])
spec = _schedule(database)[0]
real_open = continuity.os.open
real_close = continuity.os.close
held_fd = real_open(database, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
alias_fds = set()
close_attempts = []
foreign_fd = None

def open_alias(path, flags, *args, **kwargs):
    value = real_open(path, flags, *args, **kwargs)
    if isinstance(path, str) and path.startswith("/dev/fd/"):
        alias_fds.add(value)
    return value

def close_alias(fd):
    if fd in alias_fds:
        close_attempts.append(fd)
        real_close(fd)
        global foreign_fd
        foreign_fd = real_open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        assert foreign_fd == fd
        raise OSError("descriptor was closed before close reported failure")
    real_close(fd)

continuity.os.open = open_alias
continuity.os.close = close_alias
try:
    try:
        with continuity.open_registered_collector_read_connection(spec):
            pass
        open_rejected = False
        error_stage = ""
    except BaseException as exc:
        open_rejected = True
        error_stage = str(exc)
    try:
        os.fstat(held_fd)
        held_open = True
    except OSError:
        held_open = False
    try:
        os.fstat(foreign_fd)
        foreign_open = True
    except OSError:
        foreign_open = False
    fatal_stage = getattr(continuity, "_COLLECTOR_CONTINUITY_FATAL_STAGE", None)
    print("%d:%d:%d:%d:%s" % (
        open_rejected,
        len(close_attempts),
        held_open,
        foreign_open,
        error_stage,
    ))
finally:
    real_close(held_fd)
    if foreign_fd is not None:
        try:
            real_close(foreign_fd)
        except OSError:
            pass
    for fd in tuple(alias_fds):
        try:
            real_close(fd)
        except OSError:
            pass
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_script,
            str(prepared["database"]),
            str(Path(__file__).parent),
        ],
        cwd=str(Path(__file__).parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    fields = completed.stdout.strip().split(":", 4)
    assert fields[:4] == ["1", "1", "1", "1"]
    assert "locator_close" in fields[4]


def test_context_cleanup_errors_keep_body_then_two_cleanup_failures() -> None:
    body_error = RuntimeError("body")
    first_cleanup = OSError("first close")
    second_cleanup = ValueError("second close")
    cleanup = builtins.ExceptionGroup("cleanup", [first_cleanup, second_cleanup])

    combined = continuity._combine_collector_context_errors(body_error, cleanup)

    assert type(combined) is builtins.ExceptionGroup
    assert combined.exceptions == (body_error, cleanup)
    assert cleanup.exceptions == (first_cleanup, second_cleanup)


def test_context_cleanup_with_base_exception_uses_base_exception_group() -> None:
    body_error = RuntimeError("body")
    cleanup_error = KeyboardInterrupt("close")

    combined = continuity._combine_collector_context_errors(body_error, cleanup_error)

    assert type(combined) is builtins.BaseExceptionGroup
    assert combined.exceptions == (body_error, cleanup_error)


def test_cleanup_only_error_preserves_close_order() -> None:
    first_cleanup = OSError("first close")
    second_cleanup = RuntimeError("second close")

    result = continuity._collector_cleanup_error(
        "collector close failed",
        ("connection_close", "control_fd_close"),
        (first_cleanup, second_cleanup),
    )

    assert type(result) is CollectorContinuityError
    assert isinstance(result.__cause__, builtins.ExceptionGroup)
    assert result.__cause__.exceptions == (first_cleanup, second_cleanup)


def test_cleanup_error_without_group_aliases_keeps_primary_and_all_close_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(continuity, "_ExceptionGroup", None, raising=False)
    monkeypatch.setattr(continuity, "_BaseExceptionGroup", None, raising=False)
    primary = OSError("primary close")
    additional = (RuntimeError("second close"), KeyboardInterrupt("third close"))
    stages = ("connection_close", "control_fd_close", "guard_fd_close")

    result = continuity._collector_cleanup_error(
        "collector close failed",
        stages,
        (primary, *additional),
    )

    assert type(result) is CollectorContinuityError
    assert result.__cause__ is primary
    message = str(result)
    assert message.startswith("collector close failed: connection_close, control_fd_close, guard_fd_close")
    assert "additional cleanup failures: 2" in message
    assert "RuntimeError" in message
    assert "KeyboardInterrupt" in message
    assert message.index("RuntimeError") < message.index("KeyboardInterrupt")
