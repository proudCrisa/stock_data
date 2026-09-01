"""Read-only execution-grade readiness checks for a stockdata cache."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .availability import price_availability_error
from .rqgm_execution_export import _receipt_covers_bar
from .ticker import normalize

SCHEMA_VERSION = 5
DAILY_PRIMARY_KEY = (
    "code", "date", "source", "adjustment_mode", "adjustment_version",
)
RECEIPT_TRIGGER_SQL = {
    "collection_receipts_no_update": """
        CREATE TRIGGER collection_receipts_no_update
        BEFORE UPDATE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END
    """,
    "collection_receipts_no_delete": """
        CREATE TRIGGER collection_receipts_no_delete
        BEFORE DELETE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END
    """,
}
DAILY_COLUMNS = {
    "code", "date", "open", "high", "low", "close", "volume", "source",
    "adjustment_mode", "adjustment_version", "retrieved_at", "is_final",
    "receipt_id",
}
RECEIPT_COLUMNS = {
    "receipt_id", "observed_at", "source", "request_json", "response_json",
    "response_sha256", "created_at",
}


def load_panel(path: str | Path) -> set[tuple[str, str]]:
    """Load exact ``symbol@date`` samples from a list or split overlay JSON."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "splits" in payload:
        values = payload.get("splits", {}).get("search-validation")
    elif isinstance(payload, dict):
        values = payload.get("panel")
    else:
        values = payload
    if not isinstance(values, list) or not values:
        raise ValueError("panel file must contain a non-empty sample list")
    panel = set()
    for value in values:
        symbol, separator, day = str(value).partition("@")
        if not separator:
            raise ValueError(f"invalid panel sample: {value!r}")
        key = (normalize(symbol), date.fromisoformat(day).isoformat())
        if key in panel:
            raise ValueError(f"duplicate panel sample: {value!r}")
        panel.add(key)
    return panel


def _blocker(code: str, items: Iterable[str]) -> dict[str, object] | None:
    values = sorted(set(items))
    if not values:
        return None
    return {"code": code, "count": len(values), "examples": values[:5]}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _primary_key(connection: sqlite3.Connection) -> tuple[str, ...]:
    columns = sorted(
        (int(row[5]), str(row[1]))
        for row in connection.execute("PRAGMA table_info(daily)")
        if row[5]
    )
    return tuple(name for _, name in columns)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";").lower()


class IdentityBoundReadConnection:
    """A read-only SQLite connection bound to a verified physical file identity.

    The file is opened with no-follow to capture its (dev, inode) identity.
    SQLite is then opened by pathname so WAL sidecar discovery works normally.
    The identity is re-verified immediately after opening and again when the
    caller closes the wrapper; any mismatch raises ``CollectorContinuityError``.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        opened: object,
        path: Path,
    ) -> None:
        self.connection = connection
        self._opened = opened
        self._path = path
        self.identity = opened.identity

    def close(self) -> None:
        from .collector_continuity import verify_file_identity

        try:
            verify_file_identity(str(self._path), self._opened.identity)
        finally:
            self.connection.close()
            self._opened.close()

    def __enter__(self) -> "IdentityBoundReadConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_readonly_identity_bound(
    database: str | Path,
) -> IdentityBoundReadConnection:
    """Open a read-only SQLite connection bound to the file's physical identity.

    WAL sidecars are discovered through the pathname while the actual identity
    is anchored by a held no-follow descriptor.
    """
    from .collector_continuity import (
        CollectorContinuityError,
        open_nofollow_regular,
        verify_file_identity,
    )

    path = Path(database).expanduser().resolve()
    opened = open_nofollow_regular(str(path))
    try:
        verify_file_identity(str(path), opened.identity)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        # Re-verify the path still resolves to the same inode after SQLite
        # opened it (catches atomic replacement between our open and SQLite's).
        current = open_nofollow_regular(str(path))
        try:
            if current.identity != opened.identity:
                raise CollectorContinuityError("database identity changed while opening")
        finally:
            current.close()
        return IdentityBoundReadConnection(connection, opened, path)
    except BaseException:
        opened.close()
        raise


def _is_legacy_v4_collector(
    connection: sqlite3.Connection,
    version: int,
    database: Path | None,
    expected_identity: object | None,
) -> bool:
    """Accept a frozen v4 collector whose immutable identity forbids migration.

    The calendar schema bump to v5 only added ``trading_calendar``; the collector
    contract binds the exact SQL of the remaining tables/triggers.  We therefore
    reuse the prepared-collector verifier to prove that this is a real,
    immutable collector: genesis claim, cohort, schema hash, guard triggers and
    ledger binding must all be valid, and the file being verified matches the
    ``expected_identity`` supplied by the caller's identity-bound open.
    Missing ``trading_calendar`` is handled as "no calendar data" by
    ``TradingCalendar`` downstream.
    """
    if version != 4 or database is None or expected_identity is None:
        return False
    # Reject ATTACHed or otherwise non-simple connections.
    if len(connection.execute("PRAGMA database_list").fetchall()) != 1:
        return False
    tables = _tables(connection)
    if "forward_collector_genesis" not in tables or "forward_capture_cohort" not in tables:
        return False
    from .collector_continuity import (
        CollectorContinuityError,
        default_collector_ledger_path,
        load_verified_prepared_collector,
        open_nofollow_regular,
        verify_file_identity,
    )

    opened_before: object | None = None
    opened_after: object | None = None
    try:
        opened_before = open_nofollow_regular(str(database))
        if opened_before.identity != expected_identity:
            return False
        verify_file_identity(str(database), opened_before.identity)
        load_verified_prepared_collector(
            database_path=str(database),
            ledger_path=default_collector_ledger_path(str(database)),
        )
        opened_after = open_nofollow_regular(str(database))
        if opened_after.identity != expected_identity:
            return False
        verify_file_identity(str(database), opened_after.identity)
        return True
    except (CollectorContinuityError, OSError, sqlite3.Error, ValueError):
        return False
    finally:
        if opened_before is not None:
            try:
                opened_before.close()
            except Exception:
                pass
        if opened_after is not None:
            try:
                opened_after.close()
            except Exception:
                pass


def _structural_status(
    connection: sqlite3.Connection,
    database: Path | None,
    expected_identity: object | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    tables = _tables(connection)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    legacy_v4_collector = _is_legacy_v4_collector(
        connection, version, database, expected_identity
    )
    triggers = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        )
    }
    missing_tables = sorted({"daily", "collection_receipts"} - tables)
    pk = _primary_key(connection) if "daily" in tables else ()
    invalid_triggers = sorted(
        name
        for name, expected_sql in RECEIPT_TRIGGER_SQL.items()
        if name not in triggers
        or _normalized_sql(triggers[name]) != _normalized_sql(expected_sql)
    )
    missing_columns: list[str] = []
    if "daily" in tables:
        missing_columns.extend(
            f"daily.{name}" for name in sorted(DAILY_COLUMNS - _columns(connection, "daily"))
        )
    if "collection_receipts" in tables:
        missing_columns.extend(
            f"collection_receipts.{name}"
            for name in sorted(
                RECEIPT_COLUMNS - _columns(connection, "collection_receipts")
            )
        )
    blockers = []
    for code, values in (
        ("schema_version_mismatch", [] if version == SCHEMA_VERSION or legacy_v4_collector else [str(version)]),
        ("missing_tables", missing_tables),
        ("missing_columns", missing_columns),
        ("daily_primary_key_mismatch", [] if pk == DAILY_PRIMARY_KEY else [",".join(pk)]),
        ("invalid_receipt_triggers", invalid_triggers),
    ):
        item = _blocker(code, values)
        if item:
            blockers.append(item)
    return {
        "version": version,
        "expected_version": SCHEMA_VERSION,
        "legacy_v4_collector_accepted": legacy_v4_collector,
        "daily_primary_key": list(pk),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "invalid_receipt_triggers": invalid_triggers,
    }, blockers


def _empty_result(database: Path, code: str) -> dict[str, object]:
    return {
        "database": str(database),
        "schema_version": None,
        "ready": False,
        "schema": {},
        "counts": {},
        "coverage": [],
        "request": {},
        "blockers": [{"code": code, "count": 1, "examples": [str(database)]}],
    }


def check_execution_readiness(
    database: str | Path,
    *,
    source: str | None = None,
    adjustment_mode: str | None = None,
    adjustment_version: str | None = None,
    panel: Iterable[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Return a machine-readable, fail-closed readiness report without writes."""
    identity = (source, adjustment_mode, adjustment_version)
    if any(value is not None for value in identity) and not all(identity):
        raise ValueError(
            "source, adjustment_mode, and adjustment_version must be provided together"
        )
    expected = (
        {(normalize(symbol), date.fromisoformat(day).isoformat()) for symbol, day in panel}
        if panel is not None else None
    )
    if expected is not None and not all(identity):
        raise ValueError("an exact panel requires an explicit price identity")
    if expected == set():
        raise ValueError("panel must not be empty")

    from .collector_continuity import CollectorContinuityError

    try:
        bound = open_readonly_identity_bound(database)
    except CollectorContinuityError:
        database_path = Path(database).expanduser().resolve()
        if not database_path.is_file():
            return _empty_result(database_path, "database_missing")
        return _empty_result(database_path, "database_unreadable")

    database = bound._path
    connection = bound.connection
    try:
        schema, blockers = _structural_status(
            connection, database, bound.identity
        )
        result: dict[str, object] = {
            "database": str(database),
            "schema_version": schema["version"],
            "ready": False,
            "schema": schema,
            "counts": {},
            "coverage": [],
            "request": {
                "source": source,
                "adjustment_mode": adjustment_mode,
                "adjustment_version": adjustment_version,
                "panel_size": len(expected) if expected is not None else None,
            },
            "blockers": blockers,
        }
        if blockers:
            return result

        counts = connection.execute(
            """
            SELECT COUNT(*) AS total_rows,
                   SUM(CASE WHEN is_final=1 THEN 1 ELSE 0 END) AS finalized_rows,
                   SUM(CASE WHEN receipt_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_rows,
                   SUM(CASE WHEN receipt_id IS NULL THEN 1 ELSE 0 END) AS missing_receipt_rows
            FROM daily
            """
        ).fetchone()
        receipt_count = int(
            connection.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0]
        )
        result_counts: dict[str, int] = {
            key: int(counts[key] or 0) for key in counts.keys()
        }
        result_counts["receipts"] = receipt_count
        result["counts"] = result_counts
        result["coverage"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source,adjustment_mode,adjustment_version,
                       COUNT(*) AS row_count,
                       SUM(CASE WHEN is_final=1 THEN 1 ELSE 0 END) AS finalized_rows,
                       SUM(CASE WHEN receipt_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_rows,
                       MIN(date) AS start_date,MAX(date) AS end_date
                FROM daily
                GROUP BY source,adjustment_mode,adjustment_version
                ORDER BY source,adjustment_mode,adjustment_version
                """
            )
        ]

        where = []
        parameters: list[object] = []
        if all(identity):
            where.append(
                "d.source=? AND d.adjustment_mode=? AND d.adjustment_version=?"
            )
            parameters.extend(identity)
        rows = connection.execute(
            """
            SELECT d.*,r.source AS receipt_source,r.observed_at,r.response_json,
                   r.response_sha256
            FROM daily AS d
            LEFT JOIN collection_receipts AS r ON r.receipt_id=d.receipt_id
            """ + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY d.code,d.date",
            parameters,
        ).fetchall()
        if expected is None:
            selected_rows = rows
            selected_keys = {
                (normalize(str(row["code"])), str(row["date"])) for row in rows
            }
        else:
            selected = {
                (normalize(str(row["code"])), str(row["date"])): row
                for row in rows
                if (normalize(str(row["code"])), str(row["date"])) in expected
            }
            selected_rows = list(selected.values())
            selected_keys = set(selected)
        if expected is not None:
            missing = [f"{symbol}@{day}" for symbol, day in expected - selected_keys]
            item = _blocker("missing_panel_rows", missing)
            if item:
                blockers.append(item)
        if not selected_rows:
            item = _blocker("no_selected_rows", ["selection"])
            if item:
                blockers.append(item)

        failures: dict[str, list[str]] = {
            "non_final_rows": [],
            "missing_receipts": [],
            "receipt_source_mismatch": [],
            "receipt_hash_mismatch": [],
            "receipt_response_mismatch": [],
            "receipt_timestamp_mismatch": [],
            "invalid_availability_timestamp": [],
            "availability_precedes_finalization": [],
            "unknown_next_session": [],
            "post_hoc_availability": [],
        }
        session_days = sorted({str(row["date"]) for row in selected_rows})
        next_session = dict(zip(session_days, session_days[1:]))
        for row in selected_rows:
            symbol = normalize(str(row["code"]))
            day = str(row["date"])
            sample = f"{symbol}@{day}"
            if int(row["is_final"]) != 1:
                failures["non_final_rows"].append(sample)
            if row["receipt_id"] is None or row["response_json"] is None:
                failures["missing_receipts"].append(sample)
                continue
            if row["receipt_source"] != row["source"]:
                failures["receipt_source_mismatch"].append(sample)
            response = str(row["response_json"])
            if hashlib.sha256(response.encode("utf-8")).hexdigest() != row["response_sha256"]:
                failures["receipt_hash_mismatch"].append(sample)
            elif not _receipt_covers_bar(response, row):
                failures["receipt_response_mismatch"].append(sample)
            try:
                retrieved_at = datetime.fromisoformat(
                    str(row["retrieved_at"]).replace("Z", "+00:00")
                )
                observed_at = datetime.fromisoformat(
                    str(row["observed_at"]).replace("Z", "+00:00")
                )
                if retrieved_at.tzinfo is None or observed_at.tzinfo is None:
                    raise ValueError("timezone required")
                if retrieved_at != observed_at:
                    failures["receipt_timestamp_mismatch"].append(sample)
                error = price_availability_error(
                    day, observed_at, next_session.get(day)
                )
                if error:
                    failures[error].append(sample)
            except (TypeError, ValueError):
                failures["invalid_availability_timestamp"].append(sample)
        for code, values in failures.items():
            item = _blocker(code, values)
            if item:
                blockers.append(item)
        result_counts["selected_rows"] = len(selected_rows)
        result["blockers"] = blockers
        result["ready"] = not blockers
        return result
    except CollectorContinuityError:
        return _empty_result(database, "database_identity_changed")
    except sqlite3.Error as exc:
        return _empty_result(database, f"database_error:{exc.__class__.__name__}")
    finally:
        bound.close()
