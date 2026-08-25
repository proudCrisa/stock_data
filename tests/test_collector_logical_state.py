from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from typing import Any

import pytest

import stockdata.collector_continuity as continuity


_DOMAIN = b"stockdata-forward-collector-logical-state/1\x00collector_state_sha256\x00"
_UUID = "1" * 64
_COHORT_SHA = "2" * 64
_LEDGER_SHA = "3" * 64
_CREATED_AT = "2026-08-01T12:00:00+08:00"
_IDENTITY = {
    "schema_version": continuity.PHYSICAL_FILE_IDENTITY_SCHEMA,
    "canonical_path": "/fixture/collector.sqlite",
    "parent_st_dev": 11,
    "parent_st_ino": 12,
    "file_st_dev": 13,
    "file_st_ino": 14,
}

_TABLES = (
    (
        "collection_receipts",
        ("receipt_id", "observed_at", "source", "request_json", "response_json", "response_sha256", "created_at"),
        ("receipt_id",),
    ),
    (
        "daily",
        ("code", "date", "open", "high", "low", "close", "volume", "source", "adjustment_mode", "adjustment_version", "retrieved_at", "is_final", "receipt_id"),
        ("code", "date", "source", "adjustment_mode", "adjustment_version"),
    ),
    (
        "forward_capture_cohort",
        ("singleton", "spec_json", "spec_sha256", "created_at"),
        ("singleton",),
    ),
    (
        "forward_collector_genesis",
        ("singleton", "database_uuid", "cohort_sha256", "genesis_json", "genesis_sha256", "ledger_genesis_event_sha256", "created_at"),
        ("singleton",),
    ),
    (
        "forward_context_observations",
        ("effective_date", "observation_phase", "decision_available_at", "outcome_observed_at", "finalized_at", "source", "receipt_id"),
        ("effective_date", "observation_phase", "source"),
    ),
    (
        "forward_corporate_action_coverage",
        ("observation_date", "symbol", "available_at", "source", "receipt_id", "event_count"),
        ("observation_date", "symbol", "source"),
    ),
    (
        "forward_corporate_actions",
        ("observation_date", "symbol", "event_id", "effective_date", "announcement_date", "payload_json", "available_at", "source", "receipt_id"),
        ("observation_date", "symbol", "event_id", "source"),
    ),
    (
        "forward_status_observations",
        ("effective_date", "observation_phase", "symbol", "name", "listing_status", "board", "is_st", "is_suspended", "source", "receipt_id"),
        ("effective_date", "observation_phase", "symbol", "source"),
    ),
    (
        "forward_universe_observations",
        ("effective_date", "observation_phase", "symbol", "is_member", "source", "receipt_id"),
        ("effective_date", "observation_phase", "symbol", "source"),
    ),
    (
        "sync_coverage",
        ("code", "source", "adjustment_mode", "adjustment_version", "start_date", "end_date", "retrieved_at"),
        ("code", "source", "adjustment_mode", "adjustment_version"),
    ),
)
_EMPTY_COUNTS = {
    "collection_receipts": 0,
    "daily": 0,
    "forward_capture_cohort": 1,
    "forward_collector_genesis": 1,
    "forward_context_observations": 0,
    "forward_corporate_action_coverage": 0,
    "forward_corporate_actions": 0,
    "forward_status_observations": 0,
    "forward_universe_observations": 0,
    "sync_coverage": 0,
}


def _canonical(value: object) -> bytes:
    return continuity.canonical_json_bytes(value)


def _typed_cell(value: object) -> list[str]:
    if value is None:
        return ["null"]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite fixture value")
        if value == 0.0:
            return ["real", "0x0.0p+0"]
        return ["real", value.hex()]
    if type(value) is str:
        return ["text", value]
    raise TypeError(type(value).__name__)


def _wire_state(connection: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    wire = bytearray(_DOMAIN)
    counts: dict[str, int] = {}
    for table, columns, primary_key in _TABLES:
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        order = ", ".join(f'"{column}" COLLATE BINARY ASC' for column in primary_key)
        count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        counts[table] = count
        header = {
            "columns": list(columns),
            "count": count,
            "kind": "table",
            "primary_key": list(primary_key),
            "table": table,
        }
        rows = connection.execute(
            f'SELECT {quoted_columns} FROM "{table}" ORDER BY {order}'
        )
        wire.extend(struct.pack(">Q", len(_canonical(header))))
        wire.extend(_canonical(header))
        wire.extend(b"\n")
        for row in rows:
            payload = {"kind": "row", "values": [_typed_cell(value) for value in row]}
            encoded = _canonical(payload)
            wire.extend(struct.pack(">Q", len(encoded)))
            wire.extend(encoded)
            wire.extend(b"\n")
    return hashlib.sha256(wire).hexdigest(), counts


def _prepared_connection(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(continuity.secrets, "token_hex", lambda _: _UUID)
    monkeypatch.setattr(
        continuity,
        "_load_cohort_sha256",
        lambda connection: _COHORT_SHA,
    )
    connection = sqlite3.connect(":memory:")
    for sql in continuity._COLLECTOR_OWNED_TABLE_SQL.values():
        connection.execute(sql)
    continuity.install_collector_evidence_triggers(connection)
    cohort = {
        "symbols": ["000001.SZ"],
        "start": "2099-01-05",
        "source": "frozen-local-test",
        "adjustment_mode": "raw",
        "adjustment_version": "test-1",
    }
    cohort_raw = _canonical(cohort)
    connection.execute(
        "INSERT INTO forward_capture_cohort VALUES (1,?,?,?)",
        (cohort_raw.decode("ascii"), hashlib.sha256(cohort_raw).hexdigest(), _CREATED_AT),
    )
    genesis = {
        "schema_version": continuity.COLLECTOR_GENESIS_SCHEMA,
        "database_uuid": _UUID,
        "cohort_sha256": _COHORT_SHA,
        "database_identity": _IDENTITY,
        "ledger_identity": {**_IDENTITY, "canonical_path": "/fixture/collector.sqlite.collector-ledger.jsonl", "file_st_ino": 15},
        "created_at": _CREATED_AT,
    }
    genesis_raw = _canonical(genesis)
    connection.execute(
        "INSERT INTO forward_collector_genesis VALUES (1,?,?,?,?,?,?)",
        (_UUID, _COHORT_SHA, genesis_raw.decode("ascii"), hashlib.sha256(genesis_raw).hexdigest(), _LEDGER_SHA, _CREATED_AT),
    )
    connection.commit()
    return connection


def _compute(connection: sqlite3.Connection) -> dict[str, Any]:
    compute = getattr(continuity, "compute_collector_logical_state")
    return compute(connection)


def _clone_connection(
    source: sqlite3.Connection, *, detect_types: int = 0
) -> sqlite3.Connection:
    cloned = sqlite3.connect(":memory:", detect_types=detect_types)
    source.backup(cloned)
    return cloned


def test_empty_prepared_collector_matches_independent_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        expected_hash, expected_counts = _wire_state(connection)
        assert expected_hash == "43d51e3649f9a5847c9454ce4a3d7990ca4c55572bbc10424b1895c992b58d2a"
        assert expected_counts == _EMPTY_COUNTS
        state = _compute(connection)
        assert state == {
            "schema_version": continuity.COLLECTOR_LOGICAL_STATE_SCHEMA,
            "collector_state_sha256": expected_hash,
            "table_counts": expected_counts,
        }
        assert continuity.validate_collector_logical_state(state) == state
    finally:
        connection.close()


def test_insert_order_page_size_and_vacuum_do_not_change_logical_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _prepared_connection(monkeypatch)
    second = _prepared_connection(monkeypatch)
    try:
        rows = [("000001.SZ", "2099-01-05", 1.0), ("000001.SZ", "2099-01-06", 2.0)]
        for row in rows:
            first.execute(
                "INSERT INTO daily(code,date,open,high,low,close,volume,source,adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
                "VALUES (?,?,?,1,1,1,1,'测试源','raw','v1','',1,NULL)",
                row,
            )
        for row in reversed(rows):
            second.execute(
                "INSERT INTO daily(code,date,open,high,low,close,volume,source,adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
                "VALUES (?,?,?,1,1,1,1,'测试源','raw','v1','',1,NULL)",
                row,
            )
        second.execute("PRAGMA page_size=8192")
        first.commit()
        second.commit()
        second.execute("VACUUM")
        assert _wire_state(first)[0] == _wire_state(second)[0]
        assert _compute(first)["collector_state_sha256"] == _compute(second)["collector_state_sha256"]
    finally:
        first.close()
        second.close()


def test_value_insert_count_changes_are_visible_and_delete_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        baseline = _compute(connection)
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,2,0.5,1.5,100,'source','raw','v1','',1,NULL)"
        )
        connection.commit()
        changed = _compute(connection)
        assert changed["collector_state_sha256"] != baseline["collector_state_sha256"]
        assert changed["table_counts"]["daily"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM daily")
        connection.rollback()
        blocked = _compute(connection)
        assert blocked["collector_state_sha256"] == changed["collector_state_sha256"]
        assert blocked["table_counts"]["daily"] == 1
    finally:
        connection.close()


def test_typed_values_are_tagged_without_collisions(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _typed_cell(None) == ["null"]
    assert _typed_cell(1) == ["integer", "1"]
    assert _typed_cell(1.0) == ["real", "0x1.0000000000000p+0"]
    assert _typed_cell(-0.0) == ["real", "0x0.0p+0"]
    assert _typed_cell("1") == ["text", "1"]
    assert len({_canonical(_typed_cell(value)) for value in (1, 1.0, "1")}) == 3
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute(
            "INSERT INTO collection_receipts VALUES (7,'2026-08-01','源','{}','{}',?,?)",
            ("4" * 64, _CREATED_AT),
        )
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',-0.0,NULL,1.0,2.0,3.0,'unicode-测试','raw','v1','',1,7)"
        )
        connection.commit()
        state = _compute(connection)
        assert state["table_counts"]["collection_receipts"] == 1
        assert state["table_counts"]["daily"] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("bad_value", [b"blob", float("inf"), float("-inf")])
def test_blob_and_non_finite_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, bad_value: object
) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',?,?,?,?,?,'source','raw','v1','',1,NULL)",
            (bad_value, 1.0, 1.0, 1.0, 1.0),
        )
        connection.commit()
        with pytest.raises(continuity.CollectorContinuityError):
            _compute(connection)
    finally:
        connection.close()


def test_invalid_utf8_text_storage_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,"
            "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,1,1,1,1,CAST(X'80' AS TEXT),"
            "'raw','v1','',1,NULL)"
        )
        connection.commit()
        with pytest.raises(
            continuity.CollectorContinuityError, match="cannot be computed"
        ) as error:
            _compute(connection)
        assert isinstance(error.value.__cause__, continuity.CollectorContinuityError)
        assert "valid UTF-8" in str(error.value.__cause__)
    finally:
        connection.close()


def test_selective_text_factory_cannot_change_logical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _prepared_connection(monkeypatch)
    selective = None
    try:
        source.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,"
            "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,1,1,1,1,'business-text',"
            "'raw','v1','',1,NULL)"
        )
        source.commit()
        expected = _compute(source)

        def selective_text_factory(raw: bytes) -> object:
            if raw == b"business-text":
                return "MUTATED BUSINESS TEXT"
            return raw.decode("utf-8")

        selective = _clone_connection(source)
        selective.text_factory = selective_text_factory
        factory_before = selective.text_factory
        assert selective.execute("SELECT sql FROM sqlite_master").fetchone()[0]
        continuity.verify_collector_authority_schema(selective)
        assert (
            selective.execute("SELECT source FROM daily").fetchone()[0]
            == "MUTATED BUSINESS TEXT"
        )
        assert _compute(selective) == expected
        assert selective.text_factory is factory_before
    finally:
        source.close()
        if selective is not None:
            selective.close()


def test_row_factory_cannot_change_logical_state_and_is_restored_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,"
            "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,1,1,1,1,'business-text',"
            "'raw','v1','',1,NULL)"
        )
        connection.commit()
        baseline = _compute(connection)

        def mutating_row_factory(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> tuple[object, ...]:
            return tuple(
                (
                    b"MUTATED BUSINESS TEXT"
                    if value == b"business-text"
                    else "MUTATED BUSINESS TEXT"
                    if value == "business-text"
                    else value
                )
                for value in row
            )

        connection.row_factory = mutating_row_factory
        assert (
            connection.execute("SELECT source FROM daily").fetchone()[0]
            == "MUTATED BUSINESS TEXT"
        )
        assert _compute(connection) == baseline
        assert connection.row_factory is mutating_row_factory

        connection.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ
            else sqlite3.SQLITE_OK
        )
        with pytest.raises(continuity.CollectorContinuityError):
            _compute(connection)
        connection.set_authorizer(None)
        assert connection.row_factory is mutating_row_factory
    finally:
        connection.close()


def test_declared_text_converter_does_not_change_logical_state_or_pollute_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _prepared_connection(monkeypatch)
    converted = None
    original_converters = sqlite3.converters.copy()
    try:
        source.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,"
            "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,1,1,1,1,'converter-text',"
            "'raw','v1','',1,NULL)"
        )
        source.commit()
        expected = _compute(source)

        def malicious_text_converter(raw: bytes) -> str:
            if raw == b"converter-text":
                return "MALICIOUS CONVERSION"
            return raw.decode("utf-8")

        sqlite3.converters["TEXT"] = malicious_text_converter
        converted = _clone_connection(
            source, detect_types=sqlite3.PARSE_DECLTYPES
        )
        factory_before = converted.text_factory
        assert (
            converted.execute("SELECT source FROM daily").fetchone()[0]
            == "MALICIOUS CONVERSION"
        )
        assert _compute(converted) == expected
        assert converted.text_factory is factory_before
    finally:
        if converted is not None:
            converted.close()
        source.close()
        sqlite3.converters.clear()
        sqlite3.converters.update(original_converters)
    assert sqlite3.converters == original_converters


def test_existing_transaction_is_rejected_before_state_change(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute("BEGIN")
        assert connection.in_transaction
        with pytest.raises(continuity.CollectorContinuityError):
            _compute(connection)
        assert connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("query_only", [0, 1])
def test_query_only_is_restored_after_success(
    monkeypatch: pytest.MonkeyPatch, query_only: int
) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        factory_before = connection.text_factory
        connection.execute(f"PRAGMA query_only={query_only}")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == query_only
        state = _compute(connection)
        assert state["table_counts"]["daily"] == 0
        assert connection.in_transaction is False
        assert connection.execute("PRAGMA query_only").fetchone()[0] == query_only
        assert connection.text_factory is factory_before
    finally:
        connection.close()


def test_rollback_failure_is_explicit_and_leaves_transaction_state_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RollbackFailureConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise sqlite3.OperationalError("injected rollback failure")

    real_connect = sqlite3.connect

    def connect_with_rollback_failure(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = RollbackFailureConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_rollback_failure)
    connection = _prepared_connection(monkeypatch)
    try:
        connection.execute(
            "INSERT INTO daily(code,date,open,high,low,close,volume,source,"
            "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id) "
            "VALUES ('000001.SZ','2099-01-05',1,1,1,1,1,CAST(X'80' AS TEXT),"
            "'raw','v1','',1,NULL)"
        )
        connection.commit()
        with pytest.raises(continuity.CollectorContinuityError) as error:
            _compute(connection)
        error_text = " ".join(
            text
            for text in (str(error.value), str(error.value.__cause__))
            if text
        ).lower()
        assert "rollback" in error_text
        assert connection.in_transaction is True
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        connection.close()


def test_runtime_error_rolls_back_and_restores_query_only(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _prepared_connection(monkeypatch)
    try:
        factory_before = connection.text_factory
        connection.execute("PRAGMA query_only=0")
        connection.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ
            else sqlite3.SQLITE_OK
        )
        with pytest.raises(continuity.CollectorContinuityError):
            _compute(connection)
        connection.set_authorizer(None)
        assert connection.in_transaction is False
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0
        assert connection.text_factory is factory_before
        connection.execute("CREATE TABLE still_usable(value INTEGER)")
        connection.rollback()
    finally:
        connection.close()
