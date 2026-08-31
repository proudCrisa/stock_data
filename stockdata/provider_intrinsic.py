"""Deterministic reconstruction of provider-owned RQGM evidence."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

from .adjustment_identity import VerifiedAdjustmentIdentity
from .availability import price_availability_error
from .execution_readiness import _structural_status, check_execution_readiness
from .forward_context import (
    _TRIGGER_SQL as CONTEXT_TRIGGER_SQL,
)
from .forward_context import (
    SOURCE as CONTEXT_SOURCE,
)
from .forward_context import (
    _market_symbol,
    _normalized_sql,
    _parse_raw_pages,
    _status_values,
    check_forward_context_readiness,
)
from .forward_corporate_actions import (
    _DECISION_CUTOFF as CORPORATE_ACTION_DECISION_CUTOFF,
)
from .forward_corporate_actions import (
    _PREOPEN_START as CORPORATE_ACTION_PREOPEN_START,
)
from .forward_corporate_actions import (
    _SHANGHAI as CORPORATE_ACTION_SHANGHAI,
)
from .forward_corporate_actions import (
    _TRIGGER_SQL as CORPORATE_ACTION_TRIGGER_SQL,
)
from .forward_corporate_actions import (
    SOURCE as CORPORATE_ACTION_SOURCE,
)
from .forward_corporate_actions import (
    _event_fields,
    _request_matches,
    _source_rows,
)
from .rqgm_execution_export import _receipt_covers_bar
from .rqgm_provider_contract import COMPONENT_SCHEMAS, ProviderArtifactReference
from .ticker import normalize

INTRINSIC_COMPONENTS = (
    "execution_prices",
    "signal_prices",
    "decision_context",
)
FORWARD_COMPONENTS = (
    "universe",
    "instrument_status",
    "corporate_actions",
)
DATABASE_SOURCE_RECEIPT_SCHEMA = "stockdata-database-source-receipt/1"
INTRINSIC_VERIFIER_SCHEMA = "stockdata-provider-intrinsic-verifier/1"
_DECISION_CAPTURED_INPUT_FIELDS = ("name", "trade", "volume")


class IntrinsicEvidenceError(ValueError):
    """A stable, fail-closed intrinsic reconstruction failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ReconstructedIntrinsicEvidence:
    components: Mapping[str, Mapping[str, object]]
    source_receipts: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class ReconstructedForwardEvidence:
    components: Mapping[str, Mapping[str, object]]
    source_receipts: Mapping[str, Mapping[str, object]]


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_noncanonical_json", "intrinsic evidence is not canonical JSON"
        ) from exc


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise IntrinsicEvidenceError(
            "intrinsic_invalid_timestamp", f"{field} must be timezone-aware"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_invalid_timestamp", f"{field} must be timezone-aware"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntrinsicEvidenceError(
            "intrinsic_invalid_timestamp", f"{field} must be timezone-aware"
        )
    return parsed


def _panel(values: Sequence[str]) -> tuple[str, ...]:
    normalized = []
    for value in values:
        if not isinstance(value, str) or value.count("@") != 1:
            raise IntrinsicEvidenceError(
                "intrinsic_invalid_panel", "panel entries must be symbol@YYYY-MM-DD"
            )
        symbol, day = value.split("@")
        try:
            normalized.append(f"{normalize(symbol)}@{date.fromisoformat(day).isoformat()}")
        except ValueError as exc:
            raise IntrinsicEvidenceError(
                "intrinsic_invalid_panel", "panel entries must be symbol@YYYY-MM-DD"
            ) from exc
    result = tuple(sorted(normalized))
    if not result or len(result) != len(set(result)):
        raise IntrinsicEvidenceError(
            "intrinsic_invalid_panel", "panel must be non-empty and unique"
        )
    return result


def _connect(database: str | Path | bytes) -> sqlite3.Connection:
    try:
        if isinstance(database, bytes):
            connection = sqlite3.connect(":memory:")
            if not hasattr(connection, "deserialize"):
                connection.close()
                raise IntrinsicEvidenceError(
                    "intrinsic_database_unreadable",
                    "SQLite byte reverification requires Connection.deserialize",
                )
            connection.deserialize(database)
            connection.execute("PRAGMA query_only=ON")
        else:
            path = Path(database).expanduser().resolve()
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_database_unreadable", "intrinsic database is unreadable"
        ) from exc


def _database_receipt(row: sqlite3.Row) -> tuple[str, Mapping[str, object]]:
    try:
        request = json.loads(str(row["request_json"]))
        response = json.loads(str(row["response_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_receipt_invalid", "database receipt JSON is invalid"
        ) from exc
    request_raw = _canonical(request).decode("ascii")
    response_raw = _canonical(response).decode("ascii")
    if request_raw != row["request_json"] or response_raw != row["response_json"]:
        raise IntrinsicEvidenceError(
            "intrinsic_receipt_noncanonical", "database receipt JSON is not canonical"
        )
    response_sha256 = hashlib.sha256(response_raw.encode("ascii")).hexdigest()
    if response_sha256 != row["response_sha256"]:
        raise IntrinsicEvidenceError(
            "intrinsic_receipt_hash_mismatch", "database receipt response hash drifted"
        )
    _timestamp(row["observed_at"], "receipt observed_at")
    value = {
        "schema_version": DATABASE_SOURCE_RECEIPT_SCHEMA,
        "database_receipt_id": int(row["receipt_id"]),
        "observed_at": str(row["observed_at"]),
        "source": str(row["receipt_source"]),
        "request": request,
        "response": response,
        "response_sha256": response_sha256,
    }
    identifier = hashlib.sha256(_canonical(value)).hexdigest()
    return identifier, value


def _validate_database_structure(connection: sqlite3.Connection) -> None:
    _, blockers = _structural_status(connection)
    if blockers:
        raise IntrinsicEvidenceError(
            "intrinsic_database_structure_invalid",
            "price database schema or append-only receipts are invalid",
        )
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "forward_context_observations",
        "forward_universe_observations",
        "forward_status_observations",
    }
    if not required.issubset(tables):
        raise IntrinsicEvidenceError(
            "intrinsic_context_schema_invalid", "decision context tables are incomplete"
        )
    triggers = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        )
    }
    if any(
        name not in triggers
        or _normalized_sql(triggers[name]) != _normalized_sql(sql)
        for name, sql in CONTEXT_TRIGGER_SQL.items()
    ):
        raise IntrinsicEvidenceError(
            "intrinsic_context_structure_invalid",
            "decision context append-only triggers are invalid",
        )


def _validate_forward_component_structure(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "forward_corporate_action_coverage",
        "forward_corporate_actions",
    }
    if not required.issubset(tables):
        raise IntrinsicEvidenceError(
            "intrinsic_corporate_action_schema_invalid",
            "corporate-action tables are incomplete",
        )
    triggers = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        )
    }
    if any(
        name not in triggers
        or _normalized_sql(triggers[name]) != _normalized_sql(sql)
        for name, sql in CORPORATE_ACTION_TRIGGER_SQL.items()
    ):
        raise IntrinsicEvidenceError(
            "intrinsic_corporate_action_structure_invalid",
            "corporate-action append-only triggers are invalid",
        )


def _decision_input_projection(
    symbol: str, source_row: Mapping[str, object]
) -> dict[str, object]:
    """Project only raw pre-decision inputs from one Sina market-row receipt."""

    if _market_symbol(source_row.get("symbol")) != symbol:
        raise ValueError("decision input symbol differs from the receipt row")
    name = source_row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("decision input name is invalid")
    for field in ("trade", "volume"):
        try:
            numeric = float(source_row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"decision input {field} is invalid") from exc
        if not math.isfinite(numeric):
            raise ValueError(f"decision input {field} is invalid")
    return {field: source_row[field] for field in _DECISION_CAPTURED_INPUT_FIELDS}


def _price_artifact(
    connection: sqlite3.Connection,
    *,
    component: str,
    panel: tuple[str, ...],
    adjustment: VerifiedAdjustmentIdentity,
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]]]:
    expected = {tuple(value.split("@")) for value in panel}
    rows = connection.execute(
        """
        SELECT d.code,d.date,d.open,d.high,d.low,d.close,d.volume,d.retrieved_at,
               d.is_final,d.receipt_id,r.observed_at,r.source AS receipt_source,
               r.request_json,r.response_json,r.response_sha256
        FROM daily AS d
        JOIN collection_receipts AS r ON r.receipt_id=d.receipt_id
        WHERE d.source=? AND d.adjustment_mode=? AND d.adjustment_version=?
        ORDER BY d.code,d.date
        """,
        (
            adjustment.source,
            adjustment.adjustment_mode,
            adjustment.adjustment_version,
        ),
    ).fetchall()
    selected: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (normalize(str(row["code"])), str(row["date"]))
        if key not in expected:
            continue
        if key in selected:
            raise IntrinsicEvidenceError(
                "intrinsic_duplicate_price_row", f"duplicate price row: {key[0]}@{key[1]}"
            )
        selected[key] = row
    if set(selected) != expected:
        raise IntrinsicEvidenceError(
            "intrinsic_price_coverage_mismatch", f"{component} does not cover exact panel"
        )

    records = []
    receipts: dict[str, Mapping[str, object]] = {}
    session_days = sorted({day for _, day in expected})
    next_session = dict(zip(session_days, session_days[1:]))
    for symbol, day in sorted(expected):
        row = selected[(symbol, day)]
        if int(row["is_final"]) != 1:
            raise IntrinsicEvidenceError(
                "intrinsic_price_not_final", f"price row is not final: {symbol}@{day}"
            )
        available = _timestamp(row["observed_at"], "price available_at")
        retrieved = _timestamp(row["retrieved_at"], "price retrieved_at")
        if available != retrieved:
            raise IntrinsicEvidenceError(
                "intrinsic_receipt_timestamp_mismatch",
                f"price receipt timestamp differs: {symbol}@{day}",
            )
        availability_error = price_availability_error(
            day, available, next_session.get(day)
        )
        if availability_error:
            raise IntrinsicEvidenceError(
                f"intrinsic_{availability_error}",
                f"price availability is invalid: {symbol}@{day}",
            )
        if not _receipt_covers_bar(str(row["response_json"]), row):
            raise IntrinsicEvidenceError(
                "intrinsic_receipt_record_mismatch",
                f"price receipt does not contain row: {symbol}@{day}",
            )
        values = {
            field: float(row[field])
            for field in ("open", "high", "low", "close", "volume")
        }
        if any(not math.isfinite(value) for value in values.values()) or any(
            values[field] <= 0 for field in ("open", "high", "low", "close")
        ) or values["volume"] < 0:
            raise IntrinsicEvidenceError(
                "intrinsic_invalid_price", f"invalid price row: {symbol}@{day}"
            )
        receipt_id, receipt = _database_receipt(row)
        receipts[receipt_id] = receipt
        payload = {"symbol": symbol, **values}
        records.append(
            {
                "panel_entry": f"{symbol}@{day}",
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{day}T15:00:00+08:00",
                "available_at": str(row["observed_at"]),
            }
        )
    return (
        {
            "schema_version": COMPONENT_SCHEMAS[component],
            "component": component,
            "panel": list(panel),
            "adjustment_identity_sha256": adjustment.identifier,
            "records": records,
        },
        receipts,
    )


def _context_artifact(
    connection: sqlite3.Connection,
    *,
    panel: tuple[str, ...],
    decision_cutoffs: Mapping[str, str],
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]]]:
    if set(decision_cutoffs) != set(panel):
        raise IntrinsicEvidenceError(
            "intrinsic_context_cutoff_mismatch", "calendar cutoffs do not cover panel"
        )
    expected = {tuple(value.split("@")) for value in panel}
    rows = connection.execute(
        """
        SELECT u.symbol,u.effective_date,o.decision_available_at,u.receipt_id,
               r.observed_at,r.source AS receipt_source,r.request_json,
               r.response_json,r.response_sha256
        FROM forward_universe_observations AS u
        JOIN forward_context_observations AS o
          ON o.effective_date=u.effective_date
         AND o.observation_phase=u.observation_phase
         AND o.source=u.source AND o.receipt_id=u.receipt_id
        JOIN collection_receipts AS r ON r.receipt_id=u.receipt_id
        WHERE u.source=? AND u.observation_phase='pre_open'
        ORDER BY u.symbol,u.effective_date
        """,
        (CONTEXT_SOURCE,),
    ).fetchall()
    selected: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (normalize(str(row["symbol"])), str(row["effective_date"]))
        if key not in expected:
            continue
        if key in selected:
            raise IntrinsicEvidenceError(
                "intrinsic_duplicate_context_row", f"duplicate context row: {key[0]}@{key[1]}"
            )
        selected[key] = row
    if set(selected) != expected:
        raise IntrinsicEvidenceError(
            "intrinsic_context_coverage_mismatch", "decision context does not cover panel"
        )

    records = []
    receipts: dict[str, Mapping[str, object]] = {}
    for symbol, day in sorted(expected):
        row = selected[(symbol, day)]
        available = _timestamp(row["observed_at"], "context available_at")
        cutoff = _timestamp(decision_cutoffs[f"{symbol}@{day}"], "decision cutoff")
        if row["decision_available_at"] != row["observed_at"] or available >= cutoff:
            raise IntrinsicEvidenceError(
                "intrinsic_context_post_cutoff", f"context is not pre-decision: {symbol}@{day}"
            )
        if row["receipt_source"] != CONTEXT_SOURCE:
            raise IntrinsicEvidenceError(
                "intrinsic_context_source_mismatch", "context receipt source drifted"
            )
        receipt_id, receipt = _database_receipt(row)
        try:
            response = receipt["response"]
            if not isinstance(response, Mapping):
                raise ValueError("response is not an object")
            raw_rows = _parse_raw_pages(response.get("raw_pages"))
            response_rows = {
                _market_symbol(item.get("symbol")): dict(item) for item in raw_rows
            }
            source_row = _decision_input_projection(
                symbol, response_rows[symbol]
            )
            if (
                response.get("advertised_count", len(raw_rows)) != len(raw_rows)
                or response.get("rows") is not None
                and response["rows"] != raw_rows
            ):
                raise ValueError("context response differs from stored row")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntrinsicEvidenceError(
                "intrinsic_context_receipt_mismatch",
                f"context receipt does not contain row: {symbol}@{day}",
            ) from exc
        receipts[receipt_id] = receipt
        payload = {
            "symbol": symbol,
            "captured_input": source_row,
        }
        records.append(
            {
                "panel_entry": f"{symbol}@{day}",
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{day}T00:00:00+08:00",
                "available_at": str(row["observed_at"]),
            }
        )
    return (
        {
            "schema_version": COMPONENT_SCHEMAS["decision_context"],
            "component": "decision_context",
            "panel": list(panel),
            "records": records,
        },
        receipts,
    )


def _context_rows_by_session(
    connection: sqlite3.Connection,
    *,
    panel: tuple[str, ...],
) -> tuple[
    dict[str, tuple[str, Mapping[str, object], dict[str, Mapping[str, object]]]],
    dict[str, Mapping[str, object]],
]:
    sessions = sorted({entry.rsplit("@", 1)[1] for entry in panel})
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"""
        SELECT u.effective_date,u.symbol,u.is_member,u.receipt_id,
               r.observed_at,r.source AS receipt_source,r.request_json,
               r.response_json,r.response_sha256
        FROM forward_universe_observations AS u
        JOIN forward_context_observations AS o
          ON o.effective_date=u.effective_date
         AND o.observation_phase=u.observation_phase
         AND o.source=u.source AND o.receipt_id=u.receipt_id
        JOIN collection_receipts AS r ON r.receipt_id=u.receipt_id
        WHERE u.source=? AND u.observation_phase='pre_open'
          AND u.effective_date IN ({placeholders})
        ORDER BY u.effective_date,u.symbol
        """,
        (CONTEXT_SOURCE, *sessions),
    ).fetchall()
    by_session: dict[str, list[sqlite3.Row]] = {session: [] for session in sessions}
    for row in rows:
        by_session[str(row["effective_date"])].append(row)

    context: dict[str, tuple[str, Mapping[str, object], dict[str, Mapping[str, object]]]] = {}
    receipts: dict[str, Mapping[str, object]] = {}
    for session, session_rows in by_session.items():
        if not session_rows or len({int(row["receipt_id"]) for row in session_rows}) != 1:
            raise IntrinsicEvidenceError(
                "intrinsic_universe_receipt_mismatch",
                f"universe receipt does not close session {session}",
            )
        receipt_id, receipt = _database_receipt(session_rows[0])
        if any(_database_receipt(row)[0] != receipt_id for row in session_rows):
            raise IntrinsicEvidenceError(
                "intrinsic_universe_receipt_mismatch",
                f"universe receipt body drifts in session {session}",
            )
        try:
            response = receipt["response"]
            if not isinstance(response, Mapping):
                raise TypeError("response is not an object")
            raw_rows = _parse_raw_pages(response.get("raw_pages"))
            raw_by_symbol = {
                _market_symbol(row.get("symbol")): dict(row) for row in raw_rows
            }
            members = [
                normalize(str(row["symbol"]))
                for row in session_rows
                if int(row["is_member"]) == 1
            ]
            if (
                len(raw_by_symbol) != len(raw_rows)
                or receipt.get("source") != CONTEXT_SOURCE
                or sorted(members) != sorted(raw_by_symbol)
                or any(int(row["is_member"]) != 1 for row in session_rows)
                or response.get("advertised_count", len(raw_rows)) != len(raw_rows)
                or response.get("rows") is not None and response["rows"] != raw_rows
            ):
                raise ValueError("universe rows differ from the full-market receipt")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntrinsicEvidenceError(
                "intrinsic_universe_receipt_mismatch",
                f"universe receipt does not reproduce session {session}",
            ) from exc
        receipts[receipt_id] = receipt
        context[session] = (receipt_id, receipt, raw_by_symbol)
    return context, receipts


def _forward_universe_artifact(
    connection: sqlite3.Connection,
    *,
    panel: tuple[str, ...],
    decision_cutoffs: Mapping[str, str],
) -> tuple[
    Mapping[str, object],
    dict[str, Mapping[str, object]],
    dict[str, tuple[str, Mapping[str, object], dict[str, Mapping[str, object]]]],
]:
    context, receipts = _context_rows_by_session(connection, panel=panel)
    records = []
    for entry in panel:
        _, session = entry.split("@")
        receipt_id, receipt, _ = context[session]
        available = _timestamp(receipt["observed_at"], "universe available_at")
        cutoff = _timestamp(decision_cutoffs[entry], "universe decision cutoff")
        if available >= cutoff:
            raise IntrinsicEvidenceError(
                "intrinsic_universe_post_cutoff", f"universe is post-cutoff: {entry}"
            )
        member_symbols = sorted(context[session][2])
        identity = {
            "schema_version": "stockdata-forward-universe-identity/1",
            "effective_date": session,
            "observation_phase": "pre_open",
            "source": CONTEXT_SOURCE,
            "source_receipt_id": receipt_id,
            "member_symbols_sha256": hashlib.sha256(
                _canonical(member_symbols)
            ).hexdigest(),
        }
        payload = {
            "is_member": True,
            "universe_id": hashlib.sha256(_canonical(identity)).hexdigest(),
        }
        records.append(
            {
                "panel_entry": entry,
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{session}T00:00:00+08:00",
                "available_at": receipt["observed_at"],
            }
        )
    return (
        {
            "schema_version": COMPONENT_SCHEMAS["universe"],
            "component": "universe",
            "panel": list(panel),
            "records": records,
        },
        receipts,
        context,
    )


def _forward_status_artifact(
    connection: sqlite3.Connection,
    *,
    panel: tuple[str, ...],
    decision_cutoffs: Mapping[str, str],
    context: Mapping[str, tuple[str, Mapping[str, object], dict[str, Mapping[str, object]]]],
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]]]:
    expected = {tuple(entry.split("@")) for entry in panel}
    sessions = sorted({session for _, session in expected})
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"""
        SELECT effective_date,symbol,name,listing_status,board,is_st,is_suspended,receipt_id
        FROM forward_status_observations
        WHERE source=? AND observation_phase='pre_open'
          AND effective_date IN ({placeholders})
        ORDER BY effective_date,symbol
        """,
        (CONTEXT_SOURCE, *sessions),
    ).fetchall()
    selected: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (normalize(str(row["symbol"])), str(row["effective_date"]))
        if key in expected:
            if key in selected:
                raise IntrinsicEvidenceError(
                    "intrinsic_duplicate_status_row", f"duplicate status row: {key[0]}@{key[1]}"
                )
            selected[key] = row
    if set(selected) != expected:
        raise IntrinsicEvidenceError(
            "intrinsic_status_coverage_mismatch", "instrument status does not cover exact panel"
        )
    receipts: dict[str, Mapping[str, object]] = {}
    records = []
    for symbol, session in sorted(expected):
        row = selected[(symbol, session)]
        receipt_id, receipt, raw_by_symbol = context[session]
        if int(row["receipt_id"]) != int(receipt["database_receipt_id"]):
            raise IntrinsicEvidenceError(
                "intrinsic_status_receipt_mismatch", f"status receipt drifts: {symbol}@{session}"
            )
        try:
            name, listing_status, board, is_st, is_suspended = _status_values(
                symbol, raw_by_symbol[symbol]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntrinsicEvidenceError(
                "intrinsic_status_receipt_mismatch", f"status receipt omits: {symbol}@{session}"
            ) from exc
        if (
            str(row["name"]), str(row["listing_status"]), str(row["board"]),
            int(row["is_st"]), int(row["is_suspended"]),
        ) != (name, listing_status, board, is_st, is_suspended):
            raise IntrinsicEvidenceError(
                "intrinsic_status_receipt_mismatch", f"status row differs: {symbol}@{session}"
            )
        available = _timestamp(receipt["observed_at"], "status available_at")
        if available >= _timestamp(decision_cutoffs[f"{symbol}@{session}"], "status decision cutoff"):
            raise IntrinsicEvidenceError(
                "intrinsic_status_post_cutoff", f"status is post-cutoff: {symbol}@{session}"
            )
        payload = {
            "is_st": bool(is_st),
            "is_suspended": bool(is_suspended),
            "listing_status": listing_status,
        }
        receipts[receipt_id] = receipt
        records.append(
            {
                "panel_entry": f"{symbol}@{session}",
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{session}T00:00:00+08:00",
                "available_at": receipt["observed_at"],
            }
        )
    return (
        {
            "schema_version": COMPONENT_SCHEMAS["instrument_status"],
            "component": "instrument_status",
            "panel": list(panel),
            "records": records,
        },
        receipts,
    )


def _forward_corporate_actions_artifact(
    connection: sqlite3.Connection,
    *,
    panel: tuple[str, ...],
    decision_cutoffs: Mapping[str, str],
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]]]:
    expected = {tuple(entry.split("@")) for entry in panel}
    sessions = sorted({session for _, session in expected})
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"""
        SELECT c.observation_date,c.symbol,c.available_at,c.receipt_id,c.event_count,
               r.observed_at,r.source AS receipt_source,r.request_json,r.response_json,
               r.response_sha256
        FROM forward_corporate_action_coverage AS c
        JOIN collection_receipts AS r ON r.receipt_id=c.receipt_id
        WHERE c.source=? AND c.observation_date IN ({placeholders})
        ORDER BY c.observation_date,c.symbol
        """,
        (CORPORATE_ACTION_SOURCE, *sessions),
    ).fetchall()
    coverage: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (normalize(str(row["symbol"])), str(row["observation_date"]))
        if key not in expected or key in coverage:
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_coverage_mismatch",
                "corporate-action coverage differs from exact panel",
            )
        coverage[key] = row
    if set(coverage) != expected:
        raise IntrinsicEvidenceError(
            "intrinsic_corporate_action_coverage_mismatch",
            "corporate actions do not cover exact panel",
        )

    receipts: dict[str, Mapping[str, object]] = {}
    raw_by_session: dict[str, dict[str, list[dict[str, object]]]] = {}
    for session in sessions:
        session_rows = [row for (_, day), row in coverage.items() if day == session]
        if len({int(row["receipt_id"]) for row in session_rows}) != 1:
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_receipt_mismatch",
                f"corporate-action receipt does not close session {session}",
            )
        receipt_id, receipt = _database_receipt(session_rows[0])
        observed = _timestamp(receipt["observed_at"], "corporate-action observed_at")
        local = observed.astimezone(CORPORATE_ACTION_SHANGHAI)
        if (
            local.date().isoformat() != session
            or not CORPORATE_ACTION_PREOPEN_START <= local.time() < CORPORATE_ACTION_DECISION_CUTOFF
        ):
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_window_invalid",
                f"corporate-action capture is not pre-open: {session}",
            )
        try:
            source_rows = _source_rows(receipt["response"])
            if (
                set(source_rows) != {symbol for symbol, day in expected if day == session}
                or not _request_matches(str(session_rows[0]["request_json"]), source_rows, session)
                or any(str(row["available_at"]) != receipt["observed_at"] for row in session_rows)
            ):
                raise ValueError("coverage receipt differs")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_receipt_mismatch",
                f"corporate-action receipt does not reproduce session {session}",
            ) from exc
        for row in session_rows:
            if int(row["event_count"]) != len(source_rows[normalize(str(row["symbol"]))]):
                raise IntrinsicEvidenceError(
                    "intrinsic_corporate_action_receipt_mismatch",
                    f"corporate-action event count drifts: {row['symbol']}@{session}",
                )
        receipts[receipt_id] = receipt
        raw_by_session[session] = source_rows
    if len(receipts) != len(sessions):
        raise IntrinsicEvidenceError(
            "intrinsic_corporate_action_receipt_mismatch",
            "corporate-action sessions reuse a collector receipt",
        )

    event_rows = connection.execute(
        f"""
        SELECT observation_date,symbol,event_id,effective_date,announcement_date,payload_json,
               available_at,receipt_id
        FROM forward_corporate_actions
        WHERE source=? AND observation_date IN ({placeholders})
        ORDER BY observation_date,symbol,event_id
        """,
        (CORPORATE_ACTION_SOURCE, *sessions),
    ).fetchall()
    actual: dict[tuple[str, str], set[tuple[object, ...]]] = {
        key: set() for key in expected
    }
    for row in event_rows:
        key = (normalize(str(row["symbol"])), str(row["observation_date"]))
        if key not in actual:
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_receipt_mismatch",
                "corporate-action event is outside exact panel",
            )
        actual[key].add(
            (
                str(row["event_id"]), row["effective_date"], row["announcement_date"],
                str(row["payload_json"]), str(row["available_at"]), int(row["receipt_id"]),
            )
        )
    for (symbol, session), coverage_row in coverage.items():
        expected_events: set[tuple[object, ...]] = set()
        for source_row in raw_by_session[session][symbol]:
            payload_json = _canonical(source_row).decode("ascii")
            effective, announcement = _event_fields(source_row)
            expected_events.add(
                (
                    hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
                    effective,
                    announcement,
                    payload_json,
                    str(coverage_row["available_at"]),
                    int(coverage_row["receipt_id"]),
                )
            )
        if actual[(symbol, session)] != expected_events:
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_receipt_mismatch",
                f"corporate-action rows differ: {symbol}@{session}",
            )
        if (
            expected_events
            or int(coverage_row["event_count"]) != 0
            or raw_by_session[session][symbol]
        ):
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_nonzero_not_supported",
                f"corporate actions require a versioned normalization: {symbol}@{session}",
            )

    records = []
    for symbol, session in sorted(expected):
        coverage_row = coverage[(symbol, session)]
        receipt_id, receipt = _database_receipt(coverage_row)
        available = _timestamp(receipt["observed_at"], "corporate-action available_at")
        if available >= _timestamp(decision_cutoffs[f"{symbol}@{session}"], "corporate-action decision cutoff"):
            raise IntrinsicEvidenceError(
                "intrinsic_corporate_action_post_cutoff",
                f"corporate actions are post-cutoff: {symbol}@{session}",
            )
        payload = {"events": []}
        records.append(
            {
                "panel_entry": f"{symbol}@{session}",
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{session}T00:00:00+08:00",
                "available_at": receipt["observed_at"],
            }
        )
    return (
        {
            "schema_version": COMPONENT_SCHEMAS["corporate_actions"],
            "component": "corporate_actions",
            "panel": list(panel),
            "records": records,
        },
        receipts,
    )


def reconstruct_forward_component_evidence(
    database: bytes,
    *,
    panel: Sequence[str],
    decision_cutoffs: Mapping[str, str],
) -> ReconstructedForwardEvidence:
    """Rebuild unsigned local components and database receipts from retained bytes."""

    if not isinstance(database, bytes):
        raise IntrinsicEvidenceError(
            "intrinsic_database_not_retained", "forward components require retained database bytes"
        )
    canonical_panel = _panel(panel)
    if set(decision_cutoffs) != set(canonical_panel):
        raise IntrinsicEvidenceError(
            "intrinsic_forward_cutoff_mismatch", "calendar cutoffs do not cover panel"
        )
    connection = _connect(database)
    try:
        _validate_database_structure(connection)
        _validate_forward_component_structure(connection)
        universe, universe_receipts, context = _forward_universe_artifact(
            connection, panel=canonical_panel, decision_cutoffs=decision_cutoffs
        )
        status, status_receipts = _forward_status_artifact(
            connection,
            panel=canonical_panel,
            decision_cutoffs=decision_cutoffs,
            context=context,
        )
        actions, action_receipts = _forward_corporate_actions_artifact(
            connection, panel=canonical_panel, decision_cutoffs=decision_cutoffs
        )
    except sqlite3.Error as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_database_schema_mismatch", "forward component database schema is incomplete"
        ) from exc
    finally:
        connection.close()
    receipts = {**universe_receipts, **status_receipts, **action_receipts}
    return ReconstructedForwardEvidence(
        components={
            "universe": universe,
            "instrument_status": status,
            "corporate_actions": actions,
        },
        source_receipts=receipts,
    )


def verify_forward_component_evidence(
    reconstructed: ReconstructedForwardEvidence,
    *,
    claimed_components: Mapping[str, object],
    component_references: Mapping[str, object],
    bound_source_receipts: Mapping[str, object],
) -> None:
    """Require exact bytes and bodies for the retained forward component projection."""

    if set(claimed_components) != set(FORWARD_COMPONENTS) or set(
        component_references
    ) != set(FORWARD_COMPONENTS):
        raise IntrinsicEvidenceError(
            "intrinsic_forward_component_set_mismatch", "forward component set is incomplete"
        )
    for component in FORWARD_COMPONENTS:
        rebuilt = reconstructed.components[component]
        reference = component_references[component]
        identifier = getattr(reference, "identifier", None)
        if (
            _canonical(claimed_components[component]) != _canonical(rebuilt)
            or hashlib.sha256(_canonical(rebuilt)).hexdigest() != identifier
        ):
            raise IntrinsicEvidenceError(
                "intrinsic_forward_component_byte_mismatch",
                f"{component} differs from retained reconstruction",
            )
    for receipt_id, receipt in reconstructed.source_receipts.items():
        if (
            bound_source_receipts.get(receipt_id) != receipt
            or hashlib.sha256(_canonical(receipt)).hexdigest() != receipt_id
        ):
            raise IntrinsicEvidenceError(
                "intrinsic_forward_source_receipt_not_bound",
                "forward component source receipt is not bound",
            )


def reconstruct_intrinsic_evidence(
    database: str | Path | bytes,
    *,
    panel: Sequence[str],
    execution_adjustment: VerifiedAdjustmentIdentity,
    signal_adjustment: VerifiedAdjustmentIdentity,
    decision_cutoffs: Mapping[str, str],
) -> ReconstructedIntrinsicEvidence:
    """Rebuild all provider-owned components without trusting caller artifacts."""

    canonical_panel = _panel(panel)
    if execution_adjustment.price_role != "execution" or (
        signal_adjustment.price_role != "signal"
    ):
        raise IntrinsicEvidenceError(
            "intrinsic_adjustment_role_mismatch", "price adjustment roles are invalid"
        )
    if not isinstance(database, bytes):
        panel_pairs = cast(
            set[tuple[str, str]],
            {tuple(value.split("@")) for value in canonical_panel},
        )
        for component, adjustment in (
            ("execution_prices", execution_adjustment),
            ("signal_prices", signal_adjustment),
        ):
            report = check_execution_readiness(
                database,
                source=adjustment.source,
                adjustment_mode=adjustment.adjustment_mode,
                adjustment_version=adjustment.adjustment_version,
                panel=panel_pairs,
            )
            if report.get("ready") is not True:
                codes = sorted(
                    str(item.get("code"))
                    for item in cast(Sequence[object], report.get("blockers", []))
                    if isinstance(item, Mapping)
                )
                raise IntrinsicEvidenceError(
                    "intrinsic_price_readiness_failed",
                    f"{component} readiness failed: {','.join(codes)}",
                )
        context_report = check_forward_context_readiness(
            str(Path(database).expanduser().resolve()),
            panel_pairs,
        )
        allowed = {
            "missing_finalized_context_rows",
            "signed_session_calendar_not_enrolled",
        }
        context_codes = {
            str(item.get("code"))
            for item in cast(
                Sequence[object], context_report.get("blockers", [])
            )
            if isinstance(item, Mapping)
        }
        if context_report.get("integrity_ready") is not True or not context_codes.issubset(
            allowed
        ):
            raise IntrinsicEvidenceError(
                "intrinsic_context_readiness_failed",
                f"decision context readiness failed: {','.join(sorted(context_codes))}",
            )

    connection = _connect(database)
    try:
        _validate_database_structure(connection)
        execution, execution_receipts = _price_artifact(
            connection,
            component="execution_prices",
            panel=canonical_panel,
            adjustment=execution_adjustment,
        )
        signal, signal_receipts = _price_artifact(
            connection,
            component="signal_prices",
            panel=canonical_panel,
            adjustment=signal_adjustment,
        )
        context, context_receipts = _context_artifact(
            connection, panel=canonical_panel, decision_cutoffs=decision_cutoffs
        )
    except sqlite3.Error as exc:
        raise IntrinsicEvidenceError(
            "intrinsic_database_schema_mismatch", "intrinsic database schema is incomplete"
        ) from exc
    finally:
        connection.close()
    receipts = {**execution_receipts, **signal_receipts, **context_receipts}
    return ReconstructedIntrinsicEvidence(
        components={
            "execution_prices": execution,
            "signal_prices": signal,
            "decision_context": context,
        },
        source_receipts=receipts,
    )


def verify_intrinsic_evidence(
    reconstructed: ReconstructedIntrinsicEvidence,
    *,
    claimed_components: Mapping[str, object],
    component_references: Mapping[str, ProviderArtifactReference],
    bound_source_receipts: Mapping[str, object],
    database_sha256: str,
) -> dict[str, dict[str, object]]:
    """Byte-compare rebuilt evidence and emit deterministic component verdicts."""

    if set(claimed_components) != set(INTRINSIC_COMPONENTS) or set(
        component_references
    ) != set(INTRINSIC_COMPONENTS):
        raise IntrinsicEvidenceError(
            "intrinsic_component_set_mismatch", "intrinsic component set is incomplete"
        )
    verdicts: dict[str, dict[str, object]] = {}
    for component in INTRINSIC_COMPONENTS:
        rebuilt = reconstructed.components[component]
        rebuilt_records = cast(
            Sequence[Mapping[str, object]], rebuilt["records"]
        )
        claimed = claimed_components[component]
        reference = component_references[component]
        blockers = []
        if _canonical(claimed) != _canonical(rebuilt) or (
            hashlib.sha256(_canonical(rebuilt)).hexdigest() != reference.identifier
        ):
            blockers.append({"code": "intrinsic_component_byte_mismatch", "count": 1})
        receipt_ids = sorted(
            {
                receipt_id
                for record in rebuilt_records
                for receipt_id in cast(Sequence[str], record["source_receipt_ids"])
            }
        )
        for receipt_id in receipt_ids:
            expected = reconstructed.source_receipts.get(receipt_id)
            supplied = bound_source_receipts.get(receipt_id)
            if expected is None or supplied != expected or hashlib.sha256(
                _canonical(supplied)
            ).hexdigest() != receipt_id:
                blockers.append(
                    {"code": "intrinsic_source_receipt_not_bound", "count": 1}
                )
                break
        evidence_payload = {
            "verifier_schema": INTRINSIC_VERIFIER_SCHEMA,
            "component": component,
            "database_sha256": database_sha256,
            "artifact": reference.to_dict(),
            "source_receipt_ids": receipt_ids,
            "coverage_count": len(rebuilt_records),
            "record_sha256s": [
                record["record_sha256"] for record in rebuilt_records
            ],
        }
        verdicts[component] = {
            "ready": not blockers,
            "blockers": blockers,
            **evidence_payload,
            "evidence_sha256": hashlib.sha256(_canonical(evidence_payload)).hexdigest(),
        }
    return verdicts


__all__ = [
    "DATABASE_SOURCE_RECEIPT_SCHEMA",
    "FORWARD_COMPONENTS",
    "INTRINSIC_COMPONENTS",
    "INTRINSIC_VERIFIER_SCHEMA",
    "IntrinsicEvidenceError",
    "ReconstructedForwardEvidence",
    "ReconstructedIntrinsicEvidence",
    "reconstruct_forward_component_evidence",
    "reconstruct_intrinsic_evidence",
    "verify_forward_component_evidence",
    "verify_intrinsic_evidence",
]
