from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import sys

import pytest

import stockdata.collector_continuity as continuity
import stockdata.future_panel_registration as registration
from stockdata.collector_continuity import (
    CollectorContinuityError,
    append_collector_ledger_event,
    canonical_json_sha256,
    default_collector_ledger_path,
    load_verified_prepared_collector,
    open_exact_collector_sqlite,
    open_existing_regular_file,
)


_SYMBOLS = (
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
_SESSIONS = ("2099-01-05", "2099-01-06", "2099-01-07")
_SOURCE = "tencent"
_ADJUSTMENT_MODE = "raw"
_ADJUSTMENT_VERSION = "tencent-qt-daily-v1"
_FOUNDATION_TRIGGER_NAMES = (
    "daily_non_final_insert",
    "sync_coverage_exact_noop",
)
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


def _api(name: str):
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing task 2.4 foundation API: {name}")
    return value


def _sha(char: str = "a") -> str:
    return char * 64


def _panel() -> tuple[str, ...]:
    return tuple(sorted(f"{symbol}@{session}" for symbol in _SYMBOLS for session in _SESSIONS))


def _cohort() -> dict[str, object]:
    return {
        "symbols": list(_SYMBOLS),
        "start": _SESSIONS[0],
        "source": _SOURCE,
        "adjustment_mode": _ADJUSTMENT_MODE,
        "adjustment_version": _ADJUSTMENT_VERSION,
    }


def _prepare_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    monkeypatch.setattr(
        registration,
        "_now",
        lambda: datetime.fromisoformat("2026-08-01T12:00:00+08:00"),
    )
    panel_file = tmp_path / "panel.json"
    panel_file.write_bytes(continuity.canonical_json_bytes(list(_panel())))
    database = tmp_path / "collector.sqlite"
    registration.prepare_future_collector_database(
        database_file=database,
        panel_file=panel_file,
    )
    return database, Path(default_collector_ledger_path(database))


def _bound_registration(database: Path) -> Path:
    registration = database.with_name("registration.json")
    ledger = Path(default_collector_ledger_path(database))
    prepared = load_verified_prepared_collector(
        database_path=database,
        ledger_path=ledger,
    )
    panel = list(_panel())
    prerequisites = {
        "collector": {
            "schema_version": "stockdata-forward-collector-capability/2",
            "database_path": prepared["database_path"],
            "ledger_path": prepared["ledger_path"],
            "source": _SOURCE,
            "adjustment_mode": _ADJUSTMENT_MODE,
            "adjustment_version": _ADJUSTMENT_VERSION,
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
        "symbols": list(_SYMBOLS),
        "sessions": list(_SESSIONS),
        "source": _SOURCE,
        "adjustment_mode": _ADJUSTMENT_MODE,
        "adjustment_version": _ADJUSTMENT_VERSION,
        "database_path": prepared["database_path"],
        "panel_sha256": canonical_json_sha256(panel),
        "workspace_count": 36,
        "outcome_feedback_used": False,
        "status": "AWAITING_FULL_SNAPSHOT_READINESS",
        "prerequisite_files": {},
        "prerequisites": prerequisites,
        "prerequisites_sha256": canonical_json_sha256(prerequisites),
    }
    registration.write_bytes(continuity.canonical_json_bytes(payload))
    if len(continuity.parse_collector_ledger(ledger)) == 1:
        with open_existing_regular_file(ledger) as opened:
            append_collector_ledger_event(
                opened,
                event_type="REGISTRATION_BOUND",
                event={
                    "registration_sha256": hashlib.sha256(registration.read_bytes()).hexdigest(),
                    "panel_sha256": payload["panel_sha256"],
                    "sessions": list(_SESSIONS),
                    "sessions_sha256": canonical_json_sha256(list(_SESSIONS)),
                    "prerequisites_sha256": payload["prerequisites_sha256"],
                    "bound_at": "2026-08-01T12:00:01+08:00",
                },
            )
    return registration


def _schedule(database: Path, **overrides: object) -> tuple[object, ...]:
    if overrides:
        try:
            _api("freeze_collector_step_schedule")(
                registration_file=_bound_registration(database), **overrides
            )
        except TypeError as exc:
            raise CollectorContinuityError(
                "caller-supplied schedule authority is not accepted"
            ) from exc
        raise CollectorContinuityError("caller-supplied schedule authority was accepted")
    return tuple(
        _api("freeze_collector_step_schedule")(
            registration_file=_bound_registration(database)
        )
    )


def _step_state(allowed_tables: frozenset[str]) -> dict[str, object]:
    tables = tuple(continuity.COLLECTOR_STATE_TABLES)
    return {
        "schema_version": _api("COLLECTOR_STEP_STATE_SCHEMA"),
        "collector_state_sha256": _sha("a"),
        "table_counts": {table: 0 for table in tables},
        "table_sha256": {table: _sha("b") for table in tables},
        "outside_scope_sha256": {table: _sha("c") for table in allowed_tables},
        "receipt_id_high_water": 0,
    }


def _validate_step_state(
    value: Mapping[str, object], allowed_tables: frozenset[str]
) -> dict[str, object]:
    return _api("validate_collector_step_state")(
        value,
        allowed_tables=allowed_tables,
    )


def _insert_receipt(connection: sqlite3.Connection, receipt_id: int | None = None) -> int:
    response = "{}"
    response_sha256 = hashlib.sha256(response.encode("ascii")).hexdigest()
    if receipt_id is None:
        cursor = connection.execute(
            "INSERT INTO collection_receipts "
            "(observed_at,source,request_json,response_json,response_sha256,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "2099-01-05T08:00:00+08:00",
                _SOURCE,
                "{}",
                response,
                response_sha256,
                "2099-01-05T08:00:01+08:00",
            ),
        )
        return int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO collection_receipts "
        "(receipt_id,observed_at,source,request_json,response_json,response_sha256,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            receipt_id,
            "2099-01-05T08:00:00+08:00",
            _SOURCE,
            "{}",
            response,
            response_sha256,
            "2099-01-05T08:00:01+08:00",
        ),
    )
    return receipt_id


def _insert_daily(
    connection: sqlite3.Connection,
    *,
    code: str,
    day: str,
    receipt_id: int | None = None,
    is_final: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO daily "
        "(code,date,open,high,low,close,volume,source,adjustment_mode,"
        "adjustment_version,retrieved_at,is_final,receipt_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            code,
            day,
            1.0,
            2.0,
            0.5,
            1.5,
            100.0,
            _SOURCE,
            _ADJUSTMENT_MODE,
            _ADJUSTMENT_VERSION,
            "2099-01-05T08:00:01+08:00",
            is_final,
            receipt_id,
        ),
    )


def _insert_sync(
    connection: sqlite3.Connection,
    *,
    start: str = _SESSIONS[0],
    end: str = _SESSIONS[0],
    retrieved_at: str = "2099-01-05T08:00:01+08:00",
) -> None:
    connection.execute(
        "INSERT INTO sync_coverage "
        "(code,source,adjustment_mode,adjustment_version,start_date,end_date,retrieved_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            _SYMBOLS[0],
            _SOURCE,
            _ADJUSTMENT_MODE,
            _ADJUSTMENT_VERSION,
            start,
            end,
            retrieved_at,
        ),
    )


def _snapshot(connection: sqlite3.Connection, spec: object) -> dict[str, object]:
    del connection
    with _api("open_registered_collector_read_connection")(spec) as read_connection:
        return _api("snapshot_collector_step_state")(read_connection, spec)


def _expected_command(
    database: Path, command: str, session: str, *, symbols: tuple[str, ...] = _SYMBOLS
) -> tuple[str, ...]:
    base = (str(Path(sys.executable).resolve()), "-m", "stockdata.cli")
    if command == "forward-context-capture":
        return base + (command, "--database", str(database.resolve()), "--date", session)
    if command == "forward-corporate-actions-capture":
        return base + (command, "--database", str(database.resolve()), "--date", session)
    return base + (
        "forward-capture",
        "--database",
        str(database.resolve()),
        "--codes",
        ",".join(symbols),
        "--start",
        _SESSIONS[0],
        "--end",
        session,
        "--source",
        _SOURCE,
        "--adjustment-version",
        _ADJUSTMENT_VERSION,
    )


def _rehash_event(event: dict[str, object]) -> dict[str, object]:
    result = dict(event)
    result["event_sha256"] = canonical_json_sha256(
        {key: value for key, value in result.items() if key != "event_sha256"}
    )
    return result


def _event_state(aggregate: str) -> dict[str, object]:
    return _step_state(_PRICES_ALLOWED_TABLES) | {
        "collector_state_sha256": aggregate,
    }


def _attempt_started_detail() -> dict[str, object]:
    return {
        "registration_sha256": _sha("d"),
        "database_uuid": _sha("b"),
        "session": _SESSIONS[0],
        "phase": "post_close",
        "step_id": "post_close_prices",
        "step_ordinal": 3,
        "attempt_id": "attempt-0001",
        "command_sha256": _sha("2"),
        "lease_nonce_sha256": _sha("7"),
        "started_at": "2026-08-23T00:00:02+08:00",
        "state_before_sha256": _sha("3"),
        "step_state_before": _event_state(_sha("3")),
        "step_raw_before": {
            "schema_version": continuity.COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
            "selector_rows": {table: [] for table in sorted(_PRICES_ALLOWED_TABLES)},
        },
    }


def _attempt_completed_detail() -> dict[str, object]:
    return {
        **{key: value for key, value in _attempt_started_detail().items() if key != "lease_nonce_sha256"},
        "completed_at": "2026-08-23T00:00:03+08:00",
        "state_after_sha256": _sha("4"),
        "step_state_after": _event_state(_sha("4")),
        "returncode": 0,
        "stdout_sha256": _sha("5"),
        "stdout_bytes": 0,
        "stderr_sha256": _sha("6"),
        "stderr_bytes": 0,
        "process_result_known": True,
        "recovered": False,
        "verifier_id": "step-state-test",
    }


def test_daily_final_insert_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        _insert_daily(connection, code=_SYMBOLS[0], day=_SESSIONS[0], is_final=1)
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1


def test_pending_daily_insert_rejects_before_transaction_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                receipt_id = _insert_receipt(connection)
                _insert_sync(connection)
                _insert_daily(
                    connection,
                    code=_SYMBOLS[0],
                    day=_SESSIONS[0],
                    receipt_id=receipt_id,
                    is_final=0,
                )
        assert connection.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sync_coverage").fetchone()[0] == 0


def test_exact_sync_coverage_update_is_ignored_and_timestamp_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        _insert_sync(connection)
        connection.commit()
        connection.execute(
            "UPDATE sync_coverage SET start_date=?, end_date=?, retrieved_at=? "
            "WHERE code=?",
            (_SESSIONS[0], _SESSIONS[0], "2099-01-05T09:00:00+08:00", _SYMBOLS[0]),
        )
        connection.commit()
        assert connection.execute("SELECT retrieved_at FROM sync_coverage").fetchone()[0] == (
            "2099-01-05T08:00:01+08:00"
        )


def test_sync_coverage_true_widening_allows_new_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        _insert_sync(connection)
        connection.commit()
        connection.execute(
            "UPDATE sync_coverage SET start_date=?, end_date=?, retrieved_at=? WHERE code=?",
            (
                "2099-01-04",
                _SESSIONS[1],
                "2099-01-06T08:00:01+08:00",
                _SYMBOLS[0],
            ),
        )
        connection.commit()
        assert connection.execute(
            "SELECT start_date,end_date,retrieved_at FROM sync_coverage"
        ).fetchone() == (
            "2099-01-04",
            _SESSIONS[1],
            "2099-01-06T08:00:01+08:00",
        )


def test_missing_foundation_trigger_rejects_prepared_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger_sql = getattr(continuity, "COLLECTOR_EVIDENCE_TRIGGER_SQL", None)
    if not isinstance(trigger_sql, Mapping):
        pytest.fail("missing frozen collector trigger map")
    missing = [name for name in _FOUNDATION_TRIGGER_NAMES if name not in trigger_sql]
    if missing:
        pytest.fail(f"missing frozen foundation triggers: {', '.join(missing)}")
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        for name in _FOUNDATION_TRIGGER_NAMES:
            connection.execute(f'DROP TRIGGER "{name}"')
    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_foundation_trigger_sql_drift_rejects_prepared_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger_sql = getattr(continuity, "COLLECTOR_EVIDENCE_TRIGGER_SQL", None)
    if not isinstance(trigger_sql, Mapping):
        pytest.fail("missing frozen collector trigger map")
    missing = [name for name in _FOUNDATION_TRIGGER_NAMES if name not in trigger_sql]
    if missing:
        pytest.fail(f"missing frozen foundation triggers: {', '.join(missing)}")
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        for name in _FOUNDATION_TRIGGER_NAMES:
            connection.execute(f'DROP TRIGGER "{name}"')
            frozen_sql = str(trigger_sql[name])
            if name == "daily_non_final_insert":
                drifted_sql = frozen_sql.replace(
                    "daily evidence must be finalized on insert",
                    "daily evidence must be finalized on insert (drift)",
                    1,
                )
            else:
                drifted_sql = frozen_sql.replace(
                    "WHEN NEW.code IS OLD.code",
                    "WHEN 1=1 AND NEW.code IS OLD.code",
                    1,
                )
            assert drifted_sql != frozen_sql
            connection.execute(drifted_sql)
    with pytest.raises(CollectorContinuityError):
        load_verified_prepared_collector(database_path=database, ledger_path=ledger)


def test_step_state_validator_accepts_exact_schema_and_allowed_scope() -> None:
    allowed = _PRICES_ALLOWED_TABLES
    state = _step_state(allowed)
    normalized = _validate_step_state(state, allowed)
    assert set(normalized) == {
        "schema_version",
        "collector_state_sha256",
        "table_counts",
        "table_sha256",
        "outside_scope_sha256",
        "receipt_id_high_water",
    }
    assert set(normalized["table_counts"]) == set(continuity.COLLECTOR_STATE_TABLES)
    assert set(normalized["table_sha256"]) == set(continuity.COLLECTOR_STATE_TABLES)
    assert set(normalized["outside_scope_sha256"]) == set(allowed)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "bad-hash", "scope-drift"])
def test_step_state_validator_rejects_exact_schema_drift(mutation: str) -> None:
    allowed = _PRICES_ALLOWED_TABLES
    state = _step_state(allowed)
    if mutation == "missing":
        del state["table_sha256"]
    elif mutation == "unknown":
        state["unexpected"] = 1
    elif mutation == "bad-hash":
        state["collector_state_sha256"] = "A" * 64
    else:
        state["outside_scope_sha256"] = {
            "daily": _sha("c"),
            "sync_coverage": _sha("c"),
        }
    with pytest.raises(CollectorContinuityError):
        _validate_step_state(state, allowed)


def test_snapshot_returns_aggregate_counts_table_digests_complement_and_high_water(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        state = _snapshot(connection, spec)
        normalized = _validate_step_state(
            state,
            _PRICES_ALLOWED_TABLES,
        )
        assert set(normalized["table_counts"]) == set(continuity.COLLECTOR_STATE_TABLES)
        assert set(normalized["table_sha256"]) == set(continuity.COLLECTOR_STATE_TABLES)
        assert set(normalized["outside_scope_sha256"]) == set(_PRICES_ALLOWED_TABLES)
        assert normalized["receipt_id_high_water"] == 0


def test_snapshot_is_stable_across_insert_order_and_page_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    rows = [(_SYMBOLS[0], _SESSIONS[0]), (_SYMBOLS[1], _SESSIONS[1])]
    left_spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        left,
        _,
    ):
        for code, day in rows:
            _insert_daily(left, code=code, day=day)
        left.commit()
        first = _snapshot(left, left_spec)
    with continuity.open_registered_collector_read_connection(left_spec) as right:
        assert not isinstance(right, sqlite3.Connection)
        assert continuity.snapshot_collector_step_state(right, left_spec) == first


def test_snapshot_inside_allowed_selector_changes_table_and_aggregate_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        before = _snapshot(connection, spec)
        receipt_id = _insert_receipt(connection)
        _insert_daily(
            connection,
            code=_SYMBOLS[0],
            day=spec.session,
            receipt_id=receipt_id,
        )
        connection.commit()
        after = _snapshot(connection, spec)
    assert before["collector_state_sha256"] != after["collector_state_sha256"]
    assert before["table_sha256"]["daily"] != after["table_sha256"]["daily"]
    assert before["outside_scope_sha256"]["daily"] == after["outside_scope_sha256"]["daily"]


def test_authority_state_and_loader_ignore_temp_schema_shadows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        baseline_step = _snapshot(connection, spec)
        baseline_logical = continuity.compute_collector_logical_state(connection)
        connection.execute(
            "CREATE TEMP TABLE daily AS SELECT * FROM main.daily WHERE 0"
        )
        connection.execute(
            "INSERT INTO temp.daily "
            "(code,date,open,high,low,close,volume,source,adjustment_mode,"
            "adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _SYMBOLS[0],
                spec.session,
                1.0,
                2.0,
                0.5,
                1.5,
                100.0,
                _SOURCE,
                _ADJUSTMENT_MODE,
                _ADJUSTMENT_VERSION,
                "2099-01-05T08:00:01+08:00",
                1,
                None,
            ),
        )
        connection.execute(
            "CREATE TEMP TABLE collection_receipts "
            "AS SELECT * FROM main.collection_receipts WHERE 0"
        )
        connection.execute(
            "INSERT INTO temp.collection_receipts "
            "(receipt_id,observed_at,source,request_json,response_json,response_sha256,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                1,
                "2099-01-05T08:00:00+08:00",
                "temp-shadow-source",
                "{}",
                "{}",
                _sha("f"),
                "2099-01-05T08:00:01+08:00",
            ),
        )
        connection.execute(
            "CREATE TEMP TABLE forward_collector_genesis "
            "AS SELECT * FROM main.forward_collector_genesis"
        )
        connection.execute(
            "UPDATE temp.forward_collector_genesis SET genesis_json='{}'"
        )
        connection.commit()
        shadowed_step = _snapshot(connection, spec)
        shadowed_logical = continuity.compute_collector_logical_state(connection)
        assert shadowed_step == baseline_step
        assert shadowed_logical == baseline_logical


def test_prepared_loader_ignores_temp_genesis_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)

    @contextmanager
    def shadowed_open(**kwargs: object):
        with open_exact_collector_sqlite(**kwargs) as opened:
            connection, _ = opened
            connection.execute(
                "CREATE TEMP TABLE forward_collector_genesis "
                "AS SELECT * FROM main.forward_collector_genesis"
            )
            connection.execute(
                "UPDATE temp.forward_collector_genesis SET genesis_json='{}'"
            )
            connection.commit()
            yield opened

    monkeypatch.setattr(continuity, "open_exact_collector_sqlite", shadowed_open)
    loaded = load_verified_prepared_collector(
        database_path=database,
        ledger_path=ledger,
    )
    assert loaded["database_path"] == str(database.resolve())


def test_snapshot_requires_selector_source_binding_for_allowed_daily_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        before = _snapshot(connection, spec)
        receipt_id = _insert_receipt(connection)
        _insert_daily(
            connection,
            code=_SYMBOLS[0],
            day=spec.session,
            receipt_id=receipt_id,
        )
        connection.commit()
        after = _snapshot(connection, spec)
    assert before["outside_scope_sha256"]["daily"] == after["outside_scope_sha256"]["daily"]
    assert before["collector_state_sha256"] != after["collector_state_sha256"]
    assert before["table_sha256"]["daily"] != after["table_sha256"]["daily"]


def test_snapshot_treats_unbound_daily_receipt_as_outside_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        before = _snapshot(connection, spec)
        _insert_daily(connection, code=_SYMBOLS[0], day=spec.session, receipt_id=None)
        connection.commit()
        after = _snapshot(connection, spec)
    assert before["outside_scope_sha256"]["daily"] != after["outside_scope_sha256"]["daily"]


def test_snapshot_rejects_dangling_receipt_foreign_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        _insert_daily(connection, code=_SYMBOLS[0], day=_SESSIONS[0], receipt_id=9999)
        connection.commit()

    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        with pytest.raises(CollectorContinuityError):
            _snapshot(connection, spec)


def test_snapshot_foreign_date_or_symbol_changes_allowed_table_complement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        before = _snapshot(connection, spec)
        _insert_daily(connection, code="999999.SZ", day="2099-01-08")
        connection.commit()
        after = _snapshot(connection, spec)
    assert before["outside_scope_sha256"]["daily"] != after["outside_scope_sha256"]["daily"]


def test_snapshot_disallowed_table_change_changes_its_table_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (connection, _):
        before = _snapshot(connection, spec)
        receipt_id = _insert_receipt(connection)
        connection.execute(
            "INSERT INTO forward_context_observations "
            "(effective_date,observation_phase,decision_available_at,"
            "outcome_observed_at,finalized_at,source,receipt_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                _SESSIONS[0],
                "pre_open",
                "2099-01-05T08:00:00+08:00",
                None,
                None,
                _SOURCE,
                receipt_id,
            ),
        )
        connection.commit()
        after = _snapshot(connection, spec)
    assert before["table_sha256"]["forward_context_observations"] != after["table_sha256"]["forward_context_observations"]


def test_snapshot_ignores_caller_text_factory_and_converter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[3]
    with sqlite3.connect(database) as normal:
        with pytest.raises(CollectorContinuityError):
            continuity.snapshot_collector_step_state(normal, spec)
    original_converter = sqlite3.converters.get("TEXT")
    sqlite3.register_converter("TEXT", lambda raw: b"caller-converted")
    try:
        with sqlite3.connect(
            database,
            detect_types=sqlite3.PARSE_DECLTYPES,
        ) as converted:
            converted.text_factory = lambda raw: raw.decode("utf-8").upper()
            with pytest.raises(CollectorContinuityError):
                continuity.snapshot_collector_step_state(converted, spec)
    finally:
        if original_converter is None:
            sqlite3.converters.pop("TEXT", None)
        else:
            sqlite3.converters["TEXT"] = original_converter
    del ledger


def test_schedule_freezes_exact_twelve_steps_ordinals_commands_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _prepare_collector(tmp_path, monkeypatch)
    schedule = _schedule(database)
    assert len(schedule) == 12
    assert [spec.step_id for spec in schedule] == [
        step_id
        for session in _SESSIONS
        for step_id in (
            "pre_open_context",
            "pre_open_corporate_actions",
            "post_close_context",
            "post_close_prices",
        )
    ]
    assert [spec.phase for spec in schedule] == [
        phase
        for _ in _SESSIONS
        for phase in ("pre_open", "pre_open", "post_close", "post_close")
    ]
    assert [spec.step_ordinal for spec in schedule] == list(range(12))
    expected_allowed_tables = [
        allowed
        for _ in _SESSIONS
        for allowed in (
            _CONTEXT_ALLOWED_TABLES,
            _CORPORATE_ACTIONS_ALLOWED_TABLES,
            _CONTEXT_ALLOWED_TABLES,
            _PRICES_ALLOWED_TABLES,
        )
    ]
    assert [spec.allowed_tables for spec in schedule] == expected_allowed_tables
    expected_commands = []
    for session in _SESSIONS:
        expected_commands.extend(
            (
                _expected_command(database, "forward-context-capture", session),
                _expected_command(database, "forward-corporate-actions-capture", session),
                _expected_command(database, "forward-context-capture", session),
                _expected_command(database, "forward-capture", session),
            )
        )
    for spec, expected in zip(schedule, expected_commands):
        assert spec.command == expected
        assert spec.command_sha256 == canonical_json_sha256(
            {
                "schema_version": "stockdata-forward-collector-command/1",
                "argv": list(expected),
            }
        )
        assert Path(spec.database_path).is_absolute()
        assert Path(spec.command[0]).is_absolute()
        assert spec.symbols == tuple(sorted(_SYMBOLS))


@pytest.mark.parametrize("mutation", ["unsorted", "duplicate", "wrong-12x3", "source", "nonraw", "extra-cohort"])
def test_schedule_rejects_panel_cohort_and_provenance_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    database, _ = _prepare_collector(tmp_path, monkeypatch)
    values: dict[str, object] = {}
    if mutation == "unsorted":
        values["panel"] = tuple(reversed(_panel()))
    elif mutation == "duplicate":
        values["panel"] = _panel()[:-1] + (_panel()[-2],)
    elif mutation == "wrong-12x3":
        values["panel"] = _panel()[:-12]
    elif mutation == "source":
        values["source"] = "other"
    elif mutation == "nonraw":
        values["adjustment_mode"] = "qfq"
    else:
        values["cohort"] = {**_cohort(), "unexpected": True}
    with pytest.raises((ValueError, CollectorContinuityError)):
        _schedule(database, **values)


@pytest.mark.parametrize(
    ("field", "value"),
    [("python_executable", "/bin/echo"), ("registration_sha256", _sha("e"))],
)
def test_schedule_rejects_arbitrary_executable_and_registration_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    database, _ = _prepare_collector(tmp_path, monkeypatch)
    with pytest.raises((ValueError, CollectorContinuityError)):
        _schedule(database, **{field: value})


@pytest.mark.parametrize(
    "mutation",
    ["argv-reorder", "argv-extra", "self-hash", "wrong-session", "wrong-registration"],
)
def test_snapshot_rejects_forged_command_or_step_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    schedule = _schedule(database)
    spec = schedule[0]
    if mutation == "argv-reorder":
        command = tuple(reversed(spec.command))
        forged = replace(
            spec,
            command=command,
            command_sha256=canonical_json_sha256(
                {"schema_version": "stockdata-forward-collector-command/1", "argv": list(command)}
            ),
        )
    elif mutation == "argv-extra":
        command = spec.command + ("--extra",)
        forged = replace(
            spec,
            command=command,
            command_sha256=canonical_json_sha256(
                {"schema_version": "stockdata-forward-collector-command/1", "argv": list(command)}
            ),
        )
    elif mutation == "self-hash":
        forged = replace(spec, command_sha256=_sha("f"))
    elif mutation == "wrong-session":
        forged = replace(spec, session=_SESSIONS[1], step_ordinal=0)
    else:
        forged = replace(spec, registration_sha256=_sha("e"))

    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        with pytest.raises(CollectorContinuityError):
            _snapshot(connection, forged)


def test_prices_session_2_and_3_coverage_widening_keeps_complement_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    schedule = _schedule(database)
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        _insert_sync(connection, end=_SESSIONS[0])
        connection.commit()
        first = _snapshot(connection, schedule[3])

        connection.execute(
            "UPDATE sync_coverage SET end_date=? WHERE code=? AND source=? "
            "AND adjustment_mode=? AND adjustment_version=?",
            (
                _SESSIONS[1],
                _SYMBOLS[0],
                _SOURCE,
                _ADJUSTMENT_MODE,
                _ADJUSTMENT_VERSION,
            ),
        )
        connection.commit()
        second = _snapshot(connection, schedule[7])

        connection.execute(
            "UPDATE sync_coverage SET end_date=? WHERE code=? AND source=? "
            "AND adjustment_mode=? AND adjustment_version=?",
            (
                _SESSIONS[2],
                _SYMBOLS[0],
                _SOURCE,
                _ADJUSTMENT_MODE,
                _ADJUSTMENT_VERSION,
            ),
        )
        connection.commit()
        third = _snapshot(connection, schedule[11])

    complements = tuple(
        state["outside_scope_sha256"]["sync_coverage"]
        for state in (first, second, third)
    )
    assert complements[0] == complements[1] == complements[2]


@pytest.mark.parametrize("mutation", ["mode", "version", "cohort-start"])
def test_prices_foreign_identity_changes_coverage_complement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    spec = _schedule(database)[7]
    with open_exact_collector_sqlite(database_path=database, ledger_path=ledger) as (
        connection,
        _,
    ):
        _insert_sync(connection, end=spec.session)
        connection.commit()
        before = _snapshot(connection, spec)
        mode = "qfq" if mutation == "mode" else _ADJUSTMENT_MODE
        version = (
            "foreign-adjustment-v1"
            if mutation in {"version", "cohort-start"}
            else _ADJUSTMENT_VERSION
        )
        start = "2099-01-04" if mutation == "cohort-start" else _SESSIONS[0]
        connection.execute(
            "INSERT INTO sync_coverage "
            "(code,source,adjustment_mode,adjustment_version,start_date,end_date,retrieved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                _SYMBOLS[0],
                _SOURCE,
                mode,
                version,
                start,
                spec.session,
                "2099-01-06T08:00:01+08:00",
            ),
        )
        connection.commit()
        after = _snapshot(connection, spec)

    assert (
        after["outside_scope_sha256"]["sync_coverage"]
        != before["outside_scope_sha256"]["sync_coverage"]
    )


def test_attempt_events_require_nested_before_after_step_state_and_matching_hashes() -> None:
    builder = _api("build_collector_ledger_event")
    genesis = continuity.build_collector_genesis_ledger_event(
        {
            "schema_version": continuity.COLLECTOR_GENESIS_SCHEMA,
            "database_uuid": _sha("b"),
            "cohort_sha256": _sha("c"),
            "collector_schema_sha256": "9" * 64,
            "database_identity": {
                "schema_version": continuity.PHYSICAL_FILE_IDENTITY_SCHEMA,
                "canonical_path": "/tmp/task-2-4.sqlite",
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 3,
            },
            "ledger_identity": {
                "schema_version": continuity.PHYSICAL_FILE_IDENTITY_SCHEMA,
                "canonical_path": "/tmp/task-2-4.sqlite.collector-ledger.jsonl",
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 4,
            },
            "created_at": "2026-08-23T00:00:00+08:00",
        }
    )
    started = builder(
        previous_event=genesis,
        event_type="ATTEMPT_STARTED",
        event=_attempt_started_detail(),
    )
    completed = builder(
        previous_event=started,
        event_type="ATTEMPT_COMPLETED",
        event=_attempt_completed_detail(),
    )
    assert completed["event"]["step_state_before"]["collector_state_sha256"] == _sha("3")
    assert completed["event"]["step_state_after"]["collector_state_sha256"] == _sha("4")


@pytest.mark.parametrize("mutation", ["missing-before", "missing-after", "scope-drift", "aggregate-drift"])
def test_attempt_events_reject_nested_step_state_drift(mutation: str) -> None:
    builder = _api("build_collector_ledger_event")
    genesis = continuity.build_collector_genesis_ledger_event(
        {
            "schema_version": continuity.COLLECTOR_GENESIS_SCHEMA,
            "database_uuid": _sha("b"),
            "cohort_sha256": _sha("c"),
            "collector_schema_sha256": "9" * 64,
            "database_identity": {
                "schema_version": continuity.PHYSICAL_FILE_IDENTITY_SCHEMA,
                "canonical_path": "/tmp/task-2-4.sqlite",
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 3,
            },
            "ledger_identity": {
                "schema_version": continuity.PHYSICAL_FILE_IDENTITY_SCHEMA,
                "canonical_path": "/tmp/task-2-4.sqlite.collector-ledger.jsonl",
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 4,
            },
            "created_at": "2026-08-23T00:00:00+08:00",
        }
    )
    started = builder(
        previous_event=genesis,
        event_type="ATTEMPT_STARTED",
        event=_attempt_started_detail(),
    )
    completed = builder(
        previous_event=started,
        event_type="ATTEMPT_COMPLETED",
        event=_attempt_completed_detail(),
    )
    details = dict(completed["event"])
    if mutation == "missing-before":
        details.pop("step_state_before")
    elif mutation == "missing-after":
        details.pop("step_state_after")
    elif mutation == "scope-drift":
        details["step_state_after"] = {
            **details["step_state_after"],
            "outside_scope_sha256": {"collection_receipts": _sha("c")},
        }
    else:
        details["step_state_after"] = {
            **details["step_state_after"],
            "collector_state_sha256": _sha("9"),
        }
    mutated = _rehash_event({**completed, "event": details})
    with pytest.raises(CollectorContinuityError):
        continuity.validate_collector_ledger_event(mutated)
