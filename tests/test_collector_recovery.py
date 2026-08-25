from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Iterator

import pytest

import stockdata.cache as cache_module
import stockdata.collector_continuity as continuity
import stockdata.sync as sync_module
from stockdata.collector_continuity import CollectorContinuityError
from stockdata.forward_context import CapturedMarketRows, capture_forward_context
from stockdata.forward_corporate_actions import (
    CapturedCorporateActions,
    _source_rows,
    capture_forward_corporate_actions,
)
from test_collector_ledger import _build, _detail, _genesis_event
from test_collector_raw_postcondition import (
    _ACTIONS_SOURCE,
    _CONTEXT_SOURCE,
    _action_batches,
    _context_receipt,
    _context_rows,
    _insert_price_coverage,
    _receipt,
    _seed_actions,
    _seed_context,
    _seed_prices,
)
from test_collector_step_state import (
    _CONTEXT_ALLOWED_TABLES,
    _SESSIONS,
    _SYMBOLS,
    _bound_registration,
    _prepare_collector,
    _schedule,
    _step_state,
)
from test_sync import _tencent_capture


_RECOVERY_NOW = "2099-01-05T16:30:00+08:00"
_RECOVERY_VERIFIER = "stockdata-forward-collector-raw-postcondition/1"
_RECOVERY_KIND = "hot_delete_journal"


def _api(name: str) -> Any:
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing task 2.6 API: {name}")
    return value


@pytest.fixture(autouse=True)
def _forbid_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_provider(*args: object, **kwargs: object) -> object:
        raise AssertionError("collector recovery tests must not call a provider")

    monkeypatch.setattr("stockdata.forward_context.requests.get", no_provider)
    monkeypatch.setattr(
        "stockdata.forward_corporate_actions.fetch_baostock_corporate_actions",
        no_provider,
    )
    monkeypatch.setattr("stockdata.fetch_tencent._http_get", no_provider)


def _history(ledger: Path) -> tuple[dict[str, object], ...]:
    return continuity.parse_collector_ledger(ledger.read_bytes())


def _prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    registration = _bound_registration(database)
    return {
        "database": database,
        "ledger": ledger,
        "registration": registration,
        "schedule": _schedule(database),
    }


@contextmanager
def _writer_cache(
    prepared: dict[str, object], spec: object, lease: object, launch: object
) -> Iterator[object]:
    token = None
    cache = None
    with lease.child_handoff() as handoff:
        lease_fd = os.dup(handoff.fd)
        nonce_fd, nonce_writer = os.pipe()
        try:
            os.write(nonce_writer, bytes(launch.nonce))
        finally:
            os.close(nonce_writer)
        token = continuity.open_collector_child_writer_authority(
            argv=list(spec.command),
            environ=continuity._collector_attempt_child_environment(
                launch, lease_fd=lease_fd, nonce_fd=nonce_fd
            ),
        )
    try:
        cache = continuity.open_collector_writer_database(
            database_path=prepared["database"], writer_token=token
        )
        yield cache
    finally:
        if cache is not None:
            cache.close()
        if token is not None:
            continuity.close_collector_writer_authority(token)
        continuity._clear_nonce(launch._nonce_buffer)
        launch.nonce = b""


def _capture_with_real_writer(cache: object, spec: object, kind: str) -> None:
    if kind == "context":
        rows = _context_rows(include_noncohort=True)
        request, response = _context_receipt(spec, rows=rows)
        captured = CapturedMarketRows(
            rows,
            {
                "observed_at": "2099-01-05T08:40:00+08:00",
                "source": _CONTEXT_SOURCE,
                "request": request,
                "response": response,
            },
        )
        capture_forward_context(
            cache,
            spec.session,
            fetcher=lambda: captured,
            now=datetime.fromisoformat("2099-01-05T09:20:00+08:00"),
        )
        return
    batches = _action_batches()
    receipt = {
        "observed_at": "2099-01-05T08:40:00+08:00",
        "source": _ACTIONS_SOURCE,
        "request": {
            "symbols": list(_SYMBOLS),
            "observation_date": spec.session,
            "years": [int(spec.session[:4]) - 1, int(spec.session[:4])],
        },
        "response": {"symbols": batches},
    }
    captured_actions = CapturedCorporateActions(
        _source_rows(receipt["response"]), receipt
    )
    capture_forward_corporate_actions(
        cache,
        spec.session,
        fetcher=lambda symbols, day: captured_actions,
        now=datetime.fromisoformat("2099-01-05T09:20:00+08:00"),
    )


def _child_begin_then_exit(ledger: Path, spec: object, started_at: str) -> None:
    try:
        with continuity.acquire_collector_phase_lease(ledger) as lease:
            continuity._begin_collector_step_attempt(
                lease, spec, now=lambda: started_at
            )
            os._exit(0)
    except BaseException:
        os._exit(91)


def _child_writer_exit(
    prepared: dict[str, object],
    spec: object,
    *,
    kind: str,
    crash_on_sql: str | None = None,
    committed_prices: int = 0,
) -> None:
    try:
        started_at = (
            "2099-01-05T15:10:00+08:00"
            if kind == "prices"
            else "2099-01-05T08:35:00+08:00"
        )
        with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
            launch = continuity._begin_collector_step_attempt(
                lease, spec, now=lambda: started_at
            )
            with _writer_cache(prepared, spec, lease, launch) as cache:
                cache_module._utc_now = lambda: (
                    "2099-01-05T16:00:00+08:00"
                    if kind == "prices"
                    else "2099-01-05T08:40:00+08:00"
                )
                cache._conn.execute("PRAGMA cache_size=1")
                cache._conn.execute("PRAGMA cache_spill=ON")
                if crash_on_sql is not None:
                    target = crash_on_sql.upper()

                    def crash(statement: str) -> None:
                        if target in statement.upper():
                            os._exit(0)

                    cache._conn.set_trace_callback(crash)
                if kind in {"context", "actions"}:
                    _capture_with_real_writer(cache, spec, kind)
                    os._exit(0 if crash_on_sql is None else 92)

                sync_module.default_final_date = lambda: spec.session
                sync_module.latest_finalized_date = lambda: spec.session
                calls = 0

                def fetch(code: str, start: str, end: str) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == committed_prices + 1:
                        os._exit(0)
                    return _tencent_capture(
                        code,
                        end,
                        observed_at="2099-01-05T16:00:00+08:00",
                        start_date=start,
                    )

                sync_module.sync_symbols(
                    cache,
                    _SYMBOLS,
                    _SESSIONS[0],
                    spec.session,
                    source="tencent",
                    adjustment_mode="raw",
                    adjustment_version="tencent-qt-daily-v1",
                    fetcher=fetch,
                )
                os._exit(93)
    except BaseException:
        os._exit(94)


def _run_forked(target: Callable[..., None], *args: object, **kwargs: object) -> None:
    process = multiprocessing.get_context("fork").Process(
        target=target, args=args, kwargs=kwargs
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("collector crash child hung")
    assert process.exitcode == 0


@contextmanager
def _dangling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    step_index: int = 0,
    setup_before_start: Callable[[object, object], None] | None = None,
    setup_after_start: Callable[[object, object], None] | None = None,
) -> Iterator[tuple[dict[str, object], object, object, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][step_index]
    if setup_before_start is not None:
        with continuity.open_exact_collector_sqlite(
            database_path=prepared["database"],
            ledger_path=prepared["ledger"],
        ) as (connection, _):
            setup_before_start(connection, spec)
            connection.commit()
    begin = _api("_begin_collector_step_attempt")
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = begin(lease, spec)
        if setup_after_start is not None:
            with continuity.open_exact_collector_sqlite(
                database_path=prepared["database"],
                ledger_path=prepared["ledger"],
            ) as (connection, _):
                setup_after_start(connection, spec)
                connection.commit()
        yield prepared, spec, lease, launch


def _recover(lease: object, spec: object) -> object:
    recover = _api("_recover_dangling_collector_attempt")
    return recover(lease, spec, now=lambda: _RECOVERY_NOW)


def _recover_at(lease: object, spec: object, timestamp: str) -> object:
    return _api("_recover_dangling_collector_attempt")(
        lease, spec, now=lambda: timestamp
    )


def _assert_recovered_process_fields(detail: dict[str, object]) -> None:
    assert detail["process_launch_state"] == "indeterminate"
    assert detail["process_result_known"] is False
    assert detail["returncode"] is None
    assert detail["stdout_sha256"] is None
    assert detail["stdout_bytes"] is None
    assert detail["stderr_sha256"] is None
    assert detail["stderr_bytes"] is None
    assert detail["recovered"] is True
    assert detail["verifier_id"] == _RECOVERY_VERIFIER


def _assert_recovery_started_without_terminal(ledger: Path) -> None:
    history = _history(ledger)
    assert history[-1]["event_type"] == "SQLITE_RECOVERY_STARTED"
    assert not any(
        event["event_type"]
        in {
            "SQLITE_RECOVERY_COMPLETED",
            "SQLITE_RECOVERY_FAILED",
            "ATTEMPT_COMPLETED",
            "ATTEMPT_FAILED",
        }
        for event in history
    )


def _recovery_started_detail(
    started: dict[str, object], *, journal_path: Path, attempt_suffix: str = ""
) -> dict[str, object]:
    detail = started["event"]
    assert isinstance(detail, dict)
    return {
        "registration_sha256": detail["registration_sha256"],
        "database_uuid": detail["database_uuid"],
        "state_before_sha256": detail["state_before_sha256"],
        "attempt_id": detail["attempt_id"],
        "attempt_started_event_sha256": started["event_sha256"],
        "recovery_id": hashlib.sha256(
            ("recovery-0001" + attempt_suffix).encode("ascii")
        ).hexdigest(),
        "recovery_kind": _RECOVERY_KIND,
        "journal_identity": continuity.PhysicalFileIdentity(
            str(journal_path.resolve()), 1, 2, 1, 5
        ).to_dict(),
        "journal_bytes": 4096,
        "journal_sha256": "e" * 64,
        "started_at": "2099-01-05T16:20:00+08:00" + attempt_suffix,
    }


def _recovery_terminal_detail(
    recovery_started: dict[str, object],
    *,
    event_type: str,
    classification: str,
) -> dict[str, object]:
    start = recovery_started["event"]
    assert isinstance(start, dict)
    state = _step_state(_CONTEXT_ALLOWED_TABLES)
    detail = {
        "registration_sha256": start["registration_sha256"],
        "database_uuid": start["database_uuid"],
        "state_before_sha256": start["state_before_sha256"],
        "attempt_id": start["attempt_id"],
        "attempt_started_event_sha256": start["attempt_started_event_sha256"],
        "recovery_id": start["recovery_id"],
        "recovery_kind": start["recovery_kind"],
        "journal_identity": start["journal_identity"],
        "journal_bytes": start["journal_bytes"],
        "journal_sha256": start["journal_sha256"],
        "started_at": start["started_at"],
        "recovery_started_event_sha256": recovery_started["event_sha256"],
        "state_after_sha256": state["collector_state_sha256"],
        "step_state_after": state,
    }
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        detail.update(
            {
                "completed_at": _RECOVERY_NOW,
                "recovery_classification": classification,
            }
        )
    else:
        detail.update(
            {
                "failed_at": _RECOVERY_NOW,
                "failure_classification": classification,
                "retryable": False,
            }
        )
    return detail


def _recovered_attempt_detail(
    started: dict[str, object], *, event_type: str = "ATTEMPT_FAILED"
) -> dict[str, object]:
    detail = deepcopy(started["event"])
    assert isinstance(detail, dict)
    detail.pop("lease_nonce_sha256")
    state = _step_state(_CONTEXT_ALLOWED_TABLES)
    detail.update(
        {
            "state_after_sha256": state["collector_state_sha256"],
            "step_state_after": state,
            "process_result_known": False,
            "process_launch_state": "indeterminate",
            "returncode": None,
            "stdout_sha256": None,
            "stdout_bytes": None,
            "stderr_sha256": None,
            "stderr_bytes": None,
            "recovered": True,
            "verifier_id": _RECOVERY_VERIFIER,
        }
    )
    if event_type == "ATTEMPT_COMPLETED":
        detail["completed_at"] = _RECOVERY_NOW
    else:
        detail.update(
            {
                "failed_at": _RECOVERY_NOW,
                "failure_classification": "interrupted_no_commit",
                "retryable": True,
            }
        )
    return detail


def test_recovery_started_has_exact_frozen_fields(tmp_path: Path) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED", _detail("ATTEMPT_STARTED"))
    detail = _recovery_started_detail(
        started, journal_path=tmp_path / "evidence.sqlite-journal"
    )

    event = continuity.build_collector_ledger_event(
        previous_event=started,
        event_type="SQLITE_RECOVERY_STARTED",
        event=detail,
    )

    assert set(event["event"]) == {
        "registration_sha256",
        "database_uuid",
        "state_before_sha256",
        "attempt_id",
        "attempt_started_event_sha256",
        "recovery_id",
        "recovery_kind",
        "journal_identity",
        "journal_bytes",
        "journal_sha256",
        "started_at",
    }
    assert event["event"]["recovery_kind"] == _RECOVERY_KIND
    assert event["event"]["attempt_started_event_sha256"] == started["event_sha256"]


@pytest.mark.parametrize(
    ("event_type", "classification"),
    [
        ("SQLITE_RECOVERY_COMPLETED", "hot_delete_journal_recovered"),
        ("SQLITE_RECOVERY_FAILED", "rollback_journal_recovery_failed"),
    ],
)
def test_recovery_terminal_binds_start_and_complete_after_state(
    tmp_path: Path, event_type: str, classification: str
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED", _detail("ATTEMPT_STARTED"))
    recovery_started = _build(
        (genesis, registration, started),
        "SQLITE_RECOVERY_STARTED",
        _recovery_started_detail(
            started, journal_path=tmp_path / "evidence.sqlite-journal"
        ),
    )
    detail = _recovery_terminal_detail(
        recovery_started, event_type=event_type, classification=classification
    )

    terminal = continuity.build_collector_ledger_event(
        previous_event=recovery_started,
        event_type=event_type,
        event=detail,
    )

    assert terminal["event"]["recovery_started_event_sha256"] == recovery_started["event_sha256"]
    assert terminal["event"]["attempt_started_event_sha256"] == started["event_sha256"]
    assert terminal["event"]["state_after_sha256"] == terminal["event"]["step_state_after"]["collector_state_sha256"]
    if event_type.endswith("COMPLETED"):
        assert terminal["event"]["recovery_classification"] == "hot_delete_journal_recovered"
    else:
        assert terminal["event"]["failure_classification"] == "rollback_journal_recovery_failed"
        assert terminal["event"]["retryable"] is False


def test_recovery_failed_cannot_be_followed_by_recovered_attempt_completion(
    tmp_path: Path,
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED", _detail("ATTEMPT_STARTED"))
    recovery_started = _build(
        (genesis, registration, started),
        "SQLITE_RECOVERY_STARTED",
        _recovery_started_detail(
            started, journal_path=tmp_path / "evidence.sqlite-journal"
        ),
    )
    recovery_failed = _build(
        (genesis, registration, started, recovery_started),
        "SQLITE_RECOVERY_FAILED",
        _recovery_terminal_detail(
            recovery_started,
            event_type="SQLITE_RECOVERY_FAILED",
            classification="rollback_journal_recovery_failed",
        ),
    )
    completed = _recovered_attempt_detail(started, event_type="ATTEMPT_COMPLETED")
    candidate = continuity.build_collector_ledger_event(
        previous_event=recovery_failed,
        event_type="ATTEMPT_COMPLETED",
        event=completed,
    )
    with pytest.raises(CollectorContinuityError):
        continuity.parse_collector_ledger(
            b"".join(
                continuity.canonical_json_bytes(event) + b"\n"
                for event in (
                    genesis,
                    registration,
                    started,
                    recovery_started,
                    recovery_failed,
                    candidate,
                )
            )
        )


@pytest.mark.parametrize(
    "mutation",
    ["known-process", "wrong-verifier", "non-null-stdout"],
)
def test_recovered_attempt_terminal_requires_fixed_indeterminate_process_state(
    tmp_path: Path, mutation: str
) -> None:
    genesis = _genesis_event(tmp_path)
    registration = _build((genesis,), "REGISTRATION_BOUND")
    started = _build((genesis, registration), "ATTEMPT_STARTED", _detail("ATTEMPT_STARTED"))
    detail = _recovered_attempt_detail(started)
    if mutation == "known-process":
        detail["process_result_known"] = True
        detail["process_launch_state"] = "handle_obtained"
        detail["returncode"] = 0
        detail["stdout_sha256"] = "3" * 64
        detail["stdout_bytes"] = 0
        detail["stderr_sha256"] = "4" * 64
        detail["stderr_bytes"] = 0
    elif mutation == "wrong-verifier":
        detail["verifier_id"] = "raw-postcondition-v1"
    else:
        detail["stdout_sha256"] = "3" * 64
        detail["stdout_bytes"] = 0
    with pytest.raises(CollectorContinuityError):
        continuity.build_collector_ledger_event(
            previous_event=started,
            event_type="ATTEMPT_FAILED",
            event=detail,
        )


def test_unchanged_dangling_attempt_is_retryable_and_preserves_auditable_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, launch):
        old_nonce = bytes(launch.nonce)
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_FAILED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["failure_classification"] == "interrupted_no_commit"
        assert detail["retryable"] is True
        _assert_recovered_process_fields(detail)

        retry = _api("_begin_collector_step_attempt")(lease, spec)
        assert retry.attempt_id != launch.attempt_id
        assert retry.nonce_sha256 != launch.nonce_sha256

        with lease.child_handoff() as handoff:
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, old_nonce)
            finally:
                os.close(write_fd)
            environment = continuity._collector_attempt_child_environment(
                retry, lease_fd=handoff.fd, nonce_fd=read_fd
            )
            with pytest.raises(
                CollectorContinuityError,
                match="nonce does not match active attempt",
            ):
                continuity.open_collector_child_writer_authority(
                    argv=list(spec.command), environ=environment
                )


def test_deleted_terminal_leaving_start_reenters_dangling_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = _api("_begin_collector_step_attempt")(
            lease, spec, now=lambda: "2099-01-05T08:35:00+08:00"
        )
        _recover_at(lease, spec, "2099-01-05T08:36:00+08:00")
        history = _history(prepared["ledger"])
        assert history[-2]["event_type"] == "ATTEMPT_STARTED"
        assert history[-1]["event_type"] == "ATTEMPT_FAILED"
        continuity._clear_nonce(launch._nonce_buffer)
        launch.nonce = b""

    prepared["ledger"].write_bytes(
        b"".join(
            continuity.canonical_json_bytes(event) + b"\n"
            for event in history[:-1]
        )
    )
    assert _history(prepared["ledger"])[-1]["event_type"] == "ATTEMPT_STARTED"

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = _recover_at(lease, spec, "2099-01-05T08:37:00+08:00")
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "interrupted_no_commit"
    assert outcome.attempt_id == launch.attempt_id


def test_process_exit_after_durable_start_recovers_under_new_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    _run_forked(
        _child_begin_then_exit,
        prepared["ledger"],
        spec,
        "2099-01-05T08:35:00+08:00",
    )
    started = _history(prepared["ledger"])[-1]
    assert started["event_type"] == "ATTEMPT_STARTED"

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = _recover_at(lease, spec, "2099-01-05T08:36:00+08:00")
    assert outcome.attempt_id == started["event"]["attempt_id"]
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "interrupted_no_commit"


@pytest.mark.parametrize(
    ("kind", "step_index", "target_sql"),
    [
        ("context", 0, "INSERT INTO forward_universe_observations"),
        ("actions", 1, "INSERT INTO forward_corporate_actions"),
    ],
)
def test_real_writer_transaction_crash_recovers_atomically_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    step_index: int,
    target_sql: str,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][step_index]
    _run_forked(
        _child_writer_exit,
        prepared,
        spec,
        kind=kind,
        crash_on_sql=target_sql,
    )
    assert _history(prepared["ledger"])[-1]["event_type"] == "ATTEMPT_STARTED"
    journal = Path(str(prepared["database"]) + "-journal")
    assert journal.is_file()
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    assert not Path(str(prepared["database"]) + "-wal").exists()
    assert not Path(str(prepared["database"]) + "-shm").exists()
    original_unlink = os.unlink
    original_remove = os.remove

    def reject_manual_journal_delete(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> object:
        if Path(os.fsdecode(path)) == journal:
            raise AssertionError("recovery must not manually delete the SQLite journal")
        function = original_remove if kwargs.pop("_remove", False) else original_unlink
        return function(path, *args, **kwargs)

    monkeypatch.setattr(continuity.os, "unlink", reject_manual_journal_delete)

    def reject_manual_journal_remove(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> object:
        return reject_manual_journal_delete(path, *args, _remove=True, **kwargs)

    monkeypatch.setattr(continuity.os, "remove", reject_manual_journal_remove)

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        lease.verify()
        outcome = _recover(lease, spec)
    assert not journal.exists()
    assert not Path(str(prepared["database"]) + "-wal").exists()
    assert not Path(str(prepared["database"]) + "-shm").exists()
    assert outcome.raw_class == "unchanged"
    assert outcome.terminal_event_type == "ATTEMPT_FAILED"
    assert outcome.classification == "interrupted_no_commit"
    assert outcome.retryable is True
    terminal = _history(prepared["ledger"])[-1]
    assert terminal["event_type"] == "ATTEMPT_FAILED"
    assert terminal["event"]["failure_classification"] == "interrupted_no_commit"
    assert terminal["event"]["retryable"] is True


@pytest.mark.parametrize(("kind", "step_index"), [("context", 0), ("actions", 1)])
def test_real_writer_commit_then_child_exit_recovers_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    step_index: int,
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][step_index]
    _run_forked(_child_writer_exit, prepared, spec, kind=kind)
    assert _history(prepared["ledger"])[-1]["event_type"] == "ATTEMPT_STARTED"

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        outcome = _recover(lease, spec)
    assert outcome.raw_class == "complete"
    assert outcome.terminal_event_type == "ATTEMPT_COMPLETED"
    assert outcome.classification == "complete"


def test_deleted_committed_attempt_pair_is_rejected_before_spawn_or_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = _api("_begin_collector_step_attempt")(
            lease, spec, now=lambda: "2099-01-05T08:30:00+08:00"
        )
        _recover_at(lease, spec, "2099-01-05T08:31:00+08:00")
        continuity._clear_nonce(launch._nonce_buffer)
        launch.nonce = b""

    _run_forked(_child_writer_exit, prepared, spec, kind="context")
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        committed = _recover(lease, spec)
    assert committed.terminal_event_type == "ATTEMPT_COMPLETED"
    history = _history(prepared["ledger"])
    assert [event["event_type"] for event in history[-2:]] == [
        "ATTEMPT_STARTED",
        "ATTEMPT_COMPLETED",
    ]

    prepared["ledger"].write_bytes(
        b"".join(
            continuity.canonical_json_bytes(event) + b"\n"
            for event in history[:-2]
        )
    )
    before = prepared["ledger"].read_bytes()
    popen_calls: list[object] = []

    def forbidden_popen(*args: object, **kwargs: object) -> object:
        popen_calls.append((args, kwargs))
        raise AssertionError("drifted committed state must not spawn")

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        with pytest.raises(CollectorContinuityError, match="ledger state drifted"):
            _api("_execute_collector_step_attempt")(
                lease, spec, popen_factory=forbidden_popen
            )
    assert popen_calls == []
    assert prepared["ledger"].read_bytes() == before


def test_real_price_commits_recover_partial_and_retry_fetches_only_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed_count = 2
    prepared = _prepared(tmp_path, monkeypatch)
    spec = prepared["schedule"][3]
    _run_forked(
        _child_writer_exit,
        prepared,
        spec,
        kind="prices",
        committed_prices=committed_count,
    )

    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        partial = _recover_at(lease, spec, "2099-01-05T16:05:00+08:00")
        assert partial.raw_class == "partial_prices"
        assert partial.classification == "interrupted_partial_prices"
        assert partial.retryable is True

        retry = _api("_begin_collector_step_attempt")(
            lease, spec, now=lambda: "2099-01-05T16:06:00+08:00"
        )
        sync_module.default_final_date = lambda: spec.session
        sync_module.latest_finalized_date = lambda: spec.session
        monkeypatch.setattr(
            cache_module,
            "_utc_now",
            lambda: "2099-01-05T16:10:00+08:00",
        )
        calls: list[tuple[str, str, str]] = []

        def fetch(code: str, start: str, end: str) -> object:
            calls.append((code, start, end))
            return _tencent_capture(
                code,
                end,
                observed_at="2099-01-05T16:10:00+08:00",
                start_date=start,
            )

        with _writer_cache(prepared, spec, lease, retry) as cache:
            result = sync_module.sync_symbols(
                cache,
                _SYMBOLS,
                _SESSIONS[0],
                spec.session,
                source="tencent",
                adjustment_mode="raw",
                adjustment_version="tencent-qt-daily-v1",
                fetcher=fetch,
            )
        assert result["errors"] == 0
        assert [code for code, _start, _end in calls] == list(
            _SYMBOLS[committed_count:]
        )
        completed = _recover(lease, spec)

    assert completed.raw_class == "complete"
    assert completed.terminal_event_type == "ATTEMPT_COMPLETED"


def test_exact_context_state_is_recovered_as_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(connection: object, spec: object) -> None:
        _seed_context(connection, spec, include_noncohort=True)

    with _dangling(tmp_path, monkeypatch, setup_after_start=seed) as (
        prepared,
        spec,
        lease,
        _launch,
    ):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_COMPLETED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["recovered"] is True
        assert detail["verifier_id"] == _RECOVERY_VERIFIER
        _assert_recovered_process_fields(detail)


def test_exact_corporate_action_state_is_recovered_as_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(connection: object, spec: object) -> None:
        _seed_actions(connection, spec)

    with _dangling(
        tmp_path, monkeypatch, step_index=1, setup_after_start=seed
    ) as (prepared, spec, lease, _launch):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_COMPLETED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["recovered"] is True
        _assert_recovered_process_fields(detail)


@pytest.mark.parametrize("count", [1, 5, 11])
def test_monotonic_price_subset_is_retryable_and_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    def seed(connection: object, spec: object) -> None:
        _seed_prices(connection, spec, symbols=_SYMBOLS[:count], include_coverage=True)

    with _dangling(
        tmp_path, monkeypatch, step_index=3, setup_after_start=seed
    ) as (prepared, spec, lease, _launch):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_FAILED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["failure_classification"] == "interrupted_partial_prices"
        assert detail["retryable"] is True
        assert detail["recovered"] is True
        assert detail["process_result_known"] is False


def test_complete_price_state_is_recovered_as_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(connection: object, spec: object) -> None:
        _seed_prices(connection, spec, symbols=_SYMBOLS, include_coverage=True)

    with _dangling(
        tmp_path, monkeypatch, step_index=3, setup_after_start=seed
    ) as (prepared, spec, lease, _launch):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_COMPLETED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        _assert_recovered_process_fields(detail)


def test_coverage_only_dangling_price_attempt_is_quarantined_before_retry_nonce_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(connection: object, spec: object) -> None:
        _insert_price_coverage(connection, spec)

    with _dangling(
        tmp_path, monkeypatch, step_index=3, setup_after_start=seed
    ) as (prepared, spec, lease, _launch):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_FAILED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["failure_classification"] == "forbidden_drift"
        assert detail["retryable"] is False

        before = prepared["ledger"].read_bytes()

        def forbidden_retry_side_effect(*args: object, **kwargs: object) -> object:
            raise AssertionError("quarantined retry reached nonce allocation or ledger write")

        monkeypatch.setattr(continuity.secrets, "token_bytes", forbidden_retry_side_effect)
        monkeypatch.setattr(continuity.secrets, "token_hex", forbidden_retry_side_effect)
        monkeypatch.setattr(
            continuity, "_append_collector_phase_event", forbidden_retry_side_effect
        )
        with pytest.raises(CollectorContinuityError, match="quarantined"):
            _api("_begin_collector_step_attempt")(lease, spec)
        assert prepared["ledger"].read_bytes() == before


@pytest.mark.parametrize("mutation", ["foreign_receipt", "orphan_receipt"])
def test_foreign_or_orphan_receipt_is_permanent_forbidden_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    def seed(connection: object, spec: object) -> None:
        _receipt(
            connection,
            receipt_id=1,
            observed_at="2099-01-05T16:00:00+08:00",
            source="foreign-source" if mutation == "foreign_receipt" else "tencent",
            request={"foreign": True},
            response={"foreign": True},
        )

    with _dangling(
        tmp_path, monkeypatch, step_index=3, setup_after_start=seed
    ) as (prepared, spec, lease, _launch):
        _recover(lease, spec)
        terminal = _history(prepared["ledger"])[-1]
        assert terminal["event_type"] == "ATTEMPT_FAILED"
        detail = terminal["event"]
        assert isinstance(detail, dict)
        assert detail["failure_classification"] == "forbidden_drift"
        assert detail["retryable"] is False


def _leave_hot_delete_journal(database: Path) -> Path:
    code = (
        "import hashlib, os, sqlite3, sys\n"
        "db = sqlite3.connect(sys.argv[1])\n"
        "db.execute('PRAGMA journal_mode=DELETE')\n"
        "db.execute('PRAGMA cache_size=1')\n"
        "db.execute('PRAGMA cache_spill=ON')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "payload = 'x' * 3072\n"
        "for index in range(100):\n"
        "    request = '{\"index\":%d}' % index\n"
        "    response = '{\"index\":%d,\"payload\":\"%s\"}' % (index, payload)\n"
        "    db.execute(\"INSERT INTO collection_receipts "
        "(observed_at,source,request_json,response_json,response_sha256,created_at) "
        "VALUES (?,?,?,?,?,?)\", "
        "('2099-01-05T16:00:00+08:00', 'tencent', request, response, "
        "hashlib.sha256(response.encode('ascii')).hexdigest(), "
        "'2099-01-05T16:00:00+08:00'))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(database)], check=True)
    hot_journal = Path(str(database) + "-journal")
    assert hot_journal.exists()
    payload = hot_journal.read_bytes()
    assert payload[:8] == bytes.fromhex("d9d505f920a163d7")
    return hot_journal


def test_hot_delete_journal_recovery_is_durable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        journal = _leave_hot_delete_journal(prepared["database"])
        original_unlink = os.unlink
        original_remove = os.remove

        def reject_manual_journal_delete(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> object:
            if Path(os.fsdecode(path)) == journal:
                raise AssertionError("recovery must not manually delete the SQLite journal")
            function = original_remove if kwargs.pop("_remove", False) else original_unlink
            return function(path, *args, **kwargs)

        monkeypatch.setattr(continuity.os, "unlink", reject_manual_journal_delete)

        def reject_manual_journal_remove(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> object:
            return reject_manual_journal_delete(path, *args, _remove=True, **kwargs)

        monkeypatch.setattr(continuity.os, "remove", reject_manual_journal_remove)
        _recover(lease, spec)
        assert not journal.exists()
        first_history = _history(prepared["ledger"])
        recovery_terminals = [
            event for event in first_history if event["event_type"].startswith("SQLITE_RECOVERY_")
        ]
        assert len(recovery_terminals) == 2
        first_count = len(first_history)
        with pytest.raises(CollectorContinuityError):
            _recover(lease, spec)
        assert len(_history(prepared["ledger"])) == first_count


def test_journal_remaining_after_controlled_sqlite_rollback_hard_blocks_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        journal = _leave_hot_delete_journal(prepared["database"])
        original_payload = journal.read_bytes()
        rollback = continuity._trigger_exact_delete_journal_rollback

        def leave_replacement_after_rollback(*args: object, **kwargs: object) -> None:
            rollback(*args, **kwargs)
            assert not journal.exists()
            journal.write_bytes(original_payload)

        monkeypatch.setattr(
            continuity,
            "_trigger_exact_delete_journal_rollback",
            leave_replacement_after_rollback,
        )
        with pytest.raises(CollectorContinuityError):
            _recover(lease, spec)
        _assert_recovery_started_without_terminal(prepared["ledger"])
        assert journal.is_file()
        assert journal.read_bytes() == original_payload


@pytest.mark.parametrize(
    "mutation", ["same_inode_content", "regular_replacement", "symlink", "directory"]
)
def test_journal_drift_after_observation_hard_blocks_without_deleting_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        journal = _leave_hot_delete_journal(prepared["database"])
        observed_payload = journal.read_bytes()
        observed_inode = os.lstat(journal).st_ino
        replacement_payload = b"replacement-journal"
        target = tmp_path / "replacement-target"
        append = continuity._append_collector_phase_event
        mutated = False

        def mutate_after_recovery_start(*args: object, **kwargs: object) -> object:
            nonlocal mutated
            event = append(*args, **kwargs)
            if kwargs.get("event_type") == "SQLITE_RECOVERY_STARTED" and not mutated:
                mutated = True
                if mutation == "same_inode_content":
                    changed = bytearray(observed_payload)
                    changed[-1] ^= 1
                    with journal.open("r+b") as stream:
                        stream.write(changed)
                        stream.flush()
                        os.fsync(stream.fileno())
                elif mutation == "regular_replacement":
                    target.write_bytes(replacement_payload)
                    os.replace(target, journal)
                elif mutation == "symlink":
                    target.write_bytes(replacement_payload)
                    journal.unlink()
                    journal.symlink_to(target)
                else:
                    journal.unlink()
                    journal.mkdir()
            return event

        monkeypatch.setattr(
            continuity, "_append_collector_phase_event", mutate_after_recovery_start
        )
        with pytest.raises(CollectorContinuityError):
            _recover(lease, spec)
        _assert_recovery_started_without_terminal(prepared["ledger"])
        if mutation == "same_inode_content":
            assert os.lstat(journal).st_ino == observed_inode
            assert journal.read_bytes() != observed_payload
        elif mutation == "regular_replacement":
            assert journal.is_file()
            assert os.lstat(journal).st_ino != observed_inode
            assert journal.read_bytes() == replacement_payload
        elif mutation == "symlink":
            assert journal.is_symlink()
            assert target.read_bytes() == replacement_payload
        else:
            assert journal.is_dir()


@pytest.mark.parametrize("replacement_after_crash", [False, True])
def test_crash_after_sqlite_rollback_replays_only_without_a_new_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_after_crash: bool,
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        journal = _leave_hot_delete_journal(prepared["database"])
        replacement_payload = b"post-rollback-replacement"
        rollback = continuity._trigger_exact_delete_journal_rollback

        def crash_after_rollback(*args: object, **kwargs: object) -> None:
            rollback(*args, **kwargs)
            assert not journal.exists()
            if replacement_after_crash:
                journal.write_bytes(replacement_payload)
            raise OSError("injected crash after SQLite rollback")

        monkeypatch.setattr(
            continuity,
            "_trigger_exact_delete_journal_rollback",
            crash_after_rollback,
        )
        with pytest.raises(OSError, match="injected crash after SQLite rollback"):
            _recover(lease, spec)
        _assert_recovery_started_without_terminal(prepared["ledger"])

        monkeypatch.setattr(
            continuity, "_trigger_exact_delete_journal_rollback", rollback
        )
        if replacement_after_crash:
            with pytest.raises(CollectorContinuityError):
                _recover(lease, spec)
            _assert_recovery_started_without_terminal(prepared["ledger"])
            assert journal.read_bytes() == replacement_payload
        else:
            _recover(lease, spec)
            history = _history(prepared["ledger"])
            assert sum(
                event["event_type"] == "SQLITE_RECOVERY_COMPLETED"
                for event in history
            ) == 1
            assert history[-1]["event_type"] == "ATTEMPT_FAILED"


def test_recovery_start_append_interruption_leaves_only_the_attempt_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        _leave_hot_delete_journal(prepared["database"])
        original = continuity._append_collector_phase_event
        failed_once = False

        def fail_recovery_start(*args: object, **kwargs: object) -> object:
            nonlocal failed_once
            if kwargs.get("event_type") == "SQLITE_RECOVERY_STARTED" and not failed_once:
                failed_once = True
                original(*args, **kwargs)
                raise OSError("injected recovery-start post-fsync interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(continuity, "_append_collector_phase_event", fail_recovery_start)
        try:
            _recover(lease, spec)
        except CollectorContinuityError:
            pass
        assert _history(prepared["ledger"])[-1]["event_type"] in {
            "ATTEMPT_STARTED",
            "SQLITE_RECOVERY_STARTED",
        }
        monkeypatch.setattr(continuity, "_append_collector_phase_event", original)
        if _history(prepared["ledger"])[-1]["event_type"] == "ATTEMPT_STARTED":
            _recover(lease, spec)
        history = _history(prepared["ledger"])
        assert sum(event["event_type"] == "SQLITE_RECOVERY_STARTED" for event in history) == 1


def test_recovery_terminal_append_interruption_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        _leave_hot_delete_journal(prepared["database"])
        original = continuity._append_collector_phase_event
        failed_once = False

        def fail_recovery_terminal(*args: object, **kwargs: object) -> object:
            nonlocal failed_once
            if kwargs.get("event_type") == "SQLITE_RECOVERY_COMPLETED" and not failed_once:
                failed_once = True
                original(*args, **kwargs)
                raise OSError("injected recovery-terminal post-fsync interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(continuity, "_append_collector_phase_event", fail_recovery_terminal)
        try:
            _recover(lease, spec)
        except CollectorContinuityError:
            pass
        monkeypatch.setattr(continuity, "_append_collector_phase_event", original)
        if _history(prepared["ledger"])[-1]["event_type"] in {
            "ATTEMPT_STARTED",
            "SQLITE_RECOVERY_STARTED",
            "SQLITE_RECOVERY_COMPLETED",
        }:
            try:
                _recover(lease, spec)
            except CollectorContinuityError:
                pass
        history = _history(prepared["ledger"])
        assert sum(event["event_type"] == "SQLITE_RECOVERY_STARTED" for event in history) == 1
        assert sum(event["event_type"] == "SQLITE_RECOVERY_COMPLETED" for event in history) == 1


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal-symlink", "-journal-directory"])
def test_sidecar_or_journal_locator_loss_never_fakes_recovery_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: str
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        path = Path(str(prepared["database"]) + sidecar.replace("-journal-symlink", "-journal").replace("-journal-directory", "-journal"))
        if sidecar.endswith("symlink"):
            target = tmp_path / "journal-target"
            target.write_bytes(b"journal")
            path.symlink_to(target)
        elif sidecar.endswith("directory"):
            path.mkdir()
        else:
            path.write_bytes(b"sidecar")
        before = prepared["ledger"].read_bytes()
        with pytest.raises(CollectorContinuityError):
            _recover(lease, spec)
        assert prepared["ledger"].read_bytes() == before


@pytest.mark.parametrize("authority", ["database", "ledger"])
def test_authority_path_loss_never_appends_a_fake_recovery_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        path = Path(prepared[authority])
        moved = path.with_name(path.name + ".moved")
        os.replace(path, moved)
        before = moved.read_bytes()
        with pytest.raises(CollectorContinuityError):
            _recover(lease, spec)
        ledger_path = moved if authority == "ledger" else Path(prepared["ledger"])
        assert _history(ledger_path)[-1]["event_type"] == "ATTEMPT_STARTED"
        assert moved.read_bytes() == before


def test_terminal_append_retry_does_not_duplicate_recovery_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _dangling(tmp_path, monkeypatch) as (prepared, spec, lease, _launch):
        original = continuity._append_collector_phase_event
        failed_once = False

        def fail_once(*args: object, **kwargs: object) -> object:
            nonlocal failed_once
            if kwargs.get("event_type") == "ATTEMPT_FAILED" and not failed_once:
                failed_once = True
                raise OSError("injected terminal append interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(continuity, "_append_collector_phase_event", fail_once)
        try:
            _recover(lease, spec)
        except CollectorContinuityError:
            pass
        monkeypatch.setattr(continuity, "_append_collector_phase_event", original)
        if _history(prepared["ledger"])[-1]["event_type"] == "ATTEMPT_STARTED":
            _recover(lease, spec)
        history = _history(prepared["ledger"])
        assert sum(event["event_type"] == "ATTEMPT_FAILED" for event in history) == 1


def test_competing_phase_lease_is_rejected_before_recovery_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        with pytest.raises(CollectorContinuityError):
            continuity.acquire_collector_phase_lease(prepared["ledger"])
