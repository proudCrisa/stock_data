from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

import pytest

import stockdata.collector_continuity as continuity
from stockdata.collector_continuity import CollectorContinuityError
from stockdata.forward_context import _COUNT_URL, _PAGE_SIZE, _PAGE_URL, _status_values

from test_collector_step_state import (
    _ADJUSTMENT_MODE,
    _ADJUSTMENT_VERSION,
    _SESSIONS,
    _SYMBOLS,
    _bound_registration,
    _prepare_collector,
    _schedule,
)


_STARTED = "2099-01-05T08:40:00+08:00"
_FINISHED = "2099-01-05T09:20:00+08:00"
_POST_CLOSE_STARTED = "2099-01-05T15:10:00+08:00"
_POST_CLOSE_FINISHED = "2099-01-05T16:20:00+08:00"
_CONTEXT_SOURCE = "sina-market-center-hs-a-v1"
_ACTIONS_SOURCE = "baostock-query-dividend-data-v1"
_PRICE_SOURCE = "tencent"


def _api(name: str) -> Any:
    value = getattr(continuity, name, None)
    if value is None:
        pytest.fail(f"missing raw postcondition API: {name}")
    return value


@pytest.fixture(autouse=True)
def _forbid_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_provider(*args: object, **kwargs: object) -> object:
        raise AssertionError("raw postcondition tests must not call a provider")

    monkeypatch.setattr("stockdata.forward_context.requests.get", no_provider)
    monkeypatch.setattr(
        "stockdata.forward_corporate_actions.fetch_baostock_corporate_actions",
        no_provider,
    )
    monkeypatch.setattr("stockdata.fetch_tencent._http_get", no_provider)


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    database, ledger = _prepare_collector(tmp_path, monkeypatch)
    registration = _bound_registration(database)
    return {
        "database": database,
        "ledger": ledger,
        "registration": registration,
        "schedule": _schedule(database),
    }


def _canonical(value: object) -> str:
    return continuity.canonical_json_bytes(value).decode("ascii")


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _receipt(
    connection: sqlite3.Connection,
    *,
    receipt_id: int,
    observed_at: str,
    source: str,
    request: object,
    response: object,
    request_json: str | None = None,
    response_json: str | None = None,
) -> None:
    request_text = request_json if request_json is not None else _canonical(request)
    response_text = response_json if response_json is not None else _canonical(response)
    connection.execute(
        "INSERT INTO collection_receipts "
        "(receipt_id,observed_at,source,request_json,response_json,response_sha256,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            receipt_id,
            observed_at,
            source,
            request_text,
            response_text,
            _sha256(response_text),
            observed_at,
        ),
    )


def _symbol_wire(symbol: str) -> str:
    code, market = symbol.split(".")
    return market.lower() + code


def _context_rows(*, include_noncohort: bool = True) -> list[dict[str, object]]:
    rows = [
        {"symbol": _symbol_wire(symbol), "name": "Example", "trade": "10.0", "volume": 1000}
        for symbol in _SYMBOLS
    ]
    if include_noncohort:
        rows.append({"symbol": "sz999999", "name": "Outside", "trade": "2.0", "volume": 20})
    return rows


def _context_receipt(
    spec: object,
    *,
    rows: list[dict[str, object]] | None = None,
    observed_at: str = _STARTED,
    advertised_count: int | None = None,
    request: object | None = None,
) -> tuple[object, object]:
    raw_rows = rows if rows is not None else _context_rows()
    page = json.dumps(raw_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    response = {
        "advertised_count": len(raw_rows) if advertised_count is None else advertised_count,
        "count_raw": str(len(raw_rows) if advertised_count is None else advertised_count),
        "raw_pages": [page],
    }
    logical_request = (
        {
            "count_url": _COUNT_URL,
            "page_url": _PAGE_URL,
            "node": "hs_a",
            "page_size": _PAGE_SIZE,
        }
        if request is None
        else request
    )
    return logical_request, response


def _seed_context(
    connection: sqlite3.Connection,
    spec: object,
    *,
    include_noncohort: bool = True,
    receipt_id: int = 1,
    phase: str | None = None,
    observed_at: str | None = None,
) -> None:
    actual_phase = phase or str(spec.phase)
    actual_observed_at = observed_at or (
        _STARTED if actual_phase == "pre_open" else "2099-01-05T16:00:00+08:00"
    )
    request, response = _context_receipt(spec, observed_at=actual_observed_at)
    rows = _context_rows(include_noncohort=include_noncohort)
    _receipt(
        connection,
        receipt_id=receipt_id,
        observed_at=actual_observed_at,
        source=_CONTEXT_SOURCE,
        request=request,
        response=response,
    )
    if actual_phase == "pre_open":
        decision, outcome, finalized = actual_observed_at, None, None
    else:
        decision, outcome, finalized = None, actual_observed_at, actual_observed_at
    connection.execute(
        "INSERT INTO forward_context_observations VALUES (?,?,?,?,?,?,?)",
        (spec.session, actual_phase, decision, outcome, finalized, _CONTEXT_SOURCE, receipt_id),
    )
    connection.executemany(
        "INSERT INTO forward_universe_observations VALUES (?,?,?,?,?,?)",
        [
            (spec.session, actual_phase, _symbol_normalized(row["symbol"]), 1, _CONTEXT_SOURCE, receipt_id)
            for row in rows
        ],
    )
    connection.executemany(
        "INSERT INTO forward_status_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                spec.session,
                actual_phase,
                symbol,
                *_status_values(
                    symbol,
                    next(
                        row
                        for row in _context_rows()
                        if _symbol_normalized(row["symbol"]) == symbol
                    ),
                ),
                _CONTEXT_SOURCE,
                receipt_id,
            )
            for symbol in _SYMBOLS
        ],
    )


def _symbol_normalized(value: object) -> str:
    raw = str(value).lower()
    return f"{raw[2:]}.{raw[:2].upper()}"


def _action_batches(*, positive: bool = True, years: tuple[int, int] = (2098, 2099)) -> dict[str, list[dict[str, object]]]:
    fields = ["dividOperateDate", "dividPlanAnnounceDate", "dividCashPsBeforeTax"]
    result: dict[str, list[dict[str, object]]] = {}
    for index, symbol in enumerate(_SYMBOLS):
        first_rows = [["2099-01-03", "2098-12-01", "0.1"]] if positive and index == 0 else []
        result[symbol] = [
            {"year": years[0], "fields": fields, "rows": first_rows},
            {"year": years[1], "fields": fields, "rows": []},
        ]
    return result


def _seed_actions(
    connection: sqlite3.Connection,
    spec: object,
    *,
    batches: dict[str, list[dict[str, object]]] | None = None,
    receipt_id: int = 1,
    observed_at: str = _STARTED,
) -> None:
    source_batches = batches if batches is not None else _action_batches()
    request = {
        "symbols": list(_SYMBOLS),
        "observation_date": spec.session,
        "years": [int(spec.session[:4]) - 1, int(spec.session[:4])],
    }
    response = {"symbols": source_batches}
    _receipt(
        connection,
        receipt_id=receipt_id,
        observed_at=observed_at,
        source=_ACTIONS_SOURCE,
        request=request,
        response=response,
    )
    for symbol, symbol_batches in source_batches.items():
        event_rows = []
        for batch in symbol_batches:
            for row in batch.get("rows", []):
                payload = dict(zip(batch["fields"], row))
                payload_json = _canonical(payload)
                event_rows.append(
                    (
                        spec.session,
                        symbol,
                        _sha256(payload_json),
                        payload.get("dividOperateDate"),
                        payload.get("dividPlanAnnounceDate"),
                        payload_json,
                        observed_at,
                        _ACTIONS_SOURCE,
                        receipt_id,
                    )
                )
        connection.execute(
            "INSERT INTO forward_corporate_action_coverage VALUES (?,?,?,?,?,?)",
            (spec.session, symbol, observed_at, _ACTIONS_SOURCE, receipt_id, len(event_rows)),
        )
        if event_rows:
            connection.executemany(
                "INSERT INTO forward_corporate_actions VALUES (?,?,?,?,?,?,?,?,?)", event_rows
            )


def _tencent_raw(symbol: str, day: str) -> str:
    market = symbol.split(".")[1].lower()
    code = symbol.split(".")[0]
    parts = [""] * 45
    parts[1] = "Example"
    parts[2] = code
    parts[3] = "10.0"
    parts[4] = "9.0"
    parts[5] = "9.5"
    parts[6] = "100"
    parts[30] = day.replace("-", "") + "160000"
    parts[33] = "10.5"
    parts[34] = "9.0"
    return f'v_{market}{code}="{"~".join(parts)}";'


def _seed_prices(
    connection: sqlite3.Connection,
    spec: object,
    *,
    symbols: tuple[str, ...] = _SYMBOLS,
    receipt_start: int = 1,
    observed_at: str = "2099-01-05T16:00:00+08:00",
    coverage_end: str | None = None,
    receipt_start_date: str | None = None,
    include_coverage: bool = True,
) -> None:
    end = coverage_end or str(spec.session)
    for offset, symbol in enumerate(symbols):
        receipt_id = receipt_start + offset
        raw = _tencent_raw(symbol, str(spec.session))
        request = {
            "method": "qt",
            "url": f"https://qt.gtimg.cn/q={symbol.split('.')[1].lower()}{symbol.split('.')[0]}",
            "start_date": receipt_start_date or _SESSIONS[0],
            "end_date": str(spec.session),
        }
        response = {
            "raw": raw,
            "fields": "date,open,high,low,close,volume",
            "rows": [[str(spec.session), "9.5", "10.5", "9.0", "10.0", "10000.0"]],
        }
        _receipt(
            connection,
            receipt_id=receipt_id,
            observed_at=observed_at,
            source=_PRICE_SOURCE,
            request=request,
            response=response,
        )
        connection.execute(
            "INSERT INTO daily (code,date,open,high,low,close,volume,source,adjustment_mode,"
            "adjustment_version,retrieved_at,is_final,receipt_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                symbol,
                spec.session,
                9.5,
                10.5,
                9.0,
                10.0,
                10000.0,
                _PRICE_SOURCE,
                _ADJUSTMENT_MODE,
                _ADJUSTMENT_VERSION,
                observed_at,
                1,
                receipt_id,
            ),
        )
        if include_coverage:
            connection.execute(
                "INSERT INTO sync_coverage VALUES (?,?,?,?,?,?,?)",
                (
                    symbol,
                    _PRICE_SOURCE,
                    _ADJUSTMENT_MODE,
                    _ADJUSTMENT_VERSION,
                    _SESSIONS[0],
                    end,
                    observed_at,
                ),
            )


def _insert_price_coverage(
    connection: sqlite3.Connection,
    spec: object,
    *,
    start: str = _SESSIONS[0],
    end: str | None = None,
    retrieved_at: str = "2099-01-05T16:00:00+08:00",
) -> None:
    connection.executemany(
        "INSERT INTO sync_coverage VALUES (?,?,?,?,?,?,?)",
        [
            (
                symbol,
                _PRICE_SOURCE,
                _ADJUSTMENT_MODE,
                _ADJUSTMENT_VERSION,
                start,
                end or str(spec.session),
                retrieved_at,
            )
            for symbol in _SYMBOLS
        ],
    )


def _open(prepared: dict[str, object]):
    return continuity.open_exact_collector_sqlite(
        database_path=prepared["database"],
        ledger_path=prepared["ledger"],
    )


def _disable_trigger(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM main.sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    assert row is not None, name
    sql = str(row[0])
    connection.execute(f'DROP TRIGGER "{name}"')
    return sql


def _restore_trigger(connection: sqlite3.Connection, sql: str) -> None:
    connection.execute(sql)


def _rewrite_receipt_response(
    connection: sqlite3.Connection, receipt_id: int, response: object
) -> None:
    trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
    response_json = _canonical(response)
    connection.execute(
        "UPDATE collection_receipts SET response_json=?, response_sha256=? "
        "WHERE receipt_id=?",
        (response_json, _sha256(response_json), receipt_id),
    )
    _restore_trigger(connection, trigger_sql)


def _rewrite_receipt_created_at(
    connection: sqlite3.Connection, receipt_id: int, created_at: str
) -> None:
    trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
    connection.execute(
        "UPDATE collection_receipts SET created_at=? WHERE receipt_id=?",
        (created_at, receipt_id),
    )
    _restore_trigger(connection, trigger_sql)


def _mutate_coverage(
    connection: sqlite3.Connection, mutation: str, session: str
) -> None:
    if mutation == "delete":
        trigger_names = ("collector_sync_coverage_no_delete",)
        sql = "DELETE FROM sync_coverage WHERE code=?"
        params: tuple[object, ...] = (_SYMBOLS[0],)
    else:
        trigger_names = (
            "collector_sync_coverage_no_update",
            "sync_coverage_exact_noop",
        )
        values = {
            "shrink": (_SESSIONS[0], _SESSIONS[0]),
            "start": (_SESSIONS[1], session),
            "retimestamp": (_SESSIONS[0], session, "2099-01-07T16:30:00+08:00"),
            "mode": (_SESSIONS[0], session, "qfq", _ADJUSTMENT_VERSION),
            "version": (_SESSIONS[0], session, _ADJUSTMENT_MODE, "foreign-v1"),
        }[mutation]
        if mutation == "retimestamp":
            sql = "UPDATE sync_coverage SET start_date=?, end_date=?, retrieved_at=? WHERE code=?"
            params = (*values, _SYMBOLS[0])
        elif mutation == "mode":
            sql = "UPDATE sync_coverage SET start_date=?, end_date=?, adjustment_mode=?, adjustment_version=? WHERE code=?"
            params = (*values, _SYMBOLS[0])
        elif mutation == "version":
            sql = "UPDATE sync_coverage SET start_date=?, end_date=?, adjustment_mode=?, adjustment_version=? WHERE code=?"
            params = (*values, _SYMBOLS[0])
        else:
            sql = "UPDATE sync_coverage SET start_date=?, end_date=? WHERE code=?"
            params = (*values, _SYMBOLS[0])
    disabled: list[str] = []
    try:
        for name in trigger_names:
            disabled.append(_disable_trigger(connection, name))
        connection.execute(sql, params)
    finally:
        for trigger_sql in reversed(disabled):
            _restore_trigger(connection, trigger_sql)


@pytest.mark.parametrize("kind", ["byte_copy", "other_database"])
def test_raw_rejects_connection_not_bound_to_registration(
    prepared: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    spec = prepared["schedule"][0]
    if kind == "byte_copy":
        copied = tmp_path / "collector-copy.sqlite"
        shutil.copy2(prepared["database"], copied)
        wrong_database = copied
    else:
        other_root = tmp_path / "other"
        other_root.mkdir()
        wrong_database, _ = _prepare_collector(other_root, monkeypatch)
    connection = sqlite3.connect(str(wrong_database))
    try:
        with pytest.raises(CollectorContinuityError):
            continuity.capture_collector_step_baseline(connection, spec)
    finally:
        connection.close()


def test_aba_connection_with_same_bytes_b_inode_is_rejected_by_all_snapshots(
    prepared: dict[str, object], tmp_path: Path
) -> None:
    """A path/bytes match cannot substitute for the registered database inode."""
    spec = prepared["schedule"][0]
    database = Path(prepared["database"])
    registered_copy = tmp_path / "registered-a.sqlite"
    foreign_copy = tmp_path / "same-bytes-b.sqlite"
    relocated_connection_file = tmp_path / "opened-b.sqlite"
    shutil.copyfile(database, registered_copy)
    shutil.copyfile(database, foreign_copy)
    registered_inode = os.stat(database).st_ino
    foreign_inode = os.stat(foreign_copy).st_ino
    assert registered_inode != foreign_inode

    os.replace(database, registered_copy)
    os.replace(foreign_copy, database)
    connection = sqlite3.connect(str(database))
    try:
        # The handle is opened on B, then the registered A inode is restored at
        # the same path. The database bytes are intentionally identical.
        os.replace(database, relocated_connection_file)
        assert os.stat(relocated_connection_file).st_ino == foreign_inode
        os.replace(registered_copy, database)
        assert os.stat(database).st_ino == registered_inode
        with pytest.raises(CollectorContinuityError):
            continuity.capture_collector_step_baseline(connection, spec)
        with pytest.raises(CollectorContinuityError):
            continuity.snapshot_collector_step_state(connection, spec)
    finally:
        connection.close()


def test_price_retry_rejects_malformed_existing_receipt_during_coverage_repair(
    prepared: dict[str, object]
) -> None:
    spec = prepared["schedule"][7]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_prices(
            connection,
            spec,
            coverage_end=_SESSIONS[0],
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        _rewrite_receipt_response(connection, 1, "malformed")
        connection.commit()
        connection.execute(
            "UPDATE sync_coverage SET end_date=? WHERE code=?",
            (spec.session, _SYMBOLS[0]),
        )
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
    _assert_forbidden(result)


@pytest.mark.parametrize(
    ("receipt_start", "expected"),
    [(_SESSIONS[0], "complete"), (_SESSIONS[1], "forbidden")],
)
def test_price_coverage_only_without_baseline_coverage_requires_cohort_start_receipt(
    prepared: dict[str, object], receipt_start: str, expected: str
) -> None:
    spec = prepared["schedule"][7]
    with _open(prepared) as (connection, _):
        _seed_prices(
            connection,
            spec,
            include_coverage=False,
            receipt_start_date=receipt_start,
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        connection.commit()
        baseline = _baseline(connection, spec)
        _insert_price_coverage(connection, spec)
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
    if expected == "complete":
        assert _raw_class(result) == "complete"
    else:
        _assert_forbidden(result)


@pytest.mark.parametrize(
    ("receipt_start", "expected"),
    [(_SESSIONS[1], "complete"), (_SESSIONS[0], "forbidden")],
)
def test_price_coverage_only_with_baseline_coverage_requires_right_side_gap_receipt(
    prepared: dict[str, object], receipt_start: str, expected: str
) -> None:
    spec = prepared["schedule"][7]
    with _open(prepared) as (connection, _):
        _seed_prices(
            connection,
            spec,
            coverage_end=_SESSIONS[0],
            receipt_start_date=receipt_start,
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        connection.commit()
        baseline = _baseline(connection, spec)
        connection.execute(
            "UPDATE sync_coverage SET end_date=?",
            (str(spec.session),),
        )
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
    if expected == "complete":
        assert _raw_class(result) == "complete"
    else:
        _assert_forbidden(result)


@pytest.mark.parametrize(
    "mutation", ["delete", "shrink", "retimestamp", "start", "mode", "version"]
)
def test_price_coverage_non_monotonic_or_identity_drift_is_forbidden(
    prepared: dict[str, object], mutation: str
) -> None:
    spec = prepared["schedule"][7]
    with _open(prepared) as (connection, _):
        _seed_prices(
            connection,
            spec,
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        connection.commit()
        baseline = _baseline(connection, spec)
        _mutate_coverage(connection, mutation, spec.session)
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
    _assert_forbidden(result)


@pytest.mark.parametrize(
    ("observed_at", "created_at"),
    [
        ("2099-01-05T09:30:00+08:00", "2099-01-05T09:30:00+08:00"),
        (_STARTED, "2099-01-05T08:30:00+08:00"),
        (_STARTED, "2099-01-05T09:30:00+08:00"),
    ],
)
def test_raw_receipt_observed_and_created_times_are_phase_ordered(
    prepared: dict[str, object], observed_at: str, created_at: str
) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_context(connection, spec, observed_at=observed_at)
        _rewrite_receipt_created_at(connection, 1, created_at)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


def test_price_raw_rejects_tencent_response_with_wrong_v_symbol_prefix(
    prepared: dict[str, object]
) -> None:
    spec = prepared["schedule"][7]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_prices(
            connection,
            spec,
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        row = connection.execute(
            "SELECT response_json FROM collection_receipts WHERE receipt_id=1"
        ).fetchone()
        assert row is not None
        response = json.loads(row[0])
        response["raw"] = response["raw"].replace("v_sz000001", "v_sz999999", 1)
        _rewrite_receipt_response(connection, 1, response)
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
    _assert_forbidden(result)


@pytest.mark.parametrize("mutation", ["alias_duplicate", "extra_symbol"])
def test_actions_raw_rejects_alias_duplicate_or_extra_canonical_key(
    prepared: dict[str, object], mutation: str
) -> None:
    spec = prepared["schedule"][1]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_actions(connection, spec)
        row = connection.execute(
            "SELECT response_json FROM collection_receipts WHERE receipt_id=1"
        ).fetchone()
        assert row is not None
        response = json.loads(row[0])
        symbols = response["symbols"]
        if mutation == "alias_duplicate":
            symbols["sz000001"] = symbols["000001.SZ"]
        else:
            symbols["999999.SZ"] = symbols["000001.SZ"]
        _rewrite_receipt_response(connection, 1, response)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


def test_actions_raw_rejects_duplicate_event_when_persisted_multiplicity_is_lower(
    prepared: dict[str, object]
) -> None:
    spec = prepared["schedule"][1]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        batches = _action_batches()
        _seed_actions(connection, spec, batches=batches)
        row = connection.execute(
            "SELECT response_json FROM collection_receipts WHERE receipt_id=1"
        ).fetchone()
        assert row is not None
        response = json.loads(row[0])
        symbol_rows = response["symbols"][_SYMBOLS[0]][0]["rows"]
        assert len(symbol_rows) == 1
        symbol_rows.append(list(symbol_rows[0]))
        _rewrite_receipt_response(connection, 1, response)
        trigger_sql = _disable_trigger(
            connection, "forward_corporate_action_coverage_no_update"
        )
        connection.execute(
            "UPDATE forward_corporate_action_coverage SET event_count=2 "
            "WHERE observation_date=? AND symbol=?",
            (spec.session, _SYMBOLS[0]),
        )
        _restore_trigger(connection, trigger_sql)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


def _baseline(connection: sqlite3.Connection, spec: object) -> object:
    del connection
    with _api("open_registered_collector_read_connection")(spec) as read_connection:
        return _api("capture_collector_step_baseline")(read_connection, spec)


def _verify(
    connection: sqlite3.Connection,
    spec: object,
    baseline: object,
    *,
    started_at: str = _STARTED,
    finished_at: str = _FINISHED,
) -> object:
    del connection
    with _api("open_registered_collector_read_connection")(spec) as read_connection:
        return _api("verify_collector_raw_postcondition")(
            read_connection,
            spec,
            baseline,
            attempt_started_at=started_at,
            attempt_finished_at=finished_at,
        )


def _seed_for_step(connection: sqlite3.Connection, spec: object, **kwargs: object) -> None:
    if spec.step_id in {"pre_open_context", "post_close_context"}:
        _seed_context(connection, spec, **kwargs)
    elif spec.step_id == "pre_open_corporate_actions":
        _seed_actions(connection, spec, **kwargs)
    else:
        _seed_prices(connection, spec, **kwargs)


def _raw_class(result: object) -> str:
    return str(_field(result, "raw_class"))


def _field(result: object, name: str) -> object:
    if hasattr(result, name):
        return getattr(result, name)
    return result[name]  # type: ignore[index]


def _assert_forbidden(result: object) -> None:
    assert _raw_class(result) == "forbidden"
    assert _field(result, "retryable") is False
    assert _field(result, "code")


def test_raw_postcondition_surface_is_explicit() -> None:
    for name in (
        "CollectorStepBaseline",
        "CollectorRawPostconditionResult",
        "capture_collector_step_baseline",
        "verify_collector_raw_postcondition",
        "evaluate_collector_step_attempt",
        "open_registered_collector_read_connection",
        "require_bound_collector_read_connection",
    ):
        assert getattr(continuity, name, None) is not None, name


def test_connection_identity_implementation_has_no_private_sqlite_abi_dependency() -> None:
    source = Path(continuity.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import ctypes",
        "ctypes.",
        "import _sqlite3",
        "_sqlite3",
        "sqlite3_file",
        "sqlite3_vfs",
        "FILE_POINTER",
        "_PYSQLITE_DATABASE_OFFSET",
        "PyObject",
        "ob_type",
        "from_address",
        "sys.implementation.name",
    )
    assert not [token for token in forbidden if token in source]


def test_spec_forge_and_registration_replacement_fail_closed(prepared: dict[str, object]) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        forged = replace(spec, command=tuple(spec.command) + ("--forged",))
        with pytest.raises(CollectorContinuityError):
            _verify(connection, forged, baseline)

    registration = Path(prepared["registration"])
    original = registration.read_bytes()
    replacement = json.loads(original)
    replacement["status"] = "REPLACED"
    registration.write_bytes(_canonical(replacement).encode("ascii"))
    try:
        with _open(prepared) as (connection, _):
            with pytest.raises(CollectorContinuityError):
                _baseline(connection, spec)
    finally:
        registration.write_bytes(original)


@pytest.mark.parametrize("kind", ["disallowed", "complement", "highwater", "old_receipt"])
def test_baseline_after_state_and_receipt_drift_is_forbidden(
    prepared: dict[str, object], kind: str
) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        if kind == "old_receipt":
            _seed_context(connection, spec)
            connection.commit()
        baseline = _baseline(connection, spec)
        if kind == "disallowed":
            connection.execute(
                "INSERT INTO daily (code,date,source,adjustment_mode,adjustment_version,retrieved_at,is_final) "
                "VALUES (?,?,?,?,?,?,?)",
                ("999999.SZ", spec.session, _PRICE_SOURCE, _ADJUSTMENT_MODE, _ADJUSTMENT_VERSION, _STARTED, 1),
            )
        elif kind == "complement":
            _seed_context(connection, spec)
            connection.execute(
                "INSERT INTO forward_universe_observations VALUES (?,?,?,?,?,?)",
                (spec.session, spec.phase, "999998.SZ", 1, _CONTEXT_SOURCE, 1),
            )
        elif kind == "highwater":
            _receipt(
                connection,
                receipt_id=99,
                observed_at=_STARTED,
                source=_CONTEXT_SOURCE,
                request={"node": "hs_a"},
                response={"advertised_count": 0, "raw_pages": []},
            )
        else:
            trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
            connection.execute(
                "UPDATE collection_receipts SET response_json=? WHERE receipt_id=1",
                (_canonical({"changed": True}),),
            )
            _restore_trigger(connection, trigger_sql)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


@pytest.mark.parametrize("drift", ["disallowed-table", "selector-complement"])
def test_raw_verifier_rejects_real_drift_when_aggregate_and_counts_collide(
    prepared: dict[str, object], drift: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        if drift == "selector-complement":
            _seed_context(connection, spec)
            connection.commit()
        baseline = _baseline(connection, spec)
        if drift == "disallowed-table":
            connection.execute(
                "INSERT INTO daily "
                "(code,date,source,adjustment_mode,adjustment_version,retrieved_at,is_final) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "999999.SZ",
                    spec.session,
                    _PRICE_SOURCE,
                    _ADJUSTMENT_MODE,
                    _ADJUSTMENT_VERSION,
                    _STARTED,
                    1,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO forward_universe_observations VALUES (?,?,?,?,?,?)",
                (
                    _SESSIONS[1],
                    spec.phase,
                    "999998.SZ",
                    1,
                    _CONTEXT_SOURCE,
                    1,
                ),
            )
        connection.commit()

        class FixedDigest:
            def hexdigest(self) -> str:
                return baseline.step_state["collector_state_sha256"]

        monkeypatch.setattr(
            continuity,
            "_compute_collector_logical_digest",
            lambda _connection: (
                FixedDigest(),
                deepcopy(baseline.step_state["table_counts"]),
            ),
        )
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


@pytest.mark.parametrize("field", ["request_json", "response_json", "response_sha256", "observed_at"])
def test_receipt_canonical_json_hash_and_timestamp_drift_is_rejected(
    prepared: dict[str, object], field: str
) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_context(connection, spec)
        trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
        if field == "request_json":
            value = '{"node":"hs_a","node":"hs_a"}'
        elif field == "response_json":
            value = '{"advertised_count":0,"raw_pages":[]}'
        elif field == "response_sha256":
            value = "0" * 64
        else:
            value = "2099-01-05T10:00:00+08:00"
        connection.execute(f"UPDATE collection_receipts SET {field}=? WHERE receipt_id=1", (value,))
        _restore_trigger(connection, trigger_sql)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


@pytest.mark.parametrize("mutation", ["orphan", "foreign", "mixed"])
def test_context_orphan_foreign_and_mixed_receipts_fail_closed(
    prepared: dict[str, object], mutation: str
) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_context(connection, spec)
        if mutation == "orphan":
            _receipt(
                connection,
                receipt_id=90,
                observed_at=_STARTED,
                source=_CONTEXT_SOURCE,
                request={"node": "hs_a"},
                response={"advertised_count": 0, "count_raw": "0", "raw_pages": []},
            )
        else:
            _receipt(
                connection,
                receipt_id=90,
                observed_at=_STARTED,
                source=_CONTEXT_SOURCE if mutation == "foreign" else "foreign-source",
                request={"node": "hs_a"},
                response={"advertised_count": 0, "count_raw": "0", "raw_pages": []},
            )
            trigger_sql = _disable_trigger(connection, "forward_universe_observations_no_update")
            connection.execute(
                "UPDATE forward_universe_observations SET receipt_id=90 "
                "WHERE effective_date=? AND observation_phase=? AND symbol=? AND source=?",
                (spec.session, spec.phase, _SYMBOLS[0], _CONTEXT_SOURCE),
            )
            _restore_trigger(connection, trigger_sql)
        connection.commit()
        result = _verify(connection, spec, baseline)
    _assert_forbidden(result)


@pytest.mark.parametrize("case", ["complete", "unchanged", "missing_universe", "missing_status", "wrong_phase", "advertised_mismatch", "full_market", "orphan"])
def test_context_raw_contract(case: str, prepared: dict[str, object]) -> None:
    spec = prepared["schedule"][0]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        if case not in {
            "unchanged",
            "missing_universe",
            "missing_status",
            "wrong_phase",
            "advertised_mismatch",
            "full_market",
        }:
            _seed_context(connection, spec, include_noncohort=True)
        elif case == "full_market":
            _seed_context(connection, spec, include_noncohort=True)
        elif case == "missing_universe":
            _seed_context(connection, spec)
            trigger_sql = _disable_trigger(connection, "forward_universe_observations_no_delete")
            connection.execute("DELETE FROM forward_universe_observations")
            _restore_trigger(connection, trigger_sql)
        elif case == "missing_status":
            _seed_context(connection, spec)
            trigger_sql = _disable_trigger(connection, "forward_status_observations_no_delete")
            connection.execute("DELETE FROM forward_status_observations")
            _restore_trigger(connection, trigger_sql)
        elif case == "advertised_mismatch":
            request, response = _context_receipt(spec, advertised_count=1)
            _receipt(
                connection,
                receipt_id=1,
                observed_at=_STARTED,
                source=_CONTEXT_SOURCE,
                request=request,
                response=response,
            )
        if case == "wrong_phase":
            _seed_context(connection, spec, phase="post_close", observed_at="2099-01-05T16:00:00+08:00")
        if case == "orphan":
            _receipt(
                connection,
                receipt_id=91,
                observed_at=_STARTED,
                source=_CONTEXT_SOURCE,
                request={"node": "hs_a"},
                response={"advertised_count": 1, "raw_pages": ["[]"]},
            )
        connection.commit()
        if case == "unchanged":
            result = _verify(connection, spec, baseline)
            assert _raw_class(result) == "unchanged"
            assert _field(result, "retryable") is True
        else:
            result = _verify(connection, spec, baseline)
            if case in {"complete", "full_market"}:
                assert _raw_class(result) == "complete"
                assert _field(result, "retryable") is False
            else:
                _assert_forbidden(result)


@pytest.mark.parametrize("case", ["positive_zero", "missing_symbol", "wrong_years", "malformed_batch", "event_drift"])
def test_corporate_action_raw_contract(case: str, prepared: dict[str, object]) -> None:
    spec = prepared["schedule"][1]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        batches = _action_batches()
        if case == "missing_symbol":
            batches.pop(_SYMBOLS[-1])
        elif case == "wrong_years":
            batches = _action_batches(years=(2097, 2098))
        _seed_actions(connection, spec, batches=batches)
        if case == "malformed_batch":
            trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
            connection.execute(
                "UPDATE collection_receipts SET response_json=? WHERE receipt_id=1",
                ("{\"symbols\":[]}",),
            )
            _restore_trigger(connection, trigger_sql)
        elif case == "event_drift":
            trigger_sql = _disable_trigger(connection, "forward_corporate_actions_no_update")
            connection.execute(
                "UPDATE forward_corporate_actions SET payload_json=? WHERE receipt_id=1",
                (_canonical({"dividCashPsBeforeTax": "9.9"}),),
            )
            _restore_trigger(connection, trigger_sql)
        connection.commit()
        result = _verify(connection, spec, baseline)
    if case == "positive_zero":
        assert _raw_class(result) == "complete"
        assert _field(result, "retryable") is False
    else:
        _assert_forbidden(result)


@pytest.mark.parametrize("case", ["complete", "partial", "unchanged", "retry_duplicate", "coverage_only", "orphan", "foreign_date", "foreign_symbol", "foreign_version", "malformed", "empty", "mutation"])
def test_price_raw_contract(case: str, prepared: dict[str, object]) -> None:
    spec = prepared["schedule"][3]
    with _open(prepared) as (connection, _):
        if case == "retry_duplicate":
            _seed_prices(connection, spec)
            connection.commit()
        baseline = _baseline(connection, spec)
        if case == "partial":
            _seed_prices(connection, spec, symbols=_SYMBOLS[:3])
        elif case == "unchanged":
            pass
        elif case == "coverage_only":
            connection.executemany(
                "INSERT INTO sync_coverage VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        symbol,
                        _PRICE_SOURCE,
                        _ADJUSTMENT_MODE,
                        _ADJUSTMENT_VERSION,
                        _SESSIONS[0],
                        _SESSIONS[0],
                        "2099-01-05T16:00:00+08:00",
                    )
                    for symbol in _SYMBOLS
                ],
            )
        elif case == "empty":
            _receipt(
                connection,
                receipt_id=1,
                observed_at="2099-01-05T16:00:00+08:00",
                source=_PRICE_SOURCE,
                request={"method": "qt", "url": "https://qt.gtimg.cn/q=sh000001", "start_date": _SESSIONS[0], "end_date": spec.session},
                response={"raw": "", "fields": "date,open,high,low,close,volume", "rows": []},
            )
        elif case == "retry_duplicate":
            pass
        else:
            _seed_prices(connection, spec)
            if case == "orphan":
                _receipt(
                    connection,
                    receipt_id=99,
                    observed_at="2099-01-05T16:00:00+08:00",
                    source=_PRICE_SOURCE,
                    request={"method": "qt"},
                    response={"raw": "", "rows": []},
                )
            elif case == "foreign_date":
                trigger_sql = _disable_trigger(connection, "collector_daily_final_no_update")
                connection.execute("UPDATE daily SET date='2099-01-06' WHERE code=?", (_SYMBOLS[0],))
                _restore_trigger(connection, trigger_sql)
            elif case == "foreign_symbol":
                trigger_sql = _disable_trigger(connection, "collector_daily_final_no_update")
                connection.execute("UPDATE daily SET code='999999.SZ' WHERE code=?", (_SYMBOLS[0],))
                _restore_trigger(connection, trigger_sql)
            elif case == "foreign_version":
                trigger_sql = _disable_trigger(connection, "collector_daily_final_no_update")
                connection.execute("UPDATE daily SET adjustment_version='foreign-v1' WHERE code=?", (_SYMBOLS[0],))
                _restore_trigger(connection, trigger_sql)
            elif case == "malformed":
                trigger_sql = _disable_trigger(connection, "collection_receipts_no_update")
                connection.execute("UPDATE collection_receipts SET response_json=? WHERE receipt_id=1", ("{bad",))
                _restore_trigger(connection, trigger_sql)
            elif case == "mutation":
                trigger_sql = _disable_trigger(connection, "collector_daily_final_no_update")
                connection.execute("UPDATE daily SET close=99.0 WHERE code=?", (_SYMBOLS[0],))
                _restore_trigger(connection, trigger_sql)
            connection.commit()
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=_POST_CLOSE_STARTED,
            finished_at=_POST_CLOSE_FINISHED,
        )
    if case == "partial":
        assert _raw_class(result) == "partial_prices"
        assert _field(result, "retryable") is True
    elif case == "complete":
        assert _raw_class(result) == "complete"
        assert _field(result, "retryable") is False
    elif case == "unchanged":
        assert _raw_class(result) == "unchanged"
        assert _field(result, "retryable") is True
    elif case == "coverage_only":
        _assert_forbidden(result)
    elif case == "retry_duplicate":
        assert _raw_class(result) == "unchanged"
        assert _field(result, "retryable") is True
        assert _field(result, "new_receipt_ids") == ()
    else:
        _assert_forbidden(result)


def test_exact_price_retry_does_not_duplicate_receipts(prepared: dict[str, object]) -> None:
    spec = prepared["schedule"][3]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        before = int(connection.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0])
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=_POST_CLOSE_STARTED,
            finished_at=_POST_CLOSE_FINISHED,
        )
        after = int(connection.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0])
    assert _raw_class(result) == "unchanged"
    assert before == after


def test_last_symbol_coverage_only_repair_is_complete_and_terminal(
    prepared: dict[str, object]
) -> None:
    spec = prepared["schedule"][7]
    evaluate = _api("evaluate_collector_step_attempt")
    with _open(prepared) as (connection, _):
        _seed_prices(
            connection,
            spec,
            coverage_end=_SESSIONS[0],
            receipt_start_date=_SESSIONS[1],
            observed_at=f"{spec.session}T16:00:00+08:00",
        )
        connection.commit()
        baseline = _baseline(connection, spec)
        connection.execute(
            "UPDATE sync_coverage SET end_date=? WHERE code!=?",
            (spec.session, _SYMBOLS[-1]),
        )
        connection.commit()
        partial = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
        assert _raw_class(partial) == "partial_prices"
        assert _field(partial, "missing_symbols") == (_SYMBOLS[-1],)
        assert _field(partial, "retryable") is True
        assert evaluate(partial, returncode=0) == "retryable_failure"

        connection.execute(
            "UPDATE sync_coverage SET end_date=? WHERE code=?",
            (spec.session, _SYMBOLS[-1]),
        )
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
        assert _raw_class(result) == "complete"
        assert _field(result, "missing_symbols") == ()
        assert evaluate(result, returncode=0) == "complete"

        next_baseline = _baseline(connection, spec)
        unchanged = _verify(
            connection,
            spec,
            next_baseline,
            started_at=f"{spec.session}T15:10:00+08:00",
            finished_at=f"{spec.session}T16:20:00+08:00",
        )
        assert _raw_class(unchanged) == "unchanged"
        assert _field(result, "retryable") is False


@pytest.mark.parametrize(
    ("returncode", "expected"),
    ((0, "complete"), (1, "nonretryable_failure"), (-1, "nonretryable_failure"), (True, "reject"), (False, "reject"), ("0", "reject"), (None, "reject")),
)
def test_returncode_matrix(
    prepared: dict[str, object], returncode: object, expected: str
) -> None:
    spec = prepared["schedule"][3]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        _seed_prices(connection, spec)
        connection.commit()
        result = _verify(
            connection,
            spec,
            baseline,
            started_at=_POST_CLOSE_STARTED,
            finished_at=_POST_CLOSE_FINISHED,
        )
    evaluate = _api("evaluate_collector_step_attempt")
    if expected == "reject":
        with pytest.raises(CollectorContinuityError):
            evaluate(result, returncode=returncode)
    else:
        assert evaluate(result, returncode=returncode) == expected


@pytest.mark.parametrize("step_index", [0, 1, 2, 3])
def test_raw_verifier_rechecks_registration_schedule_each_entry(
    prepared: dict[str, object], step_index: int
) -> None:
    spec = prepared["schedule"][step_index]
    with _open(prepared) as (connection, _):
        baseline = _baseline(connection, spec)
        forged = replace(spec, registration_sha256="e" * 64)
        with pytest.raises(CollectorContinuityError):
            _verify(connection, forged, baseline)
