"""Deterministic reconstruction of provider-owned RQGM evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import cast

from .adjustment_identity import VerifiedAdjustmentIdentity
from .availability import price_availability_error
from .execution_readiness import _structural_status, check_execution_readiness
from .forward_context import (
    SOURCE as CONTEXT_SOURCE,
    _TRIGGER_SQL as CONTEXT_TRIGGER_SQL,
    _market_symbol,
    _normalized_sql,
    _parse_raw_pages,
    check_forward_context_readiness,
)
from .rqgm_execution_export import _receipt_covers_bar
from .rqgm_provider_contract import COMPONENT_SCHEMAS, ProviderArtifactReference
from .ticker import normalize


INTRINSIC_COMPONENTS = (
    "execution_prices",
    "signal_prices",
    "decision_context",
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
    "INTRINSIC_COMPONENTS",
    "INTRINSIC_VERIFIER_SCHEMA",
    "IntrinsicEvidenceError",
    "ReconstructedIntrinsicEvidence",
    "reconstruct_intrinsic_evidence",
    "verify_intrinsic_evidence",
]
