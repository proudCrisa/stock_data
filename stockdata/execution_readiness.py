"""Read-only execution-grade readiness checks for a stockdata cache."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
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


class VerifiedReadonlySnapshot:
    """A read-only SQLite connection opened against a private, verified snapshot.

    The original database file and any WAL sidecars are opened through a
    no-follow descriptor, their physical identity is verified, and their
    contents are copied into a private temporary directory.  SQLite then opens
    the temporary copy by ordinary pathname, so WAL sidecar discovery works
    without polluting or relying on the source directory.  The snapshot is
    destroyed on close.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        identity: object,
        source_path: Path,
        snapshot_dir: str,
    ) -> None:
        self.connection = connection
        self.identity = identity
        self.source_path = source_path
        self._snapshot_dir = snapshot_dir

    def close(self) -> None:
        from .collector_continuity import verify_file_identity

        try:
            verify_file_identity(str(self.source_path), self.identity)
        finally:
            try:
                self.connection.close()
            finally:
                try:
                    shutil.rmtree(self._snapshot_dir, ignore_errors=True)
                except Exception:
                    pass

    def __enter__(self) -> "VerifiedReadonlySnapshot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


MAX_SNAPSHOT_CAPTURE_RETRIES = 3


def _copy_fd_to_file(source_fd: int, destination: Path) -> None:
    """Copy all readable bytes from ``source_fd`` into ``destination``.

    The descriptor is seeked to 0 first so repeated reads from the same
    anchored file produce a full, consistent copy.  The descriptor itself is
    left open for the caller.
    """
    os.lseek(source_fd, 0, os.SEEK_SET)
    with os.fdopen(source_fd, "rb", closefd=False) as source, open(destination, "wb") as sink:
        shutil.copyfileobj(source, sink)


def _hash_fd_content(fd: int) -> str:
    """Return SHA-256 of all bytes reachable through ``fd`` from its start."""
    os.lseek(fd, 0, os.SEEK_SET)
    hasher = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1_048_576)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def _hash_file(path: Path) -> str:
    """Return SHA-256 of the file at ``path``."""
    hasher = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(1_048_576)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_copy_stable(source_fd: int, destination: Path) -> None:
    """Raise ``CollectorContinuityError`` if source drifted during copy."""
    from .collector_continuity import CollectorContinuityError

    copied_hash = _hash_file(destination)
    current_hash = _hash_fd_content(source_fd)
    if copied_hash != current_hash:
        raise CollectorContinuityError("snapshot source changed during copy")


def _close_snapshot_files(opened_files: Iterable[object]) -> None:
    first_error: BaseException | None = None
    for opened in opened_files:
        try:
            getattr(opened, "close")()
        except BaseException as exc:
            if first_error is not None:
                exc.__cause__ = first_error
            first_error = exc
    if first_error is not None:
        raise first_error


def open_verified_readonly_snapshot(
    database: str | Path,
) -> VerifiedReadonlySnapshot:
    """Open a read-only SQLite connection against a verified private snapshot.

    The source database and any WAL sidecars are captured through held,
    identity-verified file descriptors.  After copying, each component is
    re-read from the same descriptor and hashed; if any component changed
    during the copy (checkpoint, writer, truncation, replacement), the entire
    snapshot is discarded and retried.  This guarantees the temporary copy is
    point-in-time consistent without ever falling back to a pathname-based
    reopen, which would reintroduce A→B→A swap races.
    """
    from .collector_continuity import (
        CollectorContinuityError,
        open_nofollow_regular,
        verify_file_identity,
    )

    source_path = Path(database).expanduser().resolve()
    source_str = str(source_path)
    main_opened = open_nofollow_regular(source_str)
    try:
        verify_file_identity(source_str, main_opened.identity)
    except BaseException:
        main_opened.close()
        raise

    snapshot_dir: str | None = None
    try:
        last_error: BaseException | None = None
        for attempt in range(MAX_SNAPSHOT_CAPTURE_RETRIES):
            snapshot_dir = tempfile.mkdtemp(prefix="stockdata_readonly_snapshot_")
            sidecars: list[tuple[Path, object, Path]] = []
            connection: sqlite3.Connection | None = None
            snapshot: VerifiedReadonlySnapshot | None = None
            primary_error: BaseException | None = None
            try:
                snapshot_path = Path(snapshot_dir) / source_path.name
                _copy_fd_to_file(main_opened.descriptor, snapshot_path)
                verify_file_identity(source_str, main_opened.identity)
                _verify_copy_stable(main_opened.descriptor, snapshot_path)

                for suffix in ("-wal", "-shm"):
                    sidecar_path = Path(f"{source_str}{suffix}")
                    if not os.path.lexists(sidecar_path):
                        continue
                    sidecar_opened = open_nofollow_regular(str(sidecar_path))
                    sidecar_destination = (
                        Path(snapshot_dir) / f"{source_path.name}{suffix}"
                    )
                    sidecars.append((sidecar_path, sidecar_opened, sidecar_destination))
                    verify_file_identity(str(sidecar_path), sidecar_opened.identity)
                    _copy_fd_to_file(sidecar_opened.descriptor, sidecar_destination)

                # Validate the complete main/sidecar set only after every copy.
                # This closes the checkpoint window between the first main-file
                # validation and WAL discovery.
                verify_file_identity(source_str, main_opened.identity)
                _verify_copy_stable(main_opened.descriptor, snapshot_path)
                captured_paths = {path for path, _, _ in sidecars}
                for sidecar_path, sidecar_opened, sidecar_destination in sidecars:
                    verify_file_identity(str(sidecar_path), sidecar_opened.identity)
                    _verify_copy_stable(
                        sidecar_opened.descriptor, sidecar_destination
                    )
                for suffix in ("-wal", "-shm"):
                    sidecar_path = Path(f"{source_str}{suffix}")
                    if (
                        sidecar_path not in captured_paths
                        and os.path.lexists(sidecar_path)
                    ):
                        raise CollectorContinuityError(
                            "snapshot sidecar set changed during copy"
                        )

                connection = sqlite3.connect(str(snapshot_path))
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row
                snapshot = VerifiedReadonlySnapshot(
                    connection, main_opened.identity, source_path, snapshot_dir
                )
            except BaseException as exc:
                primary_error = exc

            cleanup_error: BaseException | None = None
            try:
                _close_snapshot_files(opened for _, opened, _ in sidecars)
            except BaseException as exc:
                cleanup_error = exc

            if primary_error is None and cleanup_error is None:
                if snapshot is None:
                    raise AssertionError("verified snapshot is unavailable")
                return snapshot

            if connection is not None:
                try:
                    connection.close()
                except BaseException as exc:
                    if cleanup_error is not None:
                        exc.__cause__ = cleanup_error
                    cleanup_error = exc
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            snapshot_dir = None

            if primary_error is not None and cleanup_error is not None:
                raise primary_error from cleanup_error
            if cleanup_error is not None:
                raise cleanup_error
            if isinstance(primary_error, CollectorContinuityError):
                last_error = primary_error
                if attempt == MAX_SNAPSHOT_CAPTURE_RETRIES - 1:
                    raise CollectorContinuityError(
                        f"unable to capture stable snapshot after "
                        f"{MAX_SNAPSHOT_CAPTURE_RETRIES} attempts"
                    ) from primary_error
                continue
            if primary_error is None:
                raise AssertionError("snapshot attempt failed without an error")
            raise primary_error
        raise CollectorContinuityError(
            f"unable to capture stable snapshot after {MAX_SNAPSHOT_CAPTURE_RETRIES} attempts"
        ) from last_error
    finally:
        main_opened.close()


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
        bound = open_verified_readonly_snapshot(database)
    except CollectorContinuityError:
        database_path = Path(database).expanduser().resolve()
        if not database_path.is_file():
            return _empty_result(database_path, "database_missing")
        return _empty_result(database_path, "database_unreadable")

    source_path = bound.source_path
    connection = bound.connection

    def _check() -> dict[str, object]:
        schema, blockers = _structural_status(
            connection, source_path, bound.identity
        )
        result: dict[str, object] = {
            "database": str(source_path),
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

    result: dict[str, object] | None = None
    body_exc: BaseException | None = None
    close_identity_changed = False
    try:
        result = _check()
    except CollectorContinuityError:
        result = _empty_result(source_path, "database_identity_changed")
    except sqlite3.Error as exc:
        result = _empty_result(source_path, f"database_error:{exc.__class__.__name__}")
    except BaseException as exc:
        body_exc = exc
    finally:
        try:
            bound.close()
        except CollectorContinuityError:
            result = _empty_result(source_path, "database_identity_changed")
            close_identity_changed = True
    if close_identity_changed:
        return result
    if body_exc is not None:
        raise body_exc
    if result is None:
        raise AssertionError("execution readiness result is unavailable")
    return result
