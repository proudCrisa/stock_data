"""Fail-closed registration of a future exact panel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .authority import (
    AUTHORITY_COMPONENT_ROLES,
    load_provider_trust_registry,
    require_enrolled_role_coverage,
)
from .cache import (
    _CALENDAR_SCHEMA,
    _RECEIPT_SCHEMA,
    _SCHEMA,
    _SCHEMA_VERSION,
    _SYNC_SCHEMA,
)
from .collector_continuity import (
    CollectorContinuityError,
    OpenedRegularFile,
    acquire_collector_registration_lock,
    append_collector_ledger_event,
    canonical_json_sha256,
    canonical_collector_path,
    create_nofollow_regular,
    create_exclusive_collector_files,
    decode_capability,
    default_collector_ledger_path,
    fsync_parent_directory,
    initialize_prepared_collector,
    load_verified_prepared_collector,
    open_exact_collector_sqlite,
    open_nofollow_regular,
    parse_collector_ledger,
    remove_created_collector_artifacts,
    verify_file_identity,
    verify_collector_evidence_triggers,
)
from .execution_readiness import _structural_status
from .forward_capture import _bind_cohort
from .forward_context import _ensure_schema as ensure_context_schema
from .forward_corporate_actions import _ensure_schema as ensure_action_schema
from .provider_authority_admission import (
    admit_signed_component_authority,
    preregister_generic_market_rulebook,
    validate_local_mechanical_prerequisites,
)
from .ticker import normalize


REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/4"
TRUSTED_LOCAL_REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/5"
TRUSTED_LOCAL_AUTHORITY_MODE = "trusted_local_mechanical"
COLLECTOR_CAPABILITY_SCHEMA = "stockdata-forward-collector-capability/2"
SOURCE = "tencent"
ADJUSTMENT_VERSION = "tencent-qt-daily-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CONTEXT_TABLES = {
    "forward_context_observations",
    "forward_universe_observations",
    "forward_status_observations",
}
_ACTION_TABLES = {
    "forward_corporate_action_coverage",
    "forward_corporate_actions",
}
class FuturePanelRegistrationError(ValueError):
    """Raised when a future panel cannot be registered safely."""


class _FreshCollectorSchema:
    """Minimal adapter for existing forward-schema installers during preparation."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection


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
        raise FuturePanelRegistrationError("registration input is not canonical JSON") from exc


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FuturePanelRegistrationError("registration input has duplicate keys")
        result[key] = value
    return result


def _read_json(path: str | Path, field: str) -> object:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise FuturePanelRegistrationError(f"{field} must name a regular file")
    try:
        raw = candidate.read_bytes()
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FuturePanelRegistrationError(f"{field} is unreadable or invalid JSON") from exc


def _read_canonical_json(path: str | Path, field: str) -> object:
    value = _read_json(path, field)
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise FuturePanelRegistrationError(f"{field} is unreadable") from exc
    if raw != _canonical(value):
        raise FuturePanelRegistrationError(f"{field} must use canonical JSON bytes")
    return value


def _now() -> datetime:
    return datetime.now(_SHANGHAI)


def _panel(path: str | Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    value = _read_canonical_json(path, "panel file")
    if not isinstance(value, list) or len(value) != 36:
        raise FuturePanelRegistrationError("future panel must contain exactly 36 entries")
    entries: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise FuturePanelRegistrationError("future panel entries must be strings")
        symbol, separator, day = item.partition("@")
        if not separator:
            raise FuturePanelRegistrationError("future panel entry is invalid")
        try:
            entry = f"{normalize(symbol)}@{date.fromisoformat(day).isoformat()}"
        except (TypeError, ValueError) as exc:
            raise FuturePanelRegistrationError("future panel entry is invalid") from exc
        entries.append(entry)
    canonical = tuple(sorted(entries))
    if tuple(entries) != canonical or len(set(canonical)) != 36:
        raise FuturePanelRegistrationError("future panel must be sorted and unique")
    symbols = tuple(sorted({entry.split("@")[0] for entry in canonical}))
    sessions = tuple(sorted({entry.split("@")[1] for entry in canonical}))
    if len(symbols) != 12 or len(sessions) != 3 or canonical != tuple(
        sorted(f"{symbol}@{day}" for symbol in symbols for day in sessions)
    ):
        raise FuturePanelRegistrationError("future panel must be an exact 12-by-3 product")
    return canonical, symbols, sessions


def _source_receipts(paths: Sequence[str | Path]) -> dict[str, object]:
    receipts: dict[str, object] = {}
    for index, path in enumerate(paths):
        value = _read_canonical_json(path, f"source receipt {index}")
        if not isinstance(value, Mapping):
            raise FuturePanelRegistrationError("source receipt must be a JSON object")
        receipt_id = hashlib.sha256(_canonical(dict(value))).hexdigest()
        if receipt_id in receipts:
            raise FuturePanelRegistrationError("source receipt is duplicated")
        receipts[receipt_id] = value
    if not receipts:
        raise FuturePanelRegistrationError("at least one source receipt is required")
    return receipts


def _cohort(symbols: Sequence[str], first_session: str) -> dict[str, object]:
    return {
        "symbols": list(symbols),
        "start": first_session,
        "source": SOURCE,
        "adjustment_mode": "raw",
        "adjustment_version": ADJUSTMENT_VERSION,
    }


def _install_fresh_collector_schema(
    connection: sqlite3.Connection, *, symbols: Sequence[str], first_session: str
) -> None:
    """Install the current collector schema only into an exclusively created database."""

    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','trigger') LIMIT 1"
    ).fetchone()
    if existing is not None:
        raise FuturePanelRegistrationError("new collector database is not empty")
    try:
        connection.execute(_SCHEMA)
        connection.executescript(_SYNC_SCHEMA)
        connection.executescript(_RECEIPT_SCHEMA)
        connection.executescript(_CALENDAR_SCHEMA)
        connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        cache = _FreshCollectorSchema(connection)
        ensure_context_schema(cache)  # type: ignore[arg-type]
        ensure_action_schema(cache)  # type: ignore[arg-type]
        _bind_cohort(cache, _cohort(symbols, first_session))  # type: ignore[arg-type]
        connection.commit()
    except (sqlite3.Error, ValueError) as exc:
        connection.rollback()
        raise FuturePanelRegistrationError("fresh collector schema cannot be installed") from exc


def prepare_future_collector_database(
    *, database_file: str | Path, panel_file: str | Path
) -> dict[str, object]:
    """Create one empty, immutable collector cohort without fetching market data."""

    _, symbols, sessions = _panel(panel_file)
    now = _now().astimezone(_SHANGHAI)
    if any(day <= now.date().isoformat() for day in sessions):
        raise FuturePanelRegistrationError("collector panel is not wholly in the future")
    database_path = canonical_collector_path(database_file)
    ledger_path = default_collector_ledger_path(database_path)
    database_identity = None
    ledger_identity = None
    try:
        opened = create_exclusive_collector_files(
            database_path=database_path,
            ledger_path=ledger_path,
        )
        try:
            database_identity, ledger_identity = opened.verify_identities()
        finally:
            opened.close()
        with open_exact_collector_sqlite(
            database_path=database_path,
            ledger_path=ledger_path,
        ) as (connection, _):
            _install_fresh_collector_schema(
                connection,
                symbols=symbols,
                first_session=sessions[0],
            )
        return initialize_prepared_collector(
            database_path=database_path,
            ledger_path=ledger_path,
            created_at=now.isoformat(),
        )
    except (CollectorContinuityError, OSError, sqlite3.Error, ValueError) as exc:
        if database_identity is not None and ledger_identity is not None:
            remove_created_collector_artifacts(
                database_identity=database_identity,
                ledger_identity=ledger_identity,
            )
        if isinstance(exc, FuturePanelRegistrationError):
            raise
        raise FuturePanelRegistrationError("collector database preparation failed") from exc


def verify_collector_capability(
    database: str | Path,
    *,
    symbols: Sequence[str],
    first_session: str,
    require_clean: bool = True,
) -> dict[str, object]:
    """Read-only verification that the fixed forward collectors can use the database."""

    path = Path(canonical_collector_path(os.path.abspath(os.fspath(database))))
    ledger_path = default_collector_ledger_path(str(path))
    try:
        prepared = load_verified_prepared_collector(
            database_path=str(path), ledger_path=ledger_path
        )
        with open_exact_collector_sqlite(
            database_path=str(path), ledger_path=ledger_path
        ) as (connection, opened):
            structure, blockers = _structural_status(
                connection, path, opened.database.identity
            )
            if blockers:
                raise FuturePanelRegistrationError("collector database price schema is invalid")
            cohort_rows = connection.execute(
                "SELECT singleton,spec_json,spec_sha256 FROM main.forward_capture_cohort"
            ).fetchall()
            expected_json = _canonical(_cohort(symbols, first_session)).decode("ascii")
            expected_sha256 = hashlib.sha256(expected_json.encode("ascii")).hexdigest()
            if (
                len(cohort_rows) != 1
                or int(cohort_rows[0][0]) != 1
                or str(cohort_rows[0][1]) != expected_json
                or str(cohort_rows[0][2]) != expected_sha256
            ):
                raise FuturePanelRegistrationError("collector cohort identity is invalid")
            verify_collector_evidence_triggers(connection, require_exact_set=True)
            evidence_tables = {
                "daily", "collection_receipts", "sync_coverage", *_CONTEXT_TABLES, *_ACTION_TABLES,
            }
            nonempty = sorted(
                table for table in evidence_tables
                if connection.execute(f'SELECT 1 FROM main."{table}" LIMIT 1').fetchone() is not None
            )
            if require_clean and nonempty:
                raise FuturePanelRegistrationError(
                    f"collector database is not a clean future cohort: {', '.join(nonempty)}"
                )
        capability = {
            "schema_version": COLLECTOR_CAPABILITY_SCHEMA,
            "database_path": prepared["database_path"],
            "ledger_path": prepared["ledger_path"],
            "source": SOURCE,
            "adjustment_mode": "raw",
            "adjustment_version": ADJUSTMENT_VERSION,
            "collector_schema_sha256": prepared["collector_schema_sha256"],
            "database_identity": prepared["database_identity"],
            "ledger_identity": prepared["ledger_identity"],
            "database_uuid": prepared["database_uuid"],
            "cohort_sha256": prepared["cohort_sha256"],
            "genesis_sha256": prepared["genesis_sha256"],
            "ledger_genesis_event_sha256": prepared["ledger_genesis_event_sha256"],
        }
        return decode_capability(_canonical(capability))
    except (CollectorContinuityError, sqlite3.Error, OSError, ValueError) as exc:
        if isinstance(exc, FuturePanelRegistrationError):
            raise
        raise FuturePanelRegistrationError("collector database cannot be verified") from exc


def _write_exclusive(path: Path, payload: Mapping[str, object]) -> OpenedRegularFile:
    """Create and retain the no-follow authority for canonical registration bytes."""

    path = Path(canonical_collector_path(os.path.abspath(os.fspath(path))))
    if not path.parent.is_dir():
        raise FuturePanelRegistrationError("registration output directory does not exist")
    payload_bytes = _canonical(dict(payload))
    opened = None
    success = False
    try:
        opened = create_nofollow_regular(path)
        offset = 0
        while offset < len(payload_bytes):
            written = os.write(opened.descriptor, payload_bytes[offset:])
            if written <= 0:
                raise OSError("registration output short write")
            offset += written
        os.fsync(opened.descriptor)
        fsync_parent_directory(str(path))
        _verify_registration_authority(path, opened, payload_bytes)
        success = True
        return opened
    except FileExistsError as exc:
        raise FuturePanelRegistrationError("registration output already exists") from exc
    except (CollectorContinuityError, OSError) as exc:
        raise FuturePanelRegistrationError("registration output cannot be written") from exc
    except Exception:
        raise
    finally:
        if opened is not None and not success:
            opened.close()


_REGISTRATION_FIELDS = {
    "schema_version", "registered_at", "as_of", "symbols", "sessions", "source",
    "adjustment_mode", "adjustment_version", "database_path", "panel_sha256",
    "workspace_count", "outcome_feedback_used", "status", "prerequisite_files",
    "prerequisites", "prerequisites_sha256",
}
_TRUSTED_LOCAL_REGISTRATION_FIELDS = _REGISTRATION_FIELDS | {"authority_mode"}


def _verify_registration_authority(
    path: Path, opened: OpenedRegularFile, expected: bytes
) -> None:
    """Prove that the opened registration still is the canonical output bytes."""

    try:
        verify_file_identity(path, opened.identity)
        size = os.fstat(opened.descriptor).st_size
        if size != len(expected) or os.pread(opened.descriptor, size, 0) != expected:
            raise FuturePanelRegistrationError("registration output changed")
        verify_file_identity(path, opened.identity)
    except (CollectorContinuityError, OSError) as exc:
        raise FuturePanelRegistrationError("registration output changed") from exc


def _open_existing_registration(
    path: Path,
) -> tuple[bytes, dict[str, object], OpenedRegularFile] | None:
    """Open an existing registration and retain its no-follow file authority."""

    canonical = canonical_collector_path(os.path.abspath(os.fspath(path)))
    try:
        os.lstat(canonical)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FuturePanelRegistrationError("registration output cannot be inspected") from exc
    opened: OpenedRegularFile | None = None
    try:
        opened = open_nofollow_regular(canonical)
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise FuturePanelRegistrationError("registration output is invalid")
        raw = os.pread(opened.descriptor, size, 0)
        if len(raw) != size:
            raise FuturePanelRegistrationError("registration output is truncated")
        _verify_registration_authority(Path(canonical), opened, raw)
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except FuturePanelRegistrationError:
        if opened is not None:
            opened.close()
        raise
    except (CollectorContinuityError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if opened is not None:
            opened.close()
        raise FuturePanelRegistrationError("registration output is invalid") from exc
    schema = value.get("schema_version") if isinstance(value, dict) else None
    expected_fields = (
        _REGISTRATION_FIELDS
        if schema == REGISTRATION_SCHEMA
        else _TRUSTED_LOCAL_REGISTRATION_FIELDS
    )
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or schema not in {REGISTRATION_SCHEMA, TRUSTED_LOCAL_REGISTRATION_SCHEMA}
        or (
            schema == TRUSTED_LOCAL_REGISTRATION_SCHEMA
            and value.get("authority_mode") != TRUSTED_LOCAL_AUTHORITY_MODE
        )
        or raw != _canonical(value)
    ):
        assert opened is not None
        opened.close()
        raise FuturePanelRegistrationError("registration output is invalid")
    assert opened is not None
    return raw, value, opened


def _read_existing_registration(path: Path) -> tuple[bytes, dict[str, object]] | None:
    """Read an existing registration without retaining a write authority."""

    existing = _open_existing_registration(path)
    if existing is None:
        return None
    raw, value, opened = existing
    opened.close()
    return raw, value


def _registered_at(value: Mapping[str, object]) -> datetime:
    try:
        registered_at = datetime.fromisoformat(str(value["registered_at"]))
    except ValueError as exc:
        raise FuturePanelRegistrationError("registered_at must be timezone-aware") from exc
    if (
        registered_at.tzinfo is None
        or registered_at.utcoffset() is None
        or registered_at.isoformat() != value["registered_at"]
        or value.get("as_of") != registered_at.astimezone(_SHANGHAI).date().isoformat()
    ):
        raise FuturePanelRegistrationError("registered_at must be timezone-aware")
    return registered_at.astimezone(_SHANGHAI)


def _binding_matches(
    event: Mapping[str, object], *, registration_sha256: str, panel_sha256: str,
    sessions: Sequence[str], prerequisites_sha256: str,
) -> bool:
    required = {
        "registration_sha256", "panel_sha256", "sessions", "sessions_sha256",
        "prerequisites_sha256", "bound_at",
    }
    if set(event) != required:
        return False
    try:
        bound_at = datetime.fromisoformat(str(event["bound_at"]))
    except ValueError:
        return False
    return (
        bound_at.tzinfo is not None
        and bound_at.utcoffset() is not None
        and bound_at.isoformat() == event["bound_at"]
        and event["registration_sha256"] == registration_sha256
        and event["panel_sha256"] == panel_sha256
        and event["sessions"] == list(sessions)
        and event["sessions_sha256"] == canonical_json_sha256(list(sessions))
        and event["prerequisites_sha256"] == prerequisites_sha256
    )


def _prerequisite_files(
    *,
    source_receipt_files: Sequence[str | Path],
    calendar_file: str | Path,
    calendar_authority_file: str | Path | None,
    market_rules_file: str | Path,
    market_rules_authority_file: str | Path | None,
    authority_mode: str,
) -> dict[str, object]:
    files: dict[str, object] = {
        "source_receipts": sorted(
            str(Path(path).expanduser().resolve()) for path in source_receipt_files
        ),
        "trading_calendar": str(Path(calendar_file).expanduser().resolve()),
        "market_rules": str(Path(market_rules_file).expanduser().resolve()),
    }
    if authority_mode == "signed":
        if calendar_authority_file is None or market_rules_authority_file is None:
            raise FuturePanelRegistrationError("signed registration requires authority files")
        files["trading_calendar_authority"] = str(
            Path(calendar_authority_file).expanduser().resolve()
        )
        files["market_rules_authority"] = str(
            Path(market_rules_authority_file).expanduser().resolve()
        )
    return files


def _static_prerequisites(
    *,
    panel: Sequence[str],
    files: Mapping[str, object],
    registered_at: datetime,
    coverage_from: datetime,
    authority_mode: str,
) -> dict[str, object]:
    receipt_files = files.get("source_receipts")
    if (
        set(files)
        != (
            {
                "source_receipts", "trading_calendar", "trading_calendar_authority",
                "market_rules", "market_rules_authority",
            }
            if authority_mode == "signed"
            else {"source_receipts", "trading_calendar", "market_rules"}
        )
        or not isinstance(receipt_files, list)
        or not receipt_files
        or any(not isinstance(path, str) for path in receipt_files)
    ):
        raise FuturePanelRegistrationError("registration prerequisite files are invalid")
    try:
        receipts = _source_receipts(receipt_files)
        if authority_mode == TRUSTED_LOCAL_AUTHORITY_MODE:
            local = validate_local_mechanical_prerequisites(
                calendar_artifact=_read_canonical_json(
                    str(files["trading_calendar"]), "calendar artifact"
                ),
                market_rules_artifact=_read_canonical_json(
                    str(files["market_rules"]), "generic market-rule artifact"
                ),
                expected_panel=panel,
                bound_source_receipts=receipts,
            )
            available_at = max(
                datetime.fromisoformat(value)
                for prerequisite in (
                    local["trading_calendar"], local["market_rule_prerequisite"]
                )
                for value in prerequisite["available_at_by_panel"].values()
            )
            if available_at > registered_at:
                raise FuturePanelRegistrationError(
                    "static prerequisite was unavailable at registration"
                )
            return local
        registry = load_provider_trust_registry()
        calendar = admit_signed_component_authority(
            component="trading_calendar",
            artifact_value=_read_canonical_json(
                str(files["trading_calendar"]), "calendar authority artifact"
            ),
            authority_envelope=_read_canonical_json(
                str(files["trading_calendar_authority"]),
                "calendar authority envelope",
            ),
            expected_panel=panel,
            bound_source_receipts=receipts,
            registry=registry,
        )
        market_rule_prerequisite = preregister_generic_market_rulebook(
            artifact_value=_read_canonical_json(
                str(files["market_rules"]), "generic market-rule artifact"
            ),
            authority_envelope=_read_canonical_json(
                str(files["market_rules_authority"]),
                "generic market-rule authority envelope",
            ),
            expected_panel=panel,
            bound_source_receipts=receipts,
            registry=registry,
            decision_cutoff_by_panel=calendar.decision_cutoff_by_panel,
        )
    except ValueError as exc:
        raise FuturePanelRegistrationError(str(exc)) from exc

    available_at = max(
        datetime.fromisoformat(value)
        for authority in (calendar, market_rule_prerequisite)
        for value in authority.available_at_by_panel.values()
    )
    if available_at > registered_at:
        raise FuturePanelRegistrationError("static authority was unavailable at registration")
    coverage_until = max(
        datetime.fromisoformat(phases["next_session_decision_cutoff_at"])
        for phases in calendar.signed_calendar_phases_by_panel.values()
    )
    try:
        role_publishers = require_enrolled_role_coverage(
            registry,
            roles=tuple(AUTHORITY_COMPONENT_ROLES),
            valid_from=max(registered_at, coverage_from),
            valid_until=coverage_until,
        )
    except ValueError as exc:
        raise FuturePanelRegistrationError(str(exc)) from exc
    return {
        "trust_registry_sha256": registry.registry_sha256,
        "role_publishers": dict(role_publishers),
        "trading_calendar": {
            "artifact_sha256": calendar.artifact.identifier,
            "signature_sha256": calendar.signature_id,
        },
        "market_rule_prerequisite": market_rule_prerequisite.prerequisite_evidence(),
    }


def reverify_registration_prerequisites(
    *,
    prerequisites: Mapping[str, object],
    prerequisite_files: Mapping[str, object],
    database_file: str | Path,
    panel: Sequence[str],
    symbols: Sequence[str],
    first_session: str,
    registered_at: datetime,
    observed_at: datetime,
    authority_mode: str = "signed",
) -> None:
    """Recompute every registration prerequisite immediately before capture."""

    static = _static_prerequisites(
        panel=panel,
        files=prerequisite_files,
        registered_at=registered_at,
        coverage_from=observed_at,
        authority_mode=authority_mode,
    )
    collector = verify_collector_capability(
        database_file,
        symbols=symbols,
        first_session=first_session,
        require_clean=False,
    )
    if prerequisites != {**static, "collector": collector}:
        raise FuturePanelRegistrationError(
            "registration prerequisites differ from current verified evidence"
        )


def register_future_panel(
    *,
    output_file: str | Path,
    database_file: str | Path,
    panel_file: str | Path,
    source_receipt_files: Sequence[str | Path],
    calendar_file: str | Path,
    market_rules_file: str | Path,
    calendar_authority_file: str | Path | None = None,
    market_rules_authority_file: str | Path | None = None,
    authority_mode: str = "signed",
) -> dict[str, object]:
    """Register one future panel after independently recomputing every prerequisite."""

    if authority_mode not in {"signed", TRUSTED_LOCAL_AUTHORITY_MODE}:
        raise FuturePanelRegistrationError("authority_mode is invalid")
    if authority_mode == TRUSTED_LOCAL_AUTHORITY_MODE and (
        calendar_authority_file is not None or market_rules_authority_file is not None
    ):
        raise FuturePanelRegistrationError(
            "trusted_local_mechanical does not accept authority files"
        )
    panel, symbols, sessions = _panel(panel_file)
    database_path = canonical_collector_path(os.path.abspath(os.fspath(database_file)))
    ledger_path = default_collector_ledger_path(database_path)
    output_path = Path(canonical_collector_path(os.path.abspath(os.fspath(output_file))))
    files = _prerequisite_files(
        source_receipt_files=source_receipt_files,
        calendar_file=calendar_file,
        calendar_authority_file=calendar_authority_file,
        market_rules_file=market_rules_file,
        market_rules_authority_file=market_rules_authority_file,
        authority_mode=authority_mode,
    )
    try:
        with acquire_collector_registration_lock(
            database_path=database_path, ledger_path=ledger_path
        ) as opened:
            ledger = parse_collector_ledger(opened.ledger)
            if len(ledger) not in {1, 2}:
                raise FuturePanelRegistrationError("collector ledger is not a resumable registration")
            if len(ledger) == 2 and ledger[1]["event_type"] != "REGISTRATION_BOUND":
                raise FuturePanelRegistrationError("collector ledger is not a resumable registration")

            registration = _open_existing_registration(output_path)
            if registration is None and len(ledger) != 1:
                # A bound ledger is never authority to recreate a deleted registration.
                raise FuturePanelRegistrationError("collector ledger is not a resumable registration")
            registration_authority = registration[2] if registration is not None else None
            try:
                registered_at = _registered_at(registration[1]) if registration else _now()
                if registered_at.tzinfo is None or registered_at.utcoffset() is None:
                    raise FuturePanelRegistrationError("registered_at must be timezone-aware")
                registered_at = registered_at.astimezone(_SHANGHAI)
                if any(
                    day <= registered_at.date().isoformat()
                    or date.fromisoformat(day).weekday() >= 5
                    for day in sessions
                ):
                    raise FuturePanelRegistrationError(
                        "every registered session must be a future trading weekday"
                    )
                collector = verify_collector_capability(
                    database_path, symbols=symbols, first_session=sessions[0]
                )
                static = _static_prerequisites(
                    panel=panel,
                    files=files,
                    registered_at=registered_at,
                    coverage_from=registered_at,
                    authority_mode=authority_mode,
                )
                prerequisites: dict[str, object] = {**static, "collector": collector}
                result: dict[str, object] = {
                    "schema_version": (
                        REGISTRATION_SCHEMA
                        if authority_mode == "signed"
                        else TRUSTED_LOCAL_REGISTRATION_SCHEMA
                    ),
                    "registered_at": registered_at.isoformat(),
                    "as_of": registered_at.date().isoformat(),
                    "symbols": list(symbols),
                    "sessions": list(sessions),
                    "source": SOURCE,
                    "adjustment_mode": "raw",
                    "adjustment_version": ADJUSTMENT_VERSION,
                    "database_path": collector["database_path"],
                    "panel_sha256": hashlib.sha256(_canonical(list(panel))).hexdigest(),
                    "workspace_count": 36,
                    "outcome_feedback_used": False,
                    "status": "AWAITING_FULL_SNAPSHOT_READINESS",
                    "prerequisite_files": files,
                    "prerequisites": prerequisites,
                    "prerequisites_sha256": hashlib.sha256(_canonical(prerequisites)).hexdigest(),
                }
                if authority_mode != "signed":
                    result["authority_mode"] = authority_mode
                raw = _canonical(result)
                registration_sha256 = hashlib.sha256(raw).hexdigest()
                if registration is not None:
                    if registration[0] != raw:
                        raise FuturePanelRegistrationError(
                            "registration output differs from current prerequisites"
                        )
                    _verify_registration_authority(
                        output_path, registration_authority, raw
                    )
                else:
                    registration_authority = _write_exclusive(output_path, result)

                if len(ledger) == 1:
                    _verify_registration_authority(output_path, registration_authority, raw)
                    opened.verify_identities()
                    bound_at = _now()
                    if bound_at.tzinfo is None or bound_at.utcoffset() is None:
                        raise FuturePanelRegistrationError("bound_at must be timezone-aware")
                    append_collector_ledger_event(
                        opened.ledger,
                        event_type="REGISTRATION_BOUND",
                        event={
                            "registration_sha256": registration_sha256,
                            "panel_sha256": result["panel_sha256"],
                            "sessions": list(sessions),
                            "sessions_sha256": canonical_json_sha256(list(sessions)),
                            "prerequisites_sha256": result["prerequisites_sha256"],
                            "bound_at": bound_at.astimezone(_SHANGHAI).isoformat(),
                        },
                    )
                elif not (
                    isinstance(ledger[1]["event"], Mapping)
                    and _binding_matches(
                        ledger[1]["event"],
                        registration_sha256=registration_sha256,
                        panel_sha256=str(result["panel_sha256"]),
                        sessions=sessions,
                        prerequisites_sha256=str(result["prerequisites_sha256"]),
                    )
                ):
                    raise FuturePanelRegistrationError("collector ledger is not a resumable registration")

                _verify_registration_authority(output_path, registration_authority, raw)
                opened.verify_identities()
                return result
            finally:
                if registration_authority is not None:
                    registration_authority.close()
    except CollectorContinuityError as exc:
        raise FuturePanelRegistrationError("collector registration cannot be bound") from exc
