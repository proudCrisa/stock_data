from __future__ import annotations

import copy
import fcntl
import os
import pickle
from pathlib import Path
from typing import Any

import pytest

import stockdata.collector_continuity as continuity
from stockdata.cache import Cache
from stockdata.collector_continuity import CollectorContinuityError
from test_collector_step_state import _bound_registration, _prepare_collector, _schedule


def _api(name: str) -> Any:
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing task 2.5 API: {name}")
    return value


def _prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    return {
        "database": database,
        "ledger": ledger,
        "registration": _bound_registration(database),
        "schedule": _schedule(database),
    }


def _child_environment(
    spec: object,
    launch: object,
    *,
    lease_fd: int,
    nonce_fd: int,
    attempt_id: str | None = None,
) -> dict[str, str]:
    environment = continuity._collector_attempt_child_environment(
        launch, lease_fd=lease_fd, nonce_fd=nonce_fd
    )
    if attempt_id is not None:
        environment["STOCKDATA_COLLECTOR_ATTEMPT_ID"] = attempt_id
    return environment


def test_marked_collector_database_rejects_ordinary_cache_before_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    with pytest.raises(CollectorContinuityError):
        Cache(prepared["database"])


def test_noncollector_cache_behavior_is_unchanged(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "ordinary.sqlite")
    try:
        cache._conn.execute(
            "INSERT INTO daily (code,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            ("000001.SZ", "2099-01-05", 1, 2, 0.5, 1.5, 100),
        )
        cache._conn.commit()
        assert cache._conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1
    finally:
        cache.close()


def test_self_locked_fd_without_nonce_is_rejected_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    writer_factory = _api("open_collector_child_writer_authority")
    provider_calls: list[object] = []
    monkeypatch.setattr(continuity, "_provider_call", lambda *a, **k: provider_calls.append((a, k)), raising=False)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = _api("_begin_collector_step_attempt")(lease, spec)
    self_locked_fd = os.open(prepared["ledger"], os.O_RDWR | os.O_NOFOLLOW)
    probe_fd = os.open(prepared["ledger"], os.O_RDWR | os.O_NOFOLLOW)
    read_fd, write_fd = os.pipe()
    try:
        fcntl.flock(self_locked_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(write_fd)
        write_fd = -1
        consumed_lease_fd, consumed_nonce_fd = self_locked_fd, read_fd
        self_locked_fd = read_fd = -1
        with pytest.raises(CollectorContinuityError, match="nonce pipe length is invalid"):
            writer_factory(
                argv=list(spec.command),
                environ=_child_environment(
                    spec,
                    launch,
                    lease_fd=consumed_lease_fd,
                    nonce_fd=consumed_nonce_fd,
                ),
            )
    finally:
        for descriptor in (write_fd, read_fd, probe_fd, self_locked_fd):
            if descriptor >= 0:
                os.close(descriptor)
    assert provider_calls == []


def test_real_child_handoff_and_exact_nonce_produce_opaque_writer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    writer_factory = _api("open_collector_child_writer_authority")
    begin = _api("_begin_collector_step_attempt")
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        spec = prepared["schedule"][0]
        launch = begin(lease, spec)
        nonce = bytes(launch.nonce)
        assert len(nonce) == 32
        with lease.child_handoff() as handoff:
            child_lease_fd = continuity.os.dup(handoff.fd)
            read_fd, write_fd = continuity.os.pipe()
            continuity.os.write(write_fd, nonce)
            continuity.os.close(write_fd)
            token = writer_factory(
                argv=list(spec.command),
                environ=_child_environment(
                    spec,
                    launch,
                    lease_fd=child_lease_fd,
                    nonce_fd=read_fd,
                ),
            )
        assert token is not None
        assert not isinstance(token, (str, bytes, dict, list))
        _api("require_collector_writer")(
            token,
            database_path=prepared["database"],
            step_id=spec.step_id,
        )
        with pytest.raises((TypeError, CollectorContinuityError, pickle.PickleError)):
            copy.copy(token)
        with pytest.raises((TypeError, CollectorContinuityError, pickle.PickleError)):
            copy.deepcopy(token)
        with pytest.raises((TypeError, CollectorContinuityError, pickle.PickleError)):
            pickle.dumps(token)
        _api("close_collector_writer_authority")(token)


@pytest.mark.parametrize(
    "drift",
    ["attempt_id", "tail", "step", "argv"],
)
def test_writer_token_rejects_active_tail_and_spec_drift_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    writer_factory = _api("open_collector_child_writer_authority")
    begin = _api("_begin_collector_step_attempt")
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = begin(lease, spec)
        with lease.child_handoff() as handoff:
            child_lease_fd = continuity.os.dup(handoff.fd)
            read_fd, write_fd = continuity.os.pipe()
            continuity.os.write(write_fd, bytes(launch.nonce))
            continuity.os.close(write_fd)
            environment = _child_environment(
                spec,
                launch,
                lease_fd=child_lease_fd,
                nonce_fd=read_fd,
                attempt_id="attempt-foreign" if drift == "attempt_id" else None,
            )
            argv = list(spec.command)
            if drift in {"step", "argv"}:
                argv[-1] = "foreign"
            if drift == "tail":
                events = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
                details = dict(events[-1]["event"])
                details["command_sha256"] = "f" * 64
                events[-1]["event"] = details
                events[-1]["event_sha256"] = continuity.canonical_json_sha256(
                    {key: value for key, value in events[-1].items() if key != "event_sha256"}
                )
                prepared["ledger"].write_bytes(
                    b"".join(continuity.canonical_json_bytes(event) + b"\n" for event in events)
                )
            with pytest.raises(CollectorContinuityError):
                writer_factory(argv=argv, environ=environment)


def test_direct_marked_writers_require_token_before_any_sql_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(continuity, "_provider_call", lambda *a, **k: calls.append((a, k)), raising=False)
    direct_writer = _api("open_collector_writer_database")
    with pytest.raises(CollectorContinuityError):
        direct_writer(database_path=prepared["database"], writer_token=None)
    assert calls == []


def test_registered_capture_remains_unavailable_and_provider_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    capture = __import__("stockdata.registered_panel_capture", fromlist=["capture"])
    provider_calls: list[object] = []
    monkeypatch.setattr(capture, "_capture_provider", lambda *a, **k: provider_calls.append((a, k)), raising=False)
    entry = getattr(capture, "capture_registered_panel", None)
    if entry is None:
        entry = getattr(capture, "capture_registered_forward_panel", None)
    if entry is None:
        pytest.fail("missing registered capture entrypoint")
    with pytest.raises(capture.RegisteredPanelCaptureError):
        entry(
            prepared["registration"],
            database=prepared["database"],
            effective_date=prepared["schedule"][0].session,
            phase="pre_open",
        )
    assert provider_calls == []
