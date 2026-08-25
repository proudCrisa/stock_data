from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import (
    COLLECTOR_EVIDENCE_TRIGGER_SQL,
    COLLECTOR_GENESIS_SCHEMA,
    COLLECTOR_LEDGER_EVENT_SCHEMA,
    COLLECTOR_PREPARATION_SCHEMA,
    COLLECTOR_SQLITE_BUSY_TIMEOUT_MS,
    PHYSICAL_FILE_IDENTITY_SCHEMA,
    CollectorContinuityError,
    PhysicalFileIdentity,
    append_collector_ledger_event,
    canonical_json_bytes,
    canonical_json_sha256,
    create_exclusive_regular_file,
    decode_collector_ledger_event,
    decode_canonical_json_object,
    default_collector_ledger_path,
    load_verified_prepared_collector,
    open_exact_collector_sqlite,
    open_existing_collector_files,
    open_existing_regular_file,
    require_exact_keys,
)
from stockdata.cache import Cache
from stockdata.future_panel_registration import (
    FuturePanelRegistrationError,
    prepare_future_collector_database,
    register_future_panel,
    verify_collector_capability,
)


def _identity_wire(path: str) -> dict[str, object]:
    return {
        "schema_version": PHYSICAL_FILE_IDENTITY_SCHEMA,
        "canonical_path": path,
        "parent_st_dev": 1,
        "parent_st_ino": 2,
        "file_st_dev": 1,
        "file_st_ino": 3,
    }


def _collector_pair(tmp_path: Path, parent_name: str = "collector") -> tuple[Path, Path]:
    parent = tmp_path / parent_name
    parent.mkdir()
    database = parent / "evidence.sqlite"
    ledger = parent / "evidence.sqlite.collector-ledger.jsonl"
    database.write_bytes(b"database")
    ledger.write_bytes(b"ledger")
    return database, ledger


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def _descriptor_path(fd: int) -> str:
    proc_path = f"/proc/self/fd/{fd}"
    try:
        return os.path.realpath(os.readlink(proc_path))
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            raw = fcntl.fcntl(fd, 50, b"\0" * 1024)
        except OSError:
            return ""
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    return ""


def _fd_identity_snapshot() -> dict[int, tuple[int, int, str]]:
    snapshot: dict[int, tuple[int, int, str]] = {}
    for name in os.listdir("/dev/fd"):
        try:
            fd = int(name)
            status = os.fstat(fd)
        except (OSError, ValueError):
            continue
        snapshot[fd] = (status.st_dev, status.st_ino, _descriptor_path(fd))
    return snapshot


def _assert_no_new_collector_fds(
    before: dict[int, tuple[int, int, str]],
    *,
    database: Path,
    ledger: Path,
) -> None:
    expected_paths = {
        os.path.realpath(os.fspath(database.parent)),
        os.path.realpath(os.fspath(database)),
        os.path.realpath(os.fspath(ledger)),
        os.path.realpath(f"{database}-journal"),
        os.path.realpath(f"{database}-shm"),
        os.path.realpath(f"{database}-wal"),
    }
    expected_identities: set[tuple[int, int]] = set()
    for path in expected_paths:
        try:
            status = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        expected_identities.add((status.st_dev, status.st_ino))
    leaked = {
        fd: identity
        for fd, identity in _fd_identity_snapshot().items()
        if before.get(fd) != identity
        and (
            identity[2] in expected_paths
            or (identity[0], identity[1]) in expected_identities
        )
    }
    assert not leaked, (
        "collector call leaked descriptors: "
        + ", ".join(
            f"fd={fd} path={identity[2]!r} dev={identity[0]} inode={identity[1]}"
            for fd, identity in sorted(leaked.items())
        )
    )


_PREPARE_SYMBOLS = (
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
_PREPARE_SESSIONS = ("2099-01-05", "2099-01-06", "2099-01-07")


def _future_panel_file(tmp_path: Path) -> Path:
    panel = sorted(
        f"{symbol}@{session}"
        for symbol in _PREPARE_SYMBOLS
        for session in _PREPARE_SESSIONS
    )
    path = tmp_path / "future-panel.json"
    path.write_bytes(canonical_json_bytes(panel))
    return path


def _prepare_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], Path, Path, Path]:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    panel_file = _future_panel_file(tmp_path)
    database = tmp_path / "future.sqlite"
    prepared = prepare_future_collector_database(
        database_file=database,
        panel_file=panel_file,
    )
    ledger = Path(default_collector_ledger_path(database))
    return prepared, panel_file, database, ledger


def _bound_registration(database: Path) -> Path:
    registration = database.with_name("registration.json")
    ledger = Path(default_collector_ledger_path(database))
    prepared = load_verified_prepared_collector(
        database_path=database,
        ledger_path=ledger,
    )
    panel = list(
        sorted(f"{symbol}@{session}" for symbol in _PREPARE_SYMBOLS for session in _PREPARE_SESSIONS)
    )
    prerequisites = {
        "collector": {
            "schema_version": "stockdata-forward-collector-capability/2",
            "database_path": prepared["database_path"],
            "ledger_path": prepared["ledger_path"],
            "source": "tencent",
            "adjustment_mode": "raw",
            "adjustment_version": "tencent-qt-daily-v1",
            "collector_schema_sha256": prepared["collector_schema_sha256"],
            "database_identity": prepared["database_identity"],
            "ledger_identity": prepared["ledger_identity"],
            "database_uuid": prepared["database_uuid"],
            "cohort_sha256": prepared["cohort_sha256"],
            "genesis_sha256": prepared["genesis_sha256"],
            "ledger_genesis_event_sha256": prepared["ledger_genesis_event_sha256"],
        }
    }
    payload = {
        "schema_version": "rqgm-forward-panel-registration/4",
        "registered_at": "2026-08-01T12:00:00+08:00",
        "as_of": "2026-08-01",
        "symbols": list(_PREPARE_SYMBOLS),
        "sessions": list(_PREPARE_SESSIONS),
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
        "database_path": prepared["database_path"],
        "panel_sha256": canonical_json_sha256(panel),
        "workspace_count": 36,
        "outcome_feedback_used": False,
        "status": "AWAITING_FULL_SNAPSHOT_READINESS",
        "prerequisite_files": {},
        "prerequisites": prerequisites,
        "prerequisites_sha256": canonical_json_sha256(prerequisites),
    }
    registration.write_bytes(canonical_json_bytes(payload))
    if len(continuity.parse_collector_ledger(ledger)) == 1:
        with open_existing_regular_file(ledger) as opened:
            append_collector_ledger_event(
                opened,
                event_type="REGISTRATION_BOUND",
                event={
                    "registration_sha256": hashlib.sha256(registration.read_bytes()).hexdigest(),
                    "panel_sha256": payload["panel_sha256"],
                    "sessions": list(_PREPARE_SESSIONS),
                    "sessions_sha256": canonical_json_sha256(list(_PREPARE_SESSIONS)),
                    "prerequisites_sha256": payload["prerequisites_sha256"],
                    "bound_at": "2026-08-01T12:00:01+08:00",
                },
            )
    return registration


def _task24_schedule(database: Path) -> tuple[object, ...]:
    return continuity.freeze_collector_step_schedule(
        registration_file=_bound_registration(database)
    )


def _task24_snapshot(spec: object) -> dict[str, object]:
    with continuity.open_registered_collector_read_connection(spec) as token:
        assert not isinstance(token, sqlite3.Connection)
        return continuity.snapshot_collector_step_state(token, spec)


def _ledger_event(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    return json.loads(raw[:-1].decode("ascii"))


def _write_ledger_event(path: Path, event: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(event) + b"\n")


def _empty_sqlite_pair(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "empty.sqlite"
    ledger = Path(default_collector_ledger_path(database))
    database.touch(mode=0o600)
    ledger.touch(mode=0o600)
    return database, ledger


@contextmanager
def _fifo_open_deadline(seconds: float) -> Iterator[None]:
    class FifoOpenTimeout(AssertionError):
        pass

    def alarm_handler(signum: int, frame: object) -> None:
        del signum, frame
        raise FifoOpenTimeout("opening a FIFO blocked")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def test_canonical_json_is_ascii_sorted_and_compact() -> None:
    raw = canonical_json_bytes({"z": "中", "a": {"d": 2, "c": 1}})

    assert raw == b'{"a":{"c":1,"d":2},"z":"\\u4e2d"}'
    raw.decode("ascii")
    assert decode_canonical_json_object(raw) == {
        "a": {"c": 1, "d": 2},
        "z": "中",
    }


@pytest.mark.parametrize(
    "value",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
    ],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_canonical_json_rejects_non_finite_numbers(value: object) -> None:
    with pytest.raises(CollectorContinuityError, match="non-finite"):
        canonical_json_bytes(value)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"b":2,"a":1}',
        b'{"a": 1}',
        b'{"a":NaN}',
    ],
    ids=["duplicate-keys", "unsorted", "whitespace", "nan"],
)
def test_decoder_rejects_duplicate_or_noncanonical_json(raw: bytes) -> None:
    with pytest.raises(CollectorContinuityError):
        decode_canonical_json_object(raw)


def test_exact_key_validation_rejects_unknown_and_missing_keys() -> None:
    with pytest.raises(CollectorContinuityError, match="unknown=extra"):
        require_exact_keys({"required": 1, "extra": 2}, {"required"}, "object")
    with pytest.raises(CollectorContinuityError, match="missing=required"):
        require_exact_keys({}, {"required"}, "object")


def test_physical_identity_round_trip_and_exact_keys(tmp_path: Path) -> None:
    path = str(tmp_path / "evidence.sqlite")
    identity = PhysicalFileIdentity(path, 11, 12, 13, 14)

    assert PhysicalFileIdentity.from_dict(identity.to_dict()) == identity
    assert identity.to_dict()["canonical_path"] == path

    missing = identity.to_dict()
    del missing["file_st_ino"]
    with pytest.raises(CollectorContinuityError, match="missing=file_st_ino"):
        PhysicalFileIdentity.from_dict(missing)

    unknown = identity.to_dict()
    unknown["unexpected"] = 1
    with pytest.raises(CollectorContinuityError, match="unknown=unexpected"):
        PhysicalFileIdentity.from_dict(unknown)


@pytest.mark.parametrize("field", ["parent_st_dev", "parent_st_ino", "file_st_dev", "file_st_ino"])
@pytest.mark.parametrize("invalid", [True, -1], ids=["bool", "negative"])
def test_physical_identity_rejects_bool_and_negative_integers(
    tmp_path: Path, field: str, invalid: object
) -> None:
    value = _identity_wire(str(tmp_path / "evidence.sqlite"))
    value[field] = invalid

    with pytest.raises(CollectorContinuityError):
        PhysicalFileIdentity.from_dict(value)


@pytest.mark.parametrize(
    "path",
    ["relative/evidence.sqlite", "/tmp/./evidence.sqlite", "/tmp//evidence.sqlite", "/"],
    ids=["relative", "dot-component", "double-slash", "root"],
)
def test_physical_identity_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(CollectorContinuityError):
        PhysicalFileIdentity(path, 1, 2, 1, 3)


def test_root_to_leaf_parent_parser_handles_file_directly_under_root() -> None:
    assert continuity._split_parent_and_leaf("/evidence.sqlite") == ("/", "evidence.sqlite")
    with pytest.raises(CollectorContinuityError):
        continuity._split_parent_and_leaf("/")


def test_open_existing_regular_file_accepts_regular_file_and_in_place_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "evidence.sqlite"
    path.parent.mkdir()
    path.write_bytes(b"before")

    with open_existing_regular_file(path) as opened:
        original = opened.identity
        original_stat = os.fstat(opened.file_fd)
        path.write_bytes(b"after")

        assert opened.verify_identity() == original
        current_stat = os.stat(path)
        assert (current_stat.st_dev, current_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        )


def test_open_existing_regular_file_rejects_parent_directory_symlink(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    (real_parent / "evidence.sqlite").write_bytes(b"data")
    symlink_parent = tmp_path / "alias"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CollectorContinuityError):
        open_existing_regular_file(symlink_parent / "evidence.sqlite")


def test_open_existing_regular_file_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"data")
    link = tmp_path / "evidence.sqlite"
    link.symlink_to(target)

    with pytest.raises(CollectorContinuityError):
        open_existing_regular_file(link)


def test_open_existing_regular_file_rejects_symlink_replacement_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite"
    path.write_bytes(b"data")
    moved = tmp_path / "evidence.sqlite.original"
    original_open = continuity.os.open
    armed = True

    def replace_before_leaf_open(
        candidate: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal armed
        if armed and dir_fd is not None and os.fspath(candidate) == path.name:
            armed = False
            path.rename(moved)
            path.symlink_to(moved.name)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(continuity.os, "open", replace_before_leaf_open)
    try:
        with pytest.raises(CollectorContinuityError):
            open_existing_regular_file(path)
    finally:
        if path.is_symlink():
            path.unlink()
        if moved.exists():
            moved.rename(path)
    assert not armed


def test_create_exclusive_regular_file_rejects_symlink_replacement_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_directory(tmp_path)
    leaf = "new-ledger.jsonl"
    path = tmp_path / leaf
    target = tmp_path / "replacement-target"
    target.write_bytes(b"replacement")
    original_open = continuity.os.open
    armed = True

    def replace_before_create(
        candidate: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal armed
        if armed and dir_fd == parent_fd and os.fspath(candidate) == leaf:
            armed = False
            path.symlink_to(target.name)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(continuity.os, "open", replace_before_create)
    try:
        with pytest.raises(CollectorContinuityError):
            create_exclusive_regular_file(parent_fd, leaf)
    finally:
        os.close(parent_fd)
        if path.is_symlink():
            path.unlink()
    assert not armed


def test_open_existing_regular_file_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "evidence.fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    with _fifo_open_deadline(1.0):
        with pytest.raises(CollectorContinuityError):
            open_existing_regular_file(fifo)
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("kind", ["directory", "missing"], ids=["directory", "missing"])
def test_open_existing_regular_file_rejects_non_regular_or_missing_path(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "entry"
    if kind == "directory":
        path.mkdir()

    with pytest.raises(CollectorContinuityError):
        open_existing_regular_file(path)


def test_open_existing_regular_file_rejects_noncanonical_paths(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    path.write_bytes(b"data")

    for invalid in (
        str(path.parent) + "/./evidence.sqlite",
        str(path.parent) + "//evidence.sqlite",
        "relative/evidence.sqlite",
    ):
        with pytest.raises(CollectorContinuityError):
            open_existing_regular_file(invalid)


def test_open_existing_collector_files_rejects_different_parents(tmp_path: Path) -> None:
    database, _ = _collector_pair(tmp_path, "database-parent")
    ledger_parent = tmp_path / "ledger-parent"
    ledger_parent.mkdir()
    ledger = ledger_parent / "ledger.jsonl"
    ledger.write_bytes(b"ledger")

    with pytest.raises(CollectorContinuityError, match="share a canonical parent"):
        open_existing_collector_files(database_path=database, ledger_path=ledger)


def test_opened_regular_file_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    path.write_bytes(b"data")
    opened = open_existing_regular_file(path)

    opened.close()
    assert opened.file_fd == -1
    assert opened.parent_fd == -1
    opened.close()
    with pytest.raises(CollectorContinuityError, match="closed"):
        opened.verify_identity()


def test_opened_collector_files_close_is_idempotent(tmp_path: Path) -> None:
    database, ledger = _collector_pair(tmp_path)
    opened = open_existing_collector_files(database_path=database, ledger_path=ledger)

    opened.close()
    opened.close()
    with pytest.raises(CollectorContinuityError, match="closed"):
        opened.verify_identities()


@pytest.mark.parametrize("replaced", ["database", "ledger"])
def test_verify_identities_detects_atomic_same_path_replacement(
    tmp_path: Path, replaced: str
) -> None:
    database, ledger = _collector_pair(tmp_path)
    opened = open_existing_collector_files(database_path=database, ledger_path=ledger)
    try:
        bound = opened.database if replaced == "database" else opened.ledger
        path = database if replaced == "database" else ledger
        original_stat = os.fstat(bound.file_fd)
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(b"replacement")
        os.replace(replacement, path)

        current_stat = os.stat(path)
        assert (current_stat.st_dev, current_stat.st_ino) != (
            original_stat.st_dev,
            original_stat.st_ino,
        )
        with pytest.raises(CollectorContinuityError, match="physical identity"):
            opened.verify_identities()
    finally:
        opened.close()


def test_verify_identities_detects_parent_directory_rename_and_replacement(
    tmp_path: Path,
) -> None:
    database, ledger = _collector_pair(tmp_path)
    opened = open_existing_collector_files(database_path=database, ledger_path=ledger)
    original_parent = database.parent
    original_parent_stat = os.fstat(opened.database.parent_fd)
    moved_parent = tmp_path / "moved-parent"
    try:
        original_parent.rename(moved_parent)
        original_parent.mkdir()
        (original_parent / database.name).write_bytes(b"replacement")
        (original_parent / ledger.name).write_bytes(b"replacement")

        current_parent_stat = os.stat(original_parent)
        assert (current_parent_stat.st_dev, current_parent_stat.st_ino) != (
            original_parent_stat.st_dev,
            original_parent_stat.st_ino,
        )
        with pytest.raises(CollectorContinuityError, match="physical identity"):
            opened.verify_identities()
    finally:
        opened.close()


def test_create_exclusive_regular_file_is_private_and_single_use(tmp_path: Path) -> None:
    parent_fd = _open_directory(tmp_path)
    try:
        descriptor = create_exclusive_regular_file(parent_fd, "new-ledger.jsonl")
        try:
            mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            assert mode == 0o600
            assert stat.S_ISREG(os.fstat(descriptor).st_mode)
        finally:
            os.close(descriptor)

        with pytest.raises(CollectorContinuityError):
            create_exclusive_regular_file(parent_fd, "new-ledger.jsonl")
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize(
    "leaf",
    ["", ".", "..", "nested/name", "/absolute/name"],
    ids=["empty", "dot", "dot-dot", "nested", "absolute"],
)
def test_create_exclusive_regular_file_rejects_invalid_leaf(
    tmp_path: Path, leaf: str
) -> None:
    parent_fd = _open_directory(tmp_path)
    try:
        with pytest.raises(CollectorContinuityError):
            create_exclusive_regular_file(parent_fd, leaf)
    finally:
        os.close(parent_fd)


def test_create_exclusive_regular_file_rejects_non_directory_parent_fd(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"data")
    parent_fd = os.open(regular, os.O_RDONLY)
    try:
        with pytest.raises(CollectorContinuityError, match="directory"):
            create_exclusive_regular_file(parent_fd, "new-file")
    finally:
        os.close(parent_fd)


def test_future_prepare_creates_bound_12_by_3_collector_and_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)

    assert prepared["schema_version"] == COLLECTOR_PREPARATION_SCHEMA
    assert isinstance(prepared["database_uuid"], str)
    assert len(prepared["database_uuid"]) == 64
    assert all(character in "0123456789abcdef" for character in prepared["database_uuid"])
    assert isinstance(prepared["cohort_sha256"], str)
    assert len(prepared["genesis_sha256"]) == 64
    assert len(prepared["ledger_genesis_event_sha256"]) == 64
    assert prepared["database_path"] == str(database.resolve())
    assert prepared["ledger_path"] == str(ledger.resolve())
    assert PhysicalFileIdentity.from_dict(prepared["database_identity"])
    assert PhysicalFileIdentity.from_dict(prepared["ledger_identity"])
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_future_prepare_round_trip_cross_binds_database_row_and_genesis_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    loaded = load_verified_prepared_collector(
        database_path=database,
        ledger_path=ledger,
    )

    assert loaded == prepared
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT singleton,database_uuid,cohort_sha256,genesis_json,genesis_sha256,"
            "ledger_genesis_event_sha256,created_at FROM forward_collector_genesis"
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    genesis = json.loads(row[3])
    event = _ledger_event(ledger)
    assert genesis["schema_version"] == COLLECTOR_GENESIS_SCHEMA
    assert row[1] == prepared["database_uuid"] == genesis["database_uuid"]
    assert row[2] == prepared["cohort_sha256"] == genesis["cohort_sha256"]
    assert row[4] == prepared["genesis_sha256"] == canonical_json_sha256(genesis)
    assert row[5] == prepared["ledger_genesis_event_sha256"]
    assert event["schema_version"] == COLLECTOR_LEDGER_EVENT_SCHEMA
    assert event["event_type"] == "GENESIS"
    assert event["seq"] == 0
    assert event["previous_event_sha256"] == "0" * 64
    assert event["event"]["genesis"] == genesis
    assert decode_collector_ledger_event(canonical_json_bytes(event)) == event
    assert event["event_sha256"] == canonical_json_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )


def test_prepared_collector_loads_in_a_fresh_process_without_runtime_schema_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    repository = Path(__file__).resolve().parents[1]
    script = """
import sys

sys.path.insert(0, sys.argv[1])
from stockdata.collector_continuity import load_verified_prepared_collector

loaded = load_verified_prepared_collector(
    database_path=sys.argv[2],
    ledger_path=sys.argv[3],
)
assert loaded["database_path"] == sys.argv[2]
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repository), str(database), str(ledger)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "fresh-process prepared loading failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert load_verified_prepared_collector(
        database_path=database, ledger_path=ledger
    ) == prepared


def test_genesis_short_write_is_completed_or_fails_before_database_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    database = tmp_path / "short-write.sqlite"
    ledger = Path(default_collector_ledger_path(database))
    original_write = continuity.os.write
    shorted = False

    def short_once(fd: int, data: bytes) -> int:
        nonlocal shorted
        if not shorted:
            shorted = True
            assert len(data) > 1
            return original_write(fd, data[:-1])
        return original_write(fd, data)

    monkeypatch.setattr(continuity.os, "write", short_once)
    try:
        prepared = prepare_future_collector_database(
            database_file=database,
            panel_file=_future_panel_file(tmp_path),
        )
    except FuturePanelRegistrationError:
        if database.exists() and ledger.exists():
            with pytest.raises(CollectorContinuityError):
                load_verified_prepared_collector(
                    database_path=database, ledger_path=ledger
                )
        if database.exists():
            try:
                with sqlite3.connect(database) as connection:
                    genesis_count = connection.execute(
                        "SELECT COUNT(*) FROM forward_collector_genesis"
                    ).fetchone()[0]
            except sqlite3.Error:
                pass
            else:
                assert genesis_count == 0
    else:
        assert load_verified_prepared_collector(
            database_path=database, ledger_path=ledger
        ) == prepared
        assert continuity.parse_collector_ledger(ledger)[0]["event_type"] == "GENESIS"

    assert shorted


def test_future_prepare_fsyncs_the_collector_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_stat = tmp_path.stat()
    original_fsync = continuity.os.fsync
    directory_fsyncs: list[int] = []

    def record_fsync(fd: int) -> None:
        current = os.fstat(fd)
        if stat.S_ISDIR(current.st_mode) and (
            current.st_dev,
            current.st_ino,
        ) == (parent_stat.st_dev, parent_stat.st_ino):
            directory_fsyncs.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(continuity.os, "fsync", record_fsync)
    _prepare_collector(tmp_path, monkeypatch)

    assert directory_fsyncs


def test_future_prepare_same_paths_reject_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    database_bytes = database.read_bytes()
    ledger_bytes = ledger.read_bytes()

    with pytest.raises(FuturePanelRegistrationError):
        prepare_future_collector_database(
            database_file=database,
            panel_file=_future_panel_file(tmp_path),
        )

    assert database.read_bytes() == database_bytes
    assert ledger.read_bytes() == ledger_bytes


@pytest.mark.parametrize(
    "mutation",
    ["detail", "event_hash", "previous", "seq"],
    ids=["detail", "event-hash", "previous-hash", "sequence"],
)
def test_genesis_ledger_mutations_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    event = _ledger_event(ledger)
    if mutation == "detail":
        event["event"]["genesis"]["created_at"] = "2026-08-01T12:00:01+08:00"
    elif mutation == "event_hash":
        event["event_sha256"] = "f" * 64
    elif mutation == "previous":
        event["previous_event_sha256"] = "1" * 64
    else:
        event["seq"] = 1
    _write_ledger_event(ledger, event)

    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_rehashed_genesis_detail_still_fails_cross_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    event = _ledger_event(ledger)
    event["event"]["genesis"]["created_at"] = "2026-08-01T12:00:01+08:00"
    event["event_sha256"] = canonical_json_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )
    _write_ledger_event(ledger, event)

    with pytest.raises(CollectorContinuityError, match="prepared genesis drifted"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


@pytest.mark.parametrize(
    "extra_kind",
    [
        "blank-line",
        "extra-event",
    ],
    ids=["blank-line", "extra-event"],
)
def test_genesis_only_ledger_rejects_extra_bytes_or_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_kind: str,
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    original = ledger.read_bytes()
    if extra_kind == "blank-line":
        extra = b"\n"
    else:
        genesis = continuity.parse_collector_ledger(original)[0]
        extra_event = continuity.build_collector_ledger_event(
            previous_event=genesis,
            event_type="ATTEMPT_STARTED",
            event={
                "registration_sha256": "d" * 64,
                "database_uuid": "b" * 64,
                "session": "2099-01-05",
                "phase": "pre_open",
                "step_id": "pre_open_context",
                "step_ordinal": 0,
                "attempt_id": "attempt-0001",
                "command_sha256": "c" * 64,
                "lease_nonce_sha256": "7" * 64,
                "started_at": "2026-08-23T00:00:02+08:00",
                "state_before_sha256": "e" * 64,
                "step_state_before": {
                    "schema_version": "stockdata-forward-collector-step-state/1",
                    "collector_state_sha256": "e" * 64,
                    "table_counts": {table: 0 for table in continuity.COLLECTOR_STATE_TABLES},
                    "table_sha256": {
                        table: "a" * 64 for table in continuity.COLLECTOR_STATE_TABLES
                    },
                    "outside_scope_sha256": {
                        "collection_receipts": "b" * 64,
                        "forward_context_observations": "b" * 64,
                        "forward_universe_observations": "b" * 64,
                        "forward_status_observations": "b" * 64,
                    },
                    "receipt_id_high_water": 0,
                },
                "step_raw_before": {
                    "schema_version": continuity.COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
                    "selector_rows": {
                        "collection_receipts": [],
                        "forward_context_observations": [],
                        "forward_universe_observations": [],
                        "forward_status_observations": [],
                    },
                },
            },
        )
        extra = canonical_json_bytes(extra_event) + b"\n"
    ledger.write_bytes(original + extra)

    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_open_exact_collector_sqlite_sets_all_frozen_pragmas(tmp_path: Path) -> None:
    database, ledger = _empty_sqlite_pair(tmp_path)

    with open_exact_collector_sqlite(
        database_path=database,
        ledger_path=ledger,
    ) as (connection, _):
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            connection.execute("PRAGMA busy_timeout").fetchone()[0]
            == COLLECTOR_SQLITE_BUSY_TIMEOUT_MS
        )


def test_open_exact_collector_sqlite_rejects_residual_wal_or_shm(
    tmp_path: Path,
) -> None:
    database, ledger = _empty_sqlite_pair(tmp_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        sidecar.write_bytes(b"residual")
        before_fds = _fd_identity_snapshot()
        try:
            try:
                with pytest.raises(CollectorContinuityError, match="WAL/SHM"):
                    with open_exact_collector_sqlite(
                        database_path=database,
                        ledger_path=ledger,
                    ):
                        pass
            finally:
                sidecar.unlink()
        finally:
            _assert_no_new_collector_fds(
                before_fds,
                database=database,
                ledger=ledger,
            )


def test_open_exact_collector_sqlite_rejects_persisted_wal_mode(tmp_path: Path) -> None:
    database, ledger = _empty_sqlite_pair(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        connection.execute("CREATE TABLE marker (value INTEGER)")
        connection.commit()

    before_fds = _fd_identity_snapshot()
    try:
        with pytest.raises(
            CollectorContinuityError,
            match="journal mode|WAL/SHM|sidecar",
        ):
            with open_exact_collector_sqlite(database_path=database, ledger_path=ledger):
                pass
    finally:
        _assert_no_new_collector_fds(
            before_fds,
            database=database,
            ledger=ledger,
        )


@pytest.mark.parametrize(
    "mutation",
    ["wal-sidecar", "shm-sidecar", "synchronous", "journal-mode"],
)
def test_open_exact_collector_sqlite_rechecks_context_drift_on_exit(
    tmp_path: Path, mutation: str
) -> None:
    database, ledger = _empty_sqlite_pair(tmp_path)

    before_fds = _fd_identity_snapshot()
    try:
        with pytest.raises(CollectorContinuityError, match="journal|WAL|SHM|synchronous|pragma"):
            with open_exact_collector_sqlite(
                database_path=database,
                ledger_path=ledger,
            ) as (connection, _):
                if mutation == "wal-sidecar":
                    Path(f"{database}-wal").write_bytes(b"appeared")
                elif mutation == "shm-sidecar":
                    Path(f"{database}-shm").write_bytes(b"appeared")
                elif mutation == "synchronous":
                    connection.execute("PRAGMA synchronous=NORMAL")
                else:
                    assert connection.execute(
                        "PRAGMA journal_mode=TRUNCATE"
                    ).fetchone()[0].lower() == "truncate"
    finally:
        _assert_no_new_collector_fds(
            before_fds,
            database=database,
            ledger=ledger,
        )


def test_prepare_does_not_migrate_an_existing_schema_less_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    panel_file = _future_panel_file(tmp_path)
    database = tmp_path / "existing.sqlite"
    database.write_bytes(b"preexisting")

    with pytest.raises(FuturePanelRegistrationError):
        prepare_future_collector_database(database_file=database, panel_file=panel_file)

    assert database.read_bytes() == b"preexisting"
    assert not Path(default_collector_ledger_path(database)).exists()


@pytest.mark.parametrize("replaced", ["database", "ledger"], ids=["database", "ledger"])
def test_prepared_collector_rejects_database_or_ledger_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: str
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    path = database if replaced == "database" else ledger
    replacement = path.with_name(f"{path.name}.replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)

    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_genesis_update_and_delete_are_blocked_by_immutable_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE forward_collector_genesis SET database_uuid=?",
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("DELETE FROM forward_collector_genesis")


@pytest.mark.parametrize(
    "field",
    ["database_uuid", "cohort_sha256", "genesis_sha256", "ledger_genesis_event_sha256"],
)
def test_genesis_row_hash_and_identity_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_update")
        connection.execute(
            f"UPDATE forward_collector_genesis SET {field}=?", ("a" * 64,)
        )
        connection.execute(continuity._GENESIS_TRIGGERS["forward_collector_genesis_no_update"])
        connection.commit()

    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_missing_genesis_row_and_trigger_schema_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_update")
        connection.execute("DROP TRIGGER forward_collector_genesis_no_delete")
        connection.execute("DELETE FROM forward_collector_genesis")
        for sql in continuity._GENESIS_TRIGGERS.values():
            connection.execute(sql)
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="exactly one row"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_missing_genesis_trigger_is_rejected_before_loaded_data_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_delete")
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="triggers"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_cohort_hash_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        spec_json = connection.execute(
            "SELECT spec_json FROM forward_capture_cohort WHERE singleton=1"
        ).fetchone()[0]
        spec = json.loads(spec_json)
        spec["source"] = "tampered"
        encoded = canonical_json_bytes(spec).decode("ascii")
        connection.execute("DROP TRIGGER forward_capture_cohort_no_update")
        connection.execute(
            "UPDATE forward_capture_cohort SET spec_json=?, spec_sha256=? WHERE singleton=1",
            (encoded, canonical_json_sha256(spec)),
        )
        connection.execute(
            COLLECTOR_EVIDENCE_TRIGGER_SQL["forward_capture_cohort_no_update"]
        )
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="prepared genesis drifted"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


_DAILY_KEY = {
    "code": "000001.SZ",
    "date": "2099-01-05",
    "source": "tencent",
    "adjustment_mode": "raw",
    "adjustment_version": "tencent-qt-daily-v1",
}
_DAILY_ROW = {
    **_DAILY_KEY,
    "open": None,
    "high": 11.0,
    "low": 9.0,
    "close": 10.0,
    "volume": None,
    "retrieved_at": "2099-01-05T16:00:00+08:00",
    "is_final": 1,
    "receipt_id": None,
}
_DAILY_COLUMNS = (
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "adjustment_mode",
    "adjustment_version",
    "retrieved_at",
    "is_final",
    "receipt_id",
)


def _insert_daily_row(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    connection.execute(
        "INSERT INTO daily ("
        + ",".join(_DAILY_COLUMNS)
        + ") VALUES ("
        + ",".join("?" for _ in _DAILY_COLUMNS)
        + ")",
        tuple(row[column] for column in _DAILY_COLUMNS),
    )


def _daily_where() -> str:
    return " AND ".join(f"{column}=?" for column in _DAILY_KEY)


def _daily_key_values() -> tuple[object, ...]:
    return tuple(_DAILY_KEY[column] for column in _DAILY_KEY)


def test_finalized_daily_identical_full_update_is_a_null_safe_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        _insert_daily_row(connection, _DAILY_ROW)
        connection.commit()
        before = connection.execute(
            "SELECT " + ",".join(_DAILY_COLUMNS) + " FROM daily"
        ).fetchone()

        assignments = ",".join(f"{column}=?" for column in _DAILY_COLUMNS)
        connection.execute(
            f"UPDATE daily SET {assignments} WHERE {_daily_where()}",
            tuple(_DAILY_ROW[column] for column in _DAILY_COLUMNS) + _daily_key_values(),
        )
        connection.commit()

        assert connection.execute(
            "SELECT " + ",".join(_DAILY_COLUMNS) + " FROM daily"
        ).fetchone() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "000333.SZ"),
        ("date", "2099-01-06"),
        ("open", 1.0),
        ("high", 12.0),
        ("low", 8.0),
        ("close", 11.0),
        ("volume", 100.0),
        ("source", "other-source"),
        ("adjustment_mode", "qfq"),
        ("adjustment_version", "other-version"),
        ("retrieved_at", "2099-01-05T16:01:00+08:00"),
        ("is_final", 0),
        ("receipt_id", 7),
    ],
)
def test_finalized_daily_any_persisted_field_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        _insert_daily_row(connection, _DAILY_ROW)
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="finalized daily evidence is immutable"):
            connection.execute(
                f"UPDATE daily SET {field}=? WHERE {_daily_where()}",
                (value,) + _daily_key_values(),
            )
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1
        assert connection.execute(
            f"SELECT {field} FROM daily WHERE {_daily_where()}", _daily_key_values()
        ).fetchone()[0] == _DAILY_ROW[field]


def test_finalized_daily_delete_is_rejected_and_nonfinal_can_be_finalized_then_freezes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    pending = {**_DAILY_ROW, "is_final": 0}
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            _insert_daily_row(connection, pending)
        connection.rollback()
        _insert_daily_row(connection, _DAILY_ROW)
        connection.commit()
        connection.execute(
            f"UPDATE daily SET is_final=1 WHERE {_daily_where()}", _daily_key_values()
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="finalized daily evidence is immutable"):
            connection.execute(f"DELETE FROM daily WHERE {_daily_where()}", _daily_key_values())
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="finalized daily evidence is immutable"):
            connection.execute(
                f"UPDATE daily SET close=11.0 WHERE {_daily_where()}", _daily_key_values()
            )
        connection.rollback()
        assert connection.execute("SELECT is_final FROM daily").fetchone()[0] == 1


_SYNC_KEY = {
    "code": "000001.SZ",
    "source": "tencent",
    "adjustment_mode": "raw",
    "adjustment_version": "tencent-qt-daily-v1",
}
_SYNC_ROW = {
    **_SYNC_KEY,
    "start_date": "2099-01-05",
    "end_date": "2099-01-06",
    "retrieved_at": "2099-01-06T16:00:00+08:00",
}


def _sync_where() -> str:
    return " AND ".join(f"{column}=?" for column in _SYNC_KEY)


def _sync_key_values() -> tuple[object, ...]:
    return tuple(_SYNC_KEY[column] for column in _SYNC_KEY)


def test_sync_coverage_widens_for_fixed_identity_and_allows_retrieved_at_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sync_coverage (code,source,adjustment_mode,adjustment_version,"
            "start_date,end_date,retrieved_at) VALUES (?,?,?,?,?,?,?)",
            tuple(_SYNC_ROW.values()),
        )
        connection.commit()
        connection.execute(
            f"UPDATE sync_coverage SET start_date=?,end_date=?,retrieved_at=? WHERE {_sync_where()}",
            ("2099-01-01", "2099-01-10", "2099-01-10T16:00:00+08:00")
            + _sync_key_values(),
        )
        connection.commit()
        assert connection.execute(
            "SELECT start_date,end_date,retrieved_at FROM sync_coverage"
        ).fetchone() == ("2099-01-01", "2099-01-10", "2099-01-10T16:00:00+08:00")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_date", "2099-01-06"),
        ("end_date", "2099-01-05"),
        ("code", "000333.SZ"),
        ("source", "other-source"),
        ("adjustment_mode", "qfq"),
        ("adjustment_version", "other-version"),
    ],
)
def test_sync_coverage_rejects_shrink_or_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sync_coverage (code,source,adjustment_mode,adjustment_version,"
            "start_date,end_date,retrieved_at) VALUES (?,?,?,?,?,?,?)",
            tuple(_SYNC_ROW.values()),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="sync coverage must retain identity"):
            connection.execute(
                f"UPDATE sync_coverage SET {field}=? WHERE {_sync_where()}",
                (value,) + _sync_key_values(),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT start_date,end_date,retrieved_at FROM sync_coverage"
        ).fetchone() == (
            _SYNC_ROW["start_date"],
            _SYNC_ROW["end_date"],
            _SYNC_ROW["retrieved_at"],
        )


def test_sync_coverage_delete_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sync_coverage (code,source,adjustment_mode,adjustment_version,"
            "start_date,end_date,retrieved_at) VALUES (?,?,?,?,?,?,?)",
            tuple(_SYNC_ROW.values()),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="sync coverage is append-only"):
            connection.execute(f"DELETE FROM sync_coverage WHERE {_sync_where()}", _sync_key_values())
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM sync_coverage").fetchone()[0] == 1


@pytest.mark.parametrize("mutation", ["missing", "replaced"], ids=["missing", "replaced"])
@pytest.mark.parametrize("trigger_name", sorted(COLLECTOR_EVIDENCE_TRIGGER_SQL))
def test_every_collector_evidence_trigger_is_verified_by_load_and_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    trigger_name: str,
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")
        if mutation == "replaced":
            connection.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE ON forward_capture_cohort "
                "BEGIN SELECT RAISE(ABORT, 'tampered trigger'); END"
            )
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector evidence triggers"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)
    with pytest.raises(FuturePanelRegistrationError):
        verify_collector_capability(
            database,
            symbols=_PREPARE_SYMBOLS,
            first_session=_PREPARE_SESSIONS[0],
        )


def test_unknown_collector_evidence_trigger_is_rejected_by_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER collector_unexpected_trigger "
            "AFTER INSERT ON daily BEGIN SELECT 1; END"
        )
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector evidence triggers"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


@pytest.mark.parametrize("table", tuple(continuity._COLLECTOR_OWNED_TABLE_SQL))
def test_every_collector_owned_table_rejects_an_added_persistent_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'ALTER TABLE "{table}" ADD COLUMN collector_schema_drift TEXT'
        )
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector-owned table schemas"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_collector_owned_table_constraint_definition_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    table = "forward_context_observations"
    original_sql = continuity._COLLECTOR_OWNED_TABLE_SQL[table]
    altered_sql = original_sql.replace(
        "observation_phase IN ('pre_open','post_close')",
        "observation_phase IN ('pre_open','post_close','tampered')",
    )
    assert altered_sql != original_sql
    with sqlite3.connect(database) as connection:
        for trigger_name in (
            "forward_context_observations_no_update",
            "forward_context_observations_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(
            "ALTER TABLE forward_context_observations "
            "RENAME TO forward_context_observations_legacy"
        )
        connection.execute(altered_sql)
        connection.execute("DROP TABLE forward_context_observations_legacy")
        for trigger_name, trigger_sql in COLLECTOR_EVIDENCE_TRIGGER_SQL.items():
            if trigger_name.startswith("forward_context_observations_"):
                connection.execute(trigger_sql)
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector-owned table schemas"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_collector_owned_table_sql_literal_case_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    table = "forward_context_observations"
    original_sql = continuity._COLLECTOR_OWNED_TABLE_SQL[table]
    drifted_sql = original_sql.replace("'pre_open'", "'PRE_OPEN'")
    assert drifted_sql != original_sql
    with sqlite3.connect(database) as connection:
        for trigger_name in (
            "forward_context_observations_no_update",
            "forward_context_observations_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(
            "ALTER TABLE forward_context_observations "
            "RENAME TO forward_context_observations_legacy"
        )
        connection.execute(drifted_sql)
        connection.execute("DROP TABLE forward_context_observations_legacy")
        for trigger_name, trigger_sql in COLLECTOR_EVIDENCE_TRIGGER_SQL.items():
            if trigger_name.startswith("forward_context_observations_"):
                connection.execute(trigger_sql)
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector-owned table schemas"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_collector_trigger_sql_literal_case_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    trigger_name = "forward_capture_cohort_no_update"
    original_sql = COLLECTOR_EVIDENCE_TRIGGER_SQL[trigger_name]
    drifted_sql = original_sql.replace(
        "'forward capture cohort is immutable'",
        "'forward capture cohort is IMMUTABLE'",
    )
    assert drifted_sql != original_sql
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(drifted_sql)
        connection.commit()

    with pytest.raises(CollectorContinuityError, match="collector evidence triggers"):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_legacy_capability_rejects_missing_genesis_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_update")
        connection.execute("DROP TRIGGER forward_collector_genesis_no_delete")
        connection.execute("DROP TABLE forward_collector_genesis")
        connection.execute(
            "CREATE TRIGGER legacy_custom_noop AFTER INSERT ON daily "
            "BEGIN SELECT 1; END"
        )
        connection.commit()

    assert continuity.database_has_collector_genesis(database) is False
    with pytest.raises(FuturePanelRegistrationError):
        verify_collector_capability(
            database,
            symbols=_PREPARE_SYMBOLS,
            first_session=_PREPARE_SESSIONS[0],
            require_clean=True,
        )


def test_research_cache_without_collector_genesis_keeps_update_and_delete_behavior(
    tmp_path: Path,
) -> None:
    database = tmp_path / "research.sqlite"
    cache = Cache(database)
    cache.upsert(
        "000001.SZ",
        [{"date": "2020-01-02", "open": 1.0, "close": 2.0}],
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="research-v1",
        retrieved_at="2020-01-02T16:00:00+08:00",
    )
    cache.close()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='forward_collector_genesis'"
        ).fetchone() is None
        connection.execute(
            "UPDATE daily SET close=3.0 WHERE code=? AND date=?",
            ("000001.SZ", "2020-01-02"),
        )
        connection.execute(
            "DELETE FROM daily WHERE code=? AND date=?",
            ("000001.SZ", "2020-01-02"),
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0


def test_prepare_failure_cleans_only_new_database_ledger_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    database = tmp_path / "new.sqlite"
    ledger = Path(default_collector_ledger_path(database))

    def fail_install(connection: sqlite3.Connection, *, symbols: object, first_session: str) -> None:
        del connection, symbols, first_session
        for suffix in ("-journal", "-wal", "-shm"):
            Path(f"{database}{suffix}").write_bytes(b"temporary")
        raise sqlite3.OperationalError("forced prepare failure")

    monkeypatch.setattr(
        "stockdata.future_panel_registration._install_fresh_collector_schema",
        fail_install,
    )
    before_fds = _fd_identity_snapshot()
    try:
        with pytest.raises(FuturePanelRegistrationError, match="preparation failed"):
            prepare_future_collector_database(
                database_file=database,
                panel_file=_future_panel_file(tmp_path),
            )
    finally:
        _assert_no_new_collector_fds(
            before_fds,
            database=database,
            ledger=ledger,
        )

    assert not database.exists()
    assert not ledger.exists()
    assert not any(Path(f"{database}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_prepare_failure_never_removes_preexisting_database_ledger_or_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    database = tmp_path / "existing.sqlite"
    ledger = Path(default_collector_ledger_path(database))
    database.write_bytes(b"database-sentinel")
    ledger.write_bytes(b"ledger-sentinel")
    sidecars = {
        suffix: Path(f"{database}{suffix}")
        for suffix in ("-journal", "-wal", "-shm")
    }
    for path in sidecars.values():
        path.write_bytes(b"sidecar-sentinel")

    with pytest.raises(FuturePanelRegistrationError):
        prepare_future_collector_database(
            database_file=database,
            panel_file=_future_panel_file(tmp_path),
        )

    assert database.read_bytes() == b"database-sentinel"
    assert ledger.read_bytes() == b"ledger-sentinel"
    assert all(path.read_bytes() == b"sidecar-sentinel" for path in sidecars.values())


def test_genesis_database_enters_v4_registration_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, panel_file, database, _ = _prepare_collector(tmp_path, monkeypatch)
    output = tmp_path / "registration.json"
    static_called = False

    def stop_after_genesis_admission(**kwargs: object) -> object:
        nonlocal static_called
        static_called = True
        raise FuturePanelRegistrationError("fixture stops after genesis admission")

    monkeypatch.setattr(
        "stockdata.future_panel_registration._static_prerequisites",
        stop_after_genesis_admission,
    )
    with pytest.raises(FuturePanelRegistrationError, match="fixture stops"):
        register_future_panel(
            output_file=output,
            database_file=database,
            panel_file=panel_file,
            source_receipt_files=(),
            calendar_file=tmp_path / "missing-calendar.json",
            calendar_authority_file=tmp_path / "missing-calendar-authority.json",
            market_rules_file=tmp_path / "missing-rules.json",
            market_rules_authority_file=tmp_path / "missing-rules-authority.json",
        )

    assert static_called
    assert not output.exists()


def test_future_prepare_cli_emits_parseable_preparation_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    from stockdata.cli import main

    database = tmp_path / "cli.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stockdata-cli",
            "future-panel-prepare",
            "--database",
            str(database),
            "--panel-file",
            str(_future_panel_file(tmp_path)),
        ],
    )
    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == COLLECTOR_PREPARATION_SCHEMA
    assert output["database_path"] == str(database.resolve())
    assert output["ledger_path"] == default_collector_ledger_path(database)
    assert output["database_identity"]["schema_version"] == PHYSICAL_FILE_IDENTITY_SCHEMA
    assert output["ledger_identity"]["schema_version"] == PHYSICAL_FILE_IDENTITY_SCHEMA


def test_step_schedule_uses_fixed_selector_source_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    schedule = _task24_schedule(database)
    expected = {
        "pre_open_context": "sina-market-center-hs-a-v1",
        "pre_open_corporate_actions": "baostock-query-dividend-data-v1",
        "post_close_context": "sina-market-center-hs-a-v1",
        "post_close_prices": "tencent",
    }
    assert {spec.step_id: spec.source for spec in schedule} == {
        step_id: expected[step_id] for step_id in expected for _ in _PREPARE_SESSIONS
    }


def test_context_full_market_universe_is_inside_selector_but_foreign_status_is_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _task24_schedule(database)[0]
    response_sha = hashlib.sha256(b"{}").hexdigest()
    with continuity.open_exact_collector_sqlite(
        database_path=database,
        ledger_path=ledger,
    ) as (connection, _):
        baseline = _task24_snapshot(spec)
        connection.execute(
            "INSERT INTO collection_receipts "
            "(receipt_id,observed_at,source,request_json,response_json,response_sha256,created_at) "
            "VALUES (1,?,?,?,?,?,?)",
            (
                "2099-01-05T08:00:00+08:00",
                spec.source,
                "{}",
                "{}",
                response_sha,
                "2099-01-05T08:00:01+08:00",
            ),
        )
        connection.execute(
            "INSERT INTO forward_universe_observations "
            "(effective_date,observation_phase,symbol,is_member,source,receipt_id) "
            "VALUES (?,?,?,?,?,?)",
            (spec.session, spec.phase, "999999.SZ", 0, spec.source, 1),
        )
        connection.commit()
        universe = _task24_snapshot(spec)
        connection.execute(
            "INSERT INTO forward_status_observations "
            "(effective_date,observation_phase,symbol,name,listing_status,board,"
            "is_st,is_suspended,source,receipt_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                spec.session,
                spec.phase,
                "999999.SZ",
                "foreign",
                "listed",
                "main",
                0,
                0,
                spec.source,
                1,
            ),
        )
        connection.commit()
        status = _task24_snapshot(spec)

    assert (
        universe["outside_scope_sha256"]["forward_universe_observations"]
        == baseline["outside_scope_sha256"]["forward_universe_observations"]
    )
    assert (
        status["outside_scope_sha256"]["forward_status_observations"]
        != universe["outside_scope_sha256"]["forward_status_observations"]
    )


@pytest.mark.parametrize(("schedule_index", "ordinal"), ((0, 12), (3, 15)))
def test_step_snapshot_rejects_out_of_range_global_ordinal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_index: int,
    ordinal: int,
) -> None:
    _, _, database, _ = _prepare_collector(tmp_path, monkeypatch)
    spec = replace(_task24_schedule(database)[schedule_index], step_ordinal=ordinal)
    with open_exact_collector_sqlite(
        database_path=database,
        ledger_path=Path(default_collector_ledger_path(database)),
    ) as (connection, _):
        with pytest.raises(CollectorContinuityError):
            _task24_snapshot(spec)


def test_collection_receipt_complement_detects_mixed_legal_and_foreign_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _task24_schedule(database)[2]
    with open_exact_collector_sqlite(
        database_path=database,
        ledger_path=ledger,
    ) as (connection, _):
        baseline = _task24_snapshot(spec)
        connection.execute(
            """
            INSERT INTO collection_receipts
                (receipt_id, observed_at, source, request_json, response_json,
                 response_sha256, created_at)
            VALUES (2, ?, ?, '{}', '{}', ?, ?)
            """,
            (
                "2099-01-05T09:00:00+08:00",
                spec.source,
                "b" * 64,
                "2099-01-05T09:00:00+08:00",
            ),
        )
        connection.commit()
        orphan = _task24_snapshot(spec)
        assert (
            orphan["outside_scope_sha256"]["collection_receipts"]
            != baseline["outside_scope_sha256"]["collection_receipts"]
        )
        connection.execute(
            """
            INSERT INTO collection_receipts
                (receipt_id, observed_at, source, request_json, response_json,
                 response_sha256, created_at)
            VALUES (1, ?, ?, '{}', '{}', ?, ?)
            """,
            (
                "2099-01-05T09:00:00+08:00",
                spec.source,
                "a" * 64,
                "2099-01-05T09:00:00+08:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO forward_context_observations
                (effective_date, observation_phase, decision_available_at,
                 outcome_observed_at, finalized_at, source, receipt_id)
            VALUES (?, 'post_close', NULL, ?, ?, ?, 1),
                   ('2099-01-08', 'post_close', NULL, ?, ?, ?, 1),
                   (?, 'pre_open', ?, NULL, NULL, ?, 1)
            """,
            (
                spec.session,
                "2099-01-05T16:00:00+08:00",
                "2099-01-05T16:01:00+08:00",
                spec.source,
                "2099-01-08T16:00:00+08:00",
                "2099-01-08T16:01:00+08:00",
                spec.source,
                spec.session,
                "2099-01-05T08:00:00+08:00",
                spec.source,
            ),
        )
        connection.execute(
            """
            INSERT INTO forward_status_observations
                (effective_date, observation_phase, symbol, name, listing_status,
                 board, is_st, is_suspended, source, receipt_id)
            VALUES (?, 'post_close', '999999.SZ', 'foreign', 'listed',
                    'main', 0, 0, ?, 1)
            """,
            (spec.session, spec.source),
        )
        connection.execute(
            """
            INSERT INTO forward_corporate_actions
                (observation_date, symbol, event_id, effective_date,
                 announcement_date, payload_json, available_at, source, receipt_id)
            VALUES (?, '000001.SZ', 'foreign-event', NULL, NULL, '{}', ?, ?, 1)
            """,
            (spec.session, "2099-01-05T16:00:00+08:00", spec.source),
        )
        connection.commit()
        mixed = _task24_snapshot(spec)
        assert (
            mixed["outside_scope_sha256"]["collection_receipts"]
            != orphan["outside_scope_sha256"]["collection_receipts"]
        )


def test_price_snapshot_detects_mixed_legal_and_foreign_adjustment_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _task24_schedule(database)[3]
    response_sha = hashlib.sha256(b"{}").hexdigest()
    with continuity.open_exact_collector_sqlite(
        database_path=database,
        ledger_path=ledger,
    ) as (connection, _):
        baseline = _task24_snapshot(spec)
        connection.executemany(
            "INSERT INTO collection_receipts "
            "(receipt_id,observed_at,source,request_json,response_json,response_sha256,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                (
                    receipt_id,
                    "2099-01-06T16:00:00+08:00",
                    spec.source,
                    "{}",
                    "{}",
                    response_sha,
                    "2099-01-06T16:00:01+08:00",
                )
                for receipt_id in (1, 2)
            ),
        )
        connection.execute(
            "INSERT INTO daily "
            "(code,date,open,high,low,close,volume,source,adjustment_mode,"
            "adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _PREPARE_SYMBOLS[0],
                spec.session,
                1.0,
                2.0,
                0.5,
                1.5,
                100.0,
                spec.source,
                "raw",
                "tencent-qt-daily-v1",
                "2099-01-06T16:00:01+08:00",
                1,
                1,
            ),
        )
        connection.commit()
        legal = _task24_snapshot(spec)
        connection.execute(
            "INSERT INTO daily "
            "(code,date,open,high,low,close,volume,source,adjustment_mode,"
            "adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _PREPARE_SYMBOLS[0],
                spec.session,
                1.0,
                2.0,
                0.5,
                1.5,
                100.0,
                spec.source,
                "qfq",
                "foreign-adjustment-v1",
                "2099-01-06T16:00:02+08:00",
                1,
                2,
            ),
        )
        connection.commit()
        mixed = _task24_snapshot(spec)

    assert legal["outside_scope_sha256"]["daily"] == baseline["outside_scope_sha256"]["daily"]
    assert mixed["outside_scope_sha256"]["daily"] != legal["outside_scope_sha256"]["daily"]
