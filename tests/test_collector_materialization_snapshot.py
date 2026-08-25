from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
from typing import Callable

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import CollectorContinuityError
from stockdata.rqgm_provider_contract import DATABASE_SCHEMA
from test_collector_attempt_protocol import _prepared
from test_collector_phase_orchestration import _append_completed_attempt
from test_collector_recovery import _leave_hot_delete_journal, _recover


_CLOSURE_FIELDS = {
    "schema_version",
    "live_database_identity",
    "live_ledger_identity",
    "database_uuid",
    "registration_sha256",
    "ledger_head",
    "logical_state",
    "snapshot_database_reference",
}
_REFERENCE_FIELDS = {"kind", "identifier", "schema_version"}


def _snapshot_api() -> Callable[..., object]:
    value = getattr(
        continuity, "create_registered_collector_materialization_snapshot", None
    )
    if not callable(value):
        pytest.fail(
            "missing task 4.1 API: create_registered_collector_materialization_snapshot"
        )
    return value


def _materialize(
    prepared: dict[str, object], output_dir: Path
) -> dict[str, object]:
    return _snapshot_api()(
        registration_file=prepared["registration"],
        database=prepared["database"],
        staging_directory=output_dir,
    )


def _collector(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    directory.mkdir(parents=True)
    return _prepared(directory, monkeypatch)


def _staging_parent(directory: Path) -> Path:
    parent = directory / "snapshots"
    parent.mkdir()
    return parent


def _complete(prepared: dict[str, object], count: int = 12) -> None:
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        for spec in prepared["schedule"][:count]:
            _append_completed_attempt(lease, spec)


def _append_failed(prepared: dict[str, object], *, quarantined: bool) -> None:
    spec = prepared["schedule"][0]
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = continuity._begin_collector_step_attempt(
            lease, spec, now=lambda: "2099-01-05T08:45:00+08:00"
        )
        try:
            raw_class = "forbidden" if quarantined else "unchanged"
            classification = "forbidden_drift" if quarantined else "child_no_commit"
            raw = continuity._raw_result(
                raw_class, classification, launch.baseline.step_state
            )
            empty_sha256 = hashlib.sha256(b"").hexdigest()
            process = continuity._CollectorProcessResult(
                True,
                0 if quarantined else 1,
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
                failure_classification=classification,
            )
            continuity._append_terminal_once(
                lease, launch, event_type=event_type, event=detail
            )
        finally:
            continuity._clear_nonce(launch._nonce_buffer)
            launch.nonce = b""


def _append_dangling(prepared: dict[str, object]) -> None:
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        launch = continuity._begin_collector_step_attempt(
            lease,
            prepared["schedule"][0],
            now=lambda: "2099-01-05T08:45:00+08:00",
        )
        continuity._clear_nonce(launch._nonce_buffer)
        launch.nonce = b""


def _append_duplicate_after_complete(prepared: dict[str, object]) -> None:
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        history = continuity._phase_ledger_history(lease)
        started_detail = deepcopy(history[2]["event"])
        started_detail["attempt_id"] = "d" * 64
        started_detail["lease_nonce_sha256"] = "e" * 64
        started = continuity._append_collector_phase_event_once(
            lease,
            predecessor_event_sha256=history[-1]["event_sha256"],
            event_type="ATTEMPT_STARTED",
            event=started_detail,
        )
        terminal_detail = deepcopy(history[3]["event"])
        terminal_detail["attempt_id"] = started_detail["attempt_id"]
        continuity._append_collector_phase_event_once(
            lease,
            predecessor_event_sha256=started["event_sha256"],
            event_type="ATTEMPT_COMPLETED",
            event=terminal_detail,
        )


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    paths = [root, *sorted(root.rglob("*"))]
    result: list[tuple[object, ...]] = []
    for path in paths:
        status = path.lstat()
        payload_sha256 = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        result.append(
            (
                str(path.relative_to(root)),
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_size,
                payload_sha256,
            )
        )
    return tuple(result)


def _backup_spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    before: Callable[[sqlite3.Connection], None] | None = None,
    after: Callable[[sqlite3.Connection], None] | None = None,
    failure: BaseException | None = None,
) -> list[Path]:
    original_connect = sqlite3.connect
    calls: list[Path] = []

    def connect(*args, **kwargs):
        base_factory = kwargs.get("factory", sqlite3.Connection)

        class TrackedConnection(base_factory):
            def backup(self, target, *backup_args, **backup_kwargs):
                row = target.execute("PRAGMA database_list").fetchone()
                calls.append(Path(str(row[2])))
                if before is not None:
                    before(target)
                if failure is not None:
                    raise failure
                result = super().backup(target, *backup_args, **backup_kwargs)
                if after is not None:
                    after(target)
                return result

        kwargs["factory"] = TrackedConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return calls


def _paths(result: dict[str, object]) -> dict[str, Path]:
    return {
        "database": Path(result["database"]["path"]),
        "registration": Path(result["registration"]["path"]),
        "ledger": Path(result["ledger"]["path"]),
        "closure": Path(result["continuity_closure"]["path"]),
    }


def _closure(result: dict[str, object]) -> dict[str, object]:
    raw = _paths(result)["closure"].read_bytes()
    value = json.loads(raw.decode("ascii"))
    assert raw == continuity.canonical_json_bytes(value)
    return value


def _verifier_api() -> Callable[..., object]:
    value = getattr(
        continuity, "verify_registered_collector_materialization_snapshot", None
    )
    if not callable(value):
        pytest.fail(
            "missing task 4.4 API: verify_registered_collector_materialization_snapshot"
        )
    return value


def _verify_result(
    result: dict[str, object],
    *,
    registration_raw: bytes | None = None,
    ledger_raw: bytes | None = None,
    closure_raw: bytes | None = None,
    database_path: Path | None = None,
    database_reference: dict[str, object] | None = None,
    exact_panel_raw: bytes | None = None,
) -> None:
    paths = _paths(result)
    registered = json.loads(paths["registration"].read_text(encoding="ascii"))
    exact_panel_raw = (
        continuity.canonical_json_bytes(
            sorted(
                f"{symbol}@{session}"
                for symbol in registered["symbols"]
                for session in registered["sessions"]
            )
        )
        if exact_panel_raw is None
        else exact_panel_raw
    )
    registration_raw = (
        paths["registration"].read_bytes()
        if registration_raw is None
        else registration_raw
    )
    ledger_raw = paths["ledger"].read_bytes() if ledger_raw is None else ledger_raw
    closure_raw = paths["closure"].read_bytes() if closure_raw is None else closure_raw
    database_path = paths["database"] if database_path is None else database_path
    database_reference = (
        result["database"]["reference"]
        if database_reference is None
        else database_reference
    )
    retained = continuity.open_nofollow_regular(database_path)
    try:
        verified = _verifier_api()(
            registration_raw,
            ledger_raw,
            closure_raw,
            retained,
            database_reference,
            exact_panel_raw=exact_panel_raw,
        )
        assert verified is None
        os.fstat(retained.descriptor)
    finally:
        retained.close()


def _rechain(events: list[dict[str, object]]) -> bytes:
    previous = events[0]["previous_event_sha256"]
    rebuilt = []
    for seq, original in enumerate(events):
        event = deepcopy(original)
        event.pop("event_sha256", None)
        event["seq"] = seq
        event["previous_event_sha256"] = previous
        event["event_sha256"] = continuity.canonical_json_sha256(event)
        previous = event["event_sha256"]
        rebuilt.append(event)
    return b"".join(continuity.canonical_json_bytes(event) + b"\n" for event in rebuilt)


def _bind_closure_to_ledger(
    closure: dict[str, object], ledger_raw: bytes
) -> bytes:
    tail = continuity.decode_canonical_json_object(ledger_raw.splitlines()[-1])
    changed = deepcopy(closure)
    changed["ledger_head"] = {
        "seq": tail["seq"],
        "event_type": tail["event_type"],
        "event_sha256": tail["event_sha256"],
    }
    return continuity.canonical_json_bytes(changed)


def test_task_4_4_verifier_accepts_real_complete_snapshot_and_retains_caller_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))

    _verify_result(result)


@pytest.mark.parametrize(
    "tamper",
    ["duplicate-key", "legacy", "hash", "prerequisite", "capability", "genesis"],
)
def test_task_4_4_verifier_rejects_registration_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    paths = _paths(result)
    registration_raw = paths["registration"].read_bytes()
    closure = _closure(result)
    if tamper == "duplicate-key":
        changed_raw = registration_raw.replace(
            b"{", b'{"schema_version":"rqgm-forward-panel-registration/4",', 1
        )
    else:
        changed = json.loads(registration_raw.decode("ascii"))
        if tamper == "legacy":
            changed["schema_version"] = "rqgm-forward-panel-registration/3"
        elif tamper == "prerequisite":
            changed["prerequisites_sha256"] = "0" * 64
        elif tamper == "capability":
            changed["prerequisites"]["collector"]["database_uuid"] = "0" * 64
        elif tamper == "genesis":
            changed["prerequisites"]["collector"]["genesis_sha256"] = "0" * 64
        else:
            changed["source"] = "drifted-source"
        changed_raw = continuity.canonical_json_bytes(changed)
        if tamper not in {"hash", "legacy"}:
            closure["registration_sha256"] = hashlib.sha256(changed_raw).hexdigest()
    closure_raw = continuity.canonical_json_bytes(closure)

    with pytest.raises(CollectorContinuityError):
        _verify_result(
            result,
            registration_raw=changed_raw,
            closure_raw=closure_raw,
        )


@pytest.mark.parametrize(
    "tamper",
    ["framing", "chain", "ordinal", "command", "skipped", "duplicate", "dangling"],
)
def test_task_4_4_verifier_rejects_ledger_framing_chain_or_schedule_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    paths = _paths(result)
    ledger_raw = paths["ledger"].read_bytes()
    closure = _closure(result)
    events = list(continuity.parse_collector_ledger(ledger_raw))
    if tamper == "framing":
        changed_raw = ledger_raw[:-1]
        closure_raw = paths["closure"].read_bytes()
    elif tamper == "chain":
        events[4]["event"]["command_sha256"] = "0" * 64
        changed_raw = b"".join(
            continuity.canonical_json_bytes(event) + b"\n" for event in events
        )
        closure_raw = paths["closure"].read_bytes()
    else:
        if tamper in {"ordinal", "command"}:
            for event in events:
                detail = event.get("event")
                if isinstance(detail, dict) and detail.get("step_ordinal") == 5:
                    detail["step_ordinal" if tamper == "ordinal" else "command_sha256"] = (
                        6 if tamper == "ordinal" else "0" * 64
                    )
        elif tamper == "skipped":
            events = [
                event
                for event in events
                if not (
                    isinstance(event.get("event"), dict)
                    and event["event"].get("step_ordinal") == 5
                )
            ]
        elif tamper == "duplicate":
            events.extend(deepcopy(events[-2:]))
        else:
            dangling = deepcopy(events[-2])
            dangling["event"]["attempt_id"] = "f" * 64
            dangling["event"]["lease_nonce_sha256"] = "e" * 64
            events.append(dangling)
        changed_raw = _rechain(events)
        closure_raw = _bind_closure_to_ledger(closure, changed_raw)

    with pytest.raises(CollectorContinuityError):
        _verify_result(result, ledger_raw=changed_raw, closure_raw=closure_raw)


@pytest.mark.parametrize("history_kind", ["retryable", "recovered-retryable"])
def test_task_4_4_verifier_accepts_legal_retry_and_recovery_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_kind: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    if history_kind == "retryable":
        _append_failed(prepared, quarantined=False)
    else:
        _append_dangling(prepared)
        with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
            outcome = _recover(lease, prepared["schedule"][0])
        assert outcome.retryable is True
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))

    _verify_result(result)


@pytest.mark.parametrize("nested_field", ["table_sha256", "outside_scope_sha256"])
def test_task_4_4_verifier_rejects_adjacent_attempt_full_step_state_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested_field: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    if nested_field == "outside_scope_sha256":
        _append_failed(prepared, quarantined=False)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    events = list(
        continuity.parse_collector_ledger(_paths(result)["ledger"].read_bytes())
    )
    if nested_field == "table_sha256":
        started = next(
            event
            for event in events
            if event["event_type"] == "ATTEMPT_STARTED"
            and event["event"]["step_ordinal"] == 1
        )
    else:
        failed = next(
            event
            for event in events
            if event["event_type"] == "ATTEMPT_FAILED"
            and event["event"]["step_ordinal"] == 0
        )
        assert failed["event"]["retryable"] is True
        retry_starts = [
            event
            for event in events
            if event["event_type"] == "ATTEMPT_STARTED"
            and event["event"]["step_ordinal"] == 0
        ]
        assert len(retry_starts) == 2
        started = retry_starts[1]
    terminal = next(
        event
        for event in events
        if event["event_type"] in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}
        and event["event"]["attempt_id"] == started["event"]["attempt_id"]
    )
    before = started["event"]["step_state_before"]
    aggregate = before["collector_state_sha256"]
    counts = deepcopy(before["table_counts"])
    key = sorted(before[nested_field])[0]
    before[nested_field][key] = "0" * 64
    terminal["event"]["step_state_before"] = deepcopy(before)
    assert before["collector_state_sha256"] == aggregate
    assert before["table_counts"] == counts
    changed_raw = _rechain(events)
    assert len(continuity.parse_collector_ledger(changed_raw)) == len(events)
    closure_raw = _bind_closure_to_ledger(_closure(result), changed_raw)

    with pytest.raises(
        CollectorContinuityError,
        match=r"collector materialization (?:retry )?state chain drifted",
    ):
        _verify_result(result, ledger_raw=changed_raw, closure_raw=closure_raw)


@pytest.mark.parametrize("tamper", ["nonretryable", "recovery-fork"])
def test_task_4_4_verifier_rejects_failed_or_forked_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    if tamper == "nonretryable":
        _append_failed(prepared, quarantined=False)
    else:
        _append_dangling(prepared)
        with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
            _recover(lease, prepared["schedule"][0])
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    events = list(continuity.parse_collector_ledger(_paths(result)["ledger"].read_bytes()))
    failed = next(event for event in events if event["event_type"] == "ATTEMPT_FAILED")
    if tamper == "nonretryable":
        failed["event"]["retryable"] = False
        failed["event"]["failure_classification"] = "forbidden_drift"
    else:
        failed["event"]["attempt_started_event_sha256"] = "0" * 64
    changed_raw = _rechain(events)
    closure_raw = _bind_closure_to_ledger(_closure(result), changed_raw)

    with pytest.raises(CollectorContinuityError):
        _verify_result(result, ledger_raw=changed_raw, closure_raw=closure_raw)


@pytest.mark.parametrize(
    "field",
    [
        "registration_sha256",
        "ledger_head.seq",
        "ledger_head.event_sha256",
        "logical_state",
        "live_database_identity",
        "live_ledger_identity",
        "database_uuid",
        "snapshot_database_reference",
    ],
)
def test_task_4_4_verifier_rejects_every_closure_binding_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    changed = _closure(result)
    if field == "ledger_head.seq":
        changed["ledger_head"]["seq"] -= 1
    elif field == "ledger_head.event_sha256":
        changed["ledger_head"]["event_sha256"] = "0" * 64
    elif field == "logical_state":
        changed["logical_state"]["collector_state_sha256"] = "0" * 64
    elif field in {"live_database_identity", "live_ledger_identity"}:
        changed[field]["file_st_ino"] += 1
    elif field == "snapshot_database_reference":
        changed[field]["identifier"] = "0" * 64
    else:
        changed[field] = "0" * 64

    with pytest.raises(CollectorContinuityError):
        _verify_result(
            result, closure_raw=continuity.canonical_json_bytes(changed)
        )


@pytest.mark.parametrize("drift", ["schema", "uuid", "cohort", "genesis", "state"])
def test_task_4_4_verifier_recomputes_snapshot_sqlite_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    source = _paths(result)["database"]
    changed_database = tmp_path / f"changed-{drift}.sqlite"
    changed_database.write_bytes(source.read_bytes())
    changed_database.chmod(0o600)
    with sqlite3.connect(changed_database) as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for (name,) in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        if drift == "schema":
            connection.execute("CREATE TABLE semantic_drift(value TEXT)")
        elif drift == "uuid":
            connection.execute(
                "UPDATE forward_collector_genesis SET database_uuid=? WHERE singleton=1",
                ("0" * 64,),
            )
        elif drift == "cohort":
            connection.execute(
                "DELETE FROM forward_capture_cohort "
                "WHERE rowid=(SELECT MIN(rowid) FROM forward_capture_cohort)"
            )
        elif drift == "genesis":
            connection.execute(
                "UPDATE forward_collector_genesis SET genesis_sha256=? WHERE singleton=1",
                ("0" * 64,),
            )
        else:
            connection.execute(
                "INSERT INTO collection_receipts "
                "(observed_at,source,request_json,response_json,response_sha256,created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("2099-01-08T00:00:00+08:00", "drift", "{}", "{}", hashlib.sha256(b"{}").hexdigest(), "2099-01-08T00:00:00+08:00"),
            )
    changed_raw = changed_database.read_bytes()
    reference = deepcopy(result["database"]["reference"])
    reference["identifier"] = hashlib.sha256(changed_raw).hexdigest()
    closure = _closure(result)
    closure["snapshot_database_reference"] = reference

    with pytest.raises(CollectorContinuityError):
        _verify_result(
            result,
            closure_raw=continuity.canonical_json_bytes(closure),
            database_path=changed_database,
            database_reference=reference,
        )


def test_task_4_4_verifier_rejects_snapshot_sidecar_without_writer_or_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    result = _materialize(prepared, _staging_parent(tmp_path))
    paths = _paths(result)
    before = {name: path.read_bytes() for name, path in paths.items()}
    Path(str(paths["database"]) + "-wal").write_bytes(b"forbidden")
    monkeypatch.setattr(
        continuity,
        "_recover_dangling_collector_attempt",
        lambda *args, **kwargs: pytest.fail("snapshot verifier attempted recovery"),
    )

    with pytest.raises(CollectorContinuityError):
        _verify_result(result)

    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_complete_schedule_uses_sqlite_backup_and_freezes_exact_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    source_registration = prepared["registration"].read_bytes()
    source_ledger = prepared["ledger"].read_bytes()
    history = continuity.parse_collector_ledger(source_ledger)
    terminal_state = history[-1]["event"]["step_state_after"]
    backup_calls = _backup_spy(monkeypatch)

    result = _materialize(prepared, output)
    paths = _paths(result)
    snapshot_directory = Path(result["staging_directory"])

    assert len(backup_calls) == 1
    assert snapshot_directory.parent == output
    assert snapshot_directory.name.startswith(".collector-snapshot-")
    assert backup_calls[0].parent == snapshot_directory
    assert backup_calls[0].name.startswith(".database-")
    assert backup_calls[0] != paths["database"]
    assert paths["registration"].read_bytes() == source_registration
    assert paths["ledger"].read_bytes() == source_ledger
    assert set(snapshot_directory.iterdir()) == set(paths.values())
    assert paths["database"].stat().st_ino != prepared["database"].stat().st_ino
    with sqlite3.connect(prepared["database"]) as live, sqlite3.connect(
        paths["database"]
    ) as snapshot:
        live_uuid = live.execute(
            "SELECT database_uuid FROM forward_collector_genesis WHERE singleton=1"
        ).fetchone()[0]
        snapshot_uuid = snapshot.execute(
            "SELECT database_uuid FROM forward_collector_genesis WHERE singleton=1"
        ).fetchone()[0]
        assert snapshot_uuid == live_uuid
        assert continuity.compute_collector_logical_state(snapshot) == (
            continuity.compute_collector_logical_state(live)
        )

    closure = _closure(result)
    registration = json.loads(source_registration)
    collector = registration["prerequisites"]["collector"]
    assert set(closure) == _CLOSURE_FIELDS
    assert closure == {
        "schema_version": continuity.CLOSURE_SCHEMA,
        "live_database_identity": collector["database_identity"],
        "live_ledger_identity": collector["ledger_identity"],
        "database_uuid": collector["database_uuid"],
        "registration_sha256": hashlib.sha256(source_registration).hexdigest(),
        "ledger_head": {
            "seq": history[-1]["seq"],
            "event_type": "ATTEMPT_COMPLETED",
            "event_sha256": history[-1]["event_sha256"],
        },
        "logical_state": terminal_state,
        "snapshot_database_reference": {
            "kind": "stock-data-database",
            "identifier": hashlib.sha256(paths["database"].read_bytes()).hexdigest(),
            "schema_version": DATABASE_SCHEMA,
        },
    }
    assert set(closure["snapshot_database_reference"]) == _REFERENCE_FIELDS
    assert closure["logical_state"] == terminal_state
    assert result["database"]["reference"] == closure[
        "snapshot_database_reference"
    ]
    assert result["continuity_closure"]["reference"]["identifier"] == (
        hashlib.sha256(paths["closure"].read_bytes()).hexdigest()
    )
    assert not any(
        Path(str(paths["database"]) + suffix).exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


@pytest.mark.parametrize("history_kind", ["retryable", "recovered-retryable"])
def test_final_complete_schedule_after_retryable_history_can_be_snapshotted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_kind: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    if history_kind == "retryable":
        _append_failed(prepared, quarantined=False)
    else:
        _append_dangling(prepared)
        with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
            outcome = _recover(lease, prepared["schedule"][0])
        assert outcome.terminal_event_type == "ATTEMPT_FAILED"
        assert outcome.retryable is True
    failed = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())[-1]
    assert failed["event_type"] == "ATTEMPT_FAILED"
    assert failed["event"]["retryable"] is True
    assert failed["event"]["recovered"] is (
        history_kind == "recovered-retryable"
    )

    _complete(prepared)
    history = continuity.parse_collector_ledger(prepared["ledger"].read_bytes())
    completed_ordinals = [
        event["event"]["step_ordinal"]
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
    ]
    assert completed_ordinals == list(range(12))

    result = _materialize(prepared, _staging_parent(tmp_path))
    closure = _closure(result)
    assert closure["ledger_head"]["event_sha256"] == history[-1]["event_sha256"]
    assert closure["logical_state"] == history[-1]["event"]["step_state_after"]


@pytest.mark.parametrize(
    "ledger_state",
    [
        "incomplete",
        "retryable-unfinished",
        "nonretryable-quarantine",
        "dangling",
        "duplicate-after-complete",
    ],
)
def test_noncomplete_ledger_rejects_before_backup_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_state: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    if ledger_state == "incomplete":
        _complete(prepared, 11)
    elif ledger_state == "retryable-unfinished":
        _append_failed(prepared, quarantined=False)
    elif ledger_state == "dangling":
        _append_dangling(prepared)
    elif ledger_state == "duplicate-after-complete":
        _complete(prepared)
        _append_duplicate_after_complete(prepared)
    else:
        _append_failed(prepared, quarantined=True)
        with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
            with pytest.raises(CollectorContinuityError, match="quarantined"):
                continuity._begin_collector_step_attempt(
                    lease, prepared["schedule"][0]
                )
    output = _staging_parent(tmp_path)
    backup_calls = _backup_spy(monkeypatch)

    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert backup_calls == []
    assert list(output.iterdir()) == []


def test_same_inode_ledger_prefix_rollback_rejects_before_snapshot_or_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    ledger_path = Path(prepared["ledger"])
    complete_raw = ledger_path.read_bytes()
    complete_history = continuity.parse_collector_ledger(complete_raw)
    terminal_index = next(
        index
        for index, event in enumerate(complete_history)
        if event["event_type"] == "ATTEMPT_COMPLETED"
        and event["event"]["step_ordinal"] == 10
    )
    rollback_raw = b"".join(
        complete_raw.splitlines(keepends=True)[: terminal_index + 1]
    )
    rollback_history = continuity.parse_collector_ledger(rollback_raw)
    assert rollback_history[-1]["event"]["step_ordinal"] == 10
    assert rollback_history[-1]["event_type"] == "ATTEMPT_COMPLETED"

    ledger_identity = os.stat(ledger_path)
    with continuity.open_registered_collector_read_connection(
        prepared["schedule"][11]
    ) as token:
        assert continuity.snapshot_collector_step_state(
            token, prepared["schedule"][11]
        ) == complete_history[-1]["event"]["step_state_after"]

    ledger_path.write_bytes(rollback_raw)
    after_identity = os.stat(ledger_path)
    assert (after_identity.st_dev, after_identity.st_ino) == (
        ledger_identity.st_dev,
        ledger_identity.st_ino,
    )
    assert continuity.parse_collector_ledger(ledger_path.read_bytes()) == (
        *rollback_history,
    )

    output = _staging_parent(tmp_path)
    backup_calls = _backup_spy(monkeypatch)
    recovery_calls = 0
    adoption_calls = 0
    recovery_api = continuity._recover_dangling_collector_attempt
    append_api = continuity._append_collector_phase_event_once

    def recovery(*args, **kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        return recovery_api(*args, **kwargs)

    def adoption(*args, **kwargs):
        nonlocal adoption_calls
        adoption_calls += 1
        return append_api(*args, **kwargs)

    monkeypatch.setattr(continuity, "_recover_dangling_collector_attempt", recovery)
    monkeypatch.setattr(continuity, "_append_collector_phase_event_once", adoption)

    with pytest.raises(CollectorContinuityError, match=r"ledger|schedule|complete"):
        _materialize(prepared, output)

    assert backup_calls == []
    assert recovery_calls == 0
    assert adoption_calls == 0
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "drift", ["tail-state", "database-replacement", "ledger-replacement", "registration-replacement"]
)
def test_bound_authority_or_tail_drift_is_zero_backup_zero_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    if drift == "tail-state":
        response = "{}"
        with sqlite3.connect(prepared["database"]) as connection:
            connection.execute(
                "INSERT INTO collection_receipts "
                "(observed_at,source,request_json,response_json,response_sha256,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "2099-01-08T08:00:00+08:00",
                    "uncoordinated",
                    "{}",
                    response,
                    hashlib.sha256(response.encode("ascii")).hexdigest(),
                    "2099-01-08T08:00:00+08:00",
                ),
            )
    elif drift == "registration-replacement":
        prepared["registration"].write_bytes(
            prepared["registration"].read_bytes() + b"\n"
        )
    else:
        target = prepared["database" if drift == "database-replacement" else "ledger"]
        replacement = target.with_name(f"replacement-{target.name}")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
    output = _staging_parent(tmp_path)
    backup_calls = _backup_spy(monkeypatch)

    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert backup_calls == []
    assert list(output.iterdir()) == []


def test_busy_capture_lease_rejects_before_backup_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    backup_calls = _backup_spy(monkeypatch)

    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        with pytest.raises(CollectorContinuityError):
            _materialize(prepared, output)

    assert backup_calls == []
    assert list(output.iterdir()) == []


def test_uncoordinated_database_mutation_during_backup_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    original_connect = sqlite3.connect

    def mutate(_target: sqlite3.Connection) -> None:
        response = "{}"
        with original_connect(prepared["database"]) as connection:
            connection.execute(
                "INSERT INTO collection_receipts "
                "(observed_at,source,request_json,response_json,response_sha256,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "2099-01-08T08:00:00+08:00",
                    "uncoordinated",
                    "{}",
                    response,
                    hashlib.sha256(response.encode("ascii")).hexdigest(),
                    "2099-01-08T08:00:00+08:00",
                ),
            )

    backup_calls = _backup_spy(monkeypatch, before=mutate)
    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert len(backup_calls) == 1
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("sidecar", ["wal", "shm", "hot-journal"])
def test_live_sqlite_sidecars_fail_closed_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    if sidecar == "hot-journal":
        _leave_hot_delete_journal(prepared["database"])
    else:
        Path(str(prepared["database"]) + f"-{sidecar}").write_bytes(b"forbidden")
    output = _staging_parent(tmp_path)
    backup_calls = _backup_spy(monkeypatch)

    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert backup_calls == []
    assert list(output.iterdir()) == []


def test_snapshot_sidecar_residual_rejects_and_removes_staging_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)

    def leave_sidecar(target: sqlite3.Connection) -> None:
        database_path = Path(str(target.execute("PRAGMA database_list").fetchone()[2]))
        Path(str(database_path) + "-wal").write_bytes(b"residual")

    backup_calls = _backup_spy(monkeypatch, after=leave_sidecar)
    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert len(backup_calls) == 1
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("collision", ["file", "directory", "symlink"])
def test_output_collisions_are_never_overwritten_or_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    token = "a" * 64
    monkeypatch.setattr(continuity.secrets, "token_hex", lambda size: token)
    collision_path = output / f".collector-snapshot-{token}"
    symlink_target = tmp_path / "symlink-target"
    if collision == "file":
        collision_path.write_bytes(b"existing-file")
    elif collision == "directory":
        collision_path.mkdir()
        (collision_path / "sentinel").write_bytes(b"existing-directory")
    else:
        symlink_target.mkdir()
        (symlink_target / "sentinel").write_bytes(b"symlink-target")
        collision_path.symlink_to(symlink_target, target_is_directory=True)
    before_output = collision_path.lstat()
    before_target = (
        (symlink_target / "sentinel").read_bytes() if collision == "symlink" else None
    )
    backup_calls = _backup_spy(monkeypatch)

    with pytest.raises(CollectorContinuityError):
        _materialize(prepared, output)

    assert backup_calls == []
    assert collision_path.lstat().st_ino == before_output.st_ino
    if collision == "file":
        assert collision_path.read_bytes() == b"existing-file"
    elif collision == "directory":
        assert (collision_path / "sentinel").read_bytes() == b"existing-directory"
    else:
        assert collision_path.is_symlink()
        assert (symlink_target / "sentinel").read_bytes() == before_target


@pytest.mark.parametrize("fault", ["backup", "fsync", "rename"])
def test_snapshot_faults_leave_no_output_fd_or_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    before_entries = set(output.iterdir())
    before_fds = set(os.listdir("/dev/fd"))
    if fault == "backup":
        _backup_spy(
            monkeypatch, failure=sqlite3.OperationalError("injected backup crash")
        )
    else:
        _backup_spy(monkeypatch)
        if fault == "fsync":
            original_fsync = continuity.os.fsync
            injected = False

            def fail_once(descriptor: int) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    raise OSError("injected fsync crash")
                original_fsync(descriptor)

            monkeypatch.setattr(
                continuity.os,
                "fsync",
                fail_once,
            )
        else:
            monkeypatch.setattr(
                continuity.os,
                "replace",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    OSError("injected rename crash")
                ),
            )

    with pytest.raises((CollectorContinuityError, OSError, sqlite3.Error)):
        _materialize(prepared, output)

    assert set(output.iterdir()) == before_entries
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass
    assert set(os.listdir("/dev/fd")) == before_fds


@pytest.mark.parametrize(
    "crash_point", ["after-backup", "after-database-rename", "after-closure-write"]
)
def test_process_crash_before_return_never_publishes_snapshot_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    before_entries = set(output.iterdir())
    locator_reader, locator_writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(locator_reader)
        if crash_point == "after-backup":
            original_backup = continuity._backup_registered_collector_database

            def crash_after_backup(*args, **kwargs) -> None:
                original_backup(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)

            continuity._backup_registered_collector_database = crash_after_backup
        elif crash_point == "after-database-rename":
            original_replace = continuity.os.replace

            def crash_after_replace(*args, **kwargs) -> None:
                original_replace(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)

            continuity.os.replace = crash_after_replace
        else:
            original_write = continuity._write_collector_snapshot_artifact
            writes = 0

            def crash_after_closure(*args, **kwargs) -> str:
                nonlocal writes
                path = original_write(*args, **kwargs)
                writes += 1
                if writes == 3:
                    os.kill(os.getpid(), signal.SIGKILL)
                return path

            continuity._write_collector_snapshot_artifact = crash_after_closure
        try:
            result = _materialize(prepared, output)
            os.write(locator_writer, json.dumps(result, default=str).encode("ascii"))
        finally:
            os.close(locator_writer)
            os._exit(92)

    os.close(locator_writer)
    _, status = os.waitpid(child, 0)
    locator = os.read(locator_reader, 4096)
    os.close(locator_reader)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert locator == b""
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass
    created = set(output.iterdir()) - before_entries
    orphans = [
        path for path in created if path.name.startswith(".") and path.is_dir()
    ]
    assert len(orphans) == 1
    orphan = orphans[0]
    assert len(orphan.name) > 8
    orphan_identity = (orphan.stat().st_dev, orphan.stat().st_ino)
    orphan_before = _tree_fingerprint(orphan)

    result = _materialize(prepared, output)
    paths = _paths(result)
    retry_staging = Path(result["staging_directory"])

    assert retry_staging.parent == output
    assert retry_staging.name.startswith(".collector-snapshot-")
    assert retry_staging != orphan
    assert (retry_staging.stat().st_dev, retry_staging.stat().st_ino) != orphan_identity
    assert orphan.is_dir()
    assert _tree_fingerprint(orphan) == orphan_before
    assert all(orphan != path and orphan not in path.parents for path in paths.values())
    assert set(retry_staging.iterdir()) == set(paths.values())
    assert set(output.iterdir()) == {orphan, retry_staging}


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "extra",
        "type",
        "schema",
        "kind",
        "illegal-head",
        "illegal-step-state",
        "readiness-field",
        "authority-field",
    ],
)
def test_closure_tamper_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    prepared = _collector(tmp_path / "collector", monkeypatch)
    _complete(prepared)
    output = _staging_parent(tmp_path)
    result = _materialize(prepared, output)
    closure = _closure(result)
    changed = deepcopy(closure)
    if tamper == "missing":
        changed.pop("database_uuid")
    elif tamper == "extra":
        changed["unexpected"] = True
    elif tamper == "type":
        changed["database_uuid"] = 7
    elif tamper == "schema":
        changed["schema_version"] = "stockdata-forward-collector-continuity-closure/2"
    elif tamper == "kind":
        changed["snapshot_database_reference"]["kind"] = "readiness-authority"
    elif tamper == "illegal-head":
        changed["ledger_head"]["event_type"] = "ATTEMPT_FAILED"
    elif tamper == "illegal-step-state":
        changed["logical_state"]["receipt_id_high_water"] = -1
    elif tamper == "readiness-field":
        changed["ready"] = True
    else:
        changed["authority"] = {"admitted": True}

    with pytest.raises(CollectorContinuityError):
        continuity.decode_collector_continuity_closure(
            continuity.canonical_json_bytes(changed)
        )


def test_closure_and_snapshot_reference_fields_are_exact() -> None:
    assert continuity.CLOSURE_SCHEMA == (
        "stockdata-forward-collector-continuity-closure/1"
    )
    assert _CLOSURE_FIELDS == {
        "schema_version",
        "live_database_identity",
        "live_ledger_identity",
        "database_uuid",
        "registration_sha256",
        "ledger_head",
        "logical_state",
        "snapshot_database_reference",
    }
    assert _REFERENCE_FIELDS == {"kind", "identifier", "schema_version"}
