"""A-stage continuity primitives for prospective forward collectors.

This module intentionally stops at genesis, identity, strict SQLite opening, and
collector-owned SQLite guards. Ledger chaining and capture orchestration belong
to later stages.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta  # noqa: F401 - retained compatibility module attribute
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets  # noqa: F401 - retained compatibility module attribute
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
from typing import Final, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo


_ExceptionGroup = getattr(builtins, "ExceptionGroup", None)
_BaseExceptionGroup = getattr(builtins, "BaseExceptionGroup", None)


GENESIS_SCHEMA: Final = "stockdata-forward-collector-genesis/1"
CAPABILITY_SCHEMA: Final = "stockdata-forward-collector-capability/2"
PHYSICAL_IDENTITY_SCHEMA: Final = "stockdata-forward-collector-physical-identity/1"
LOGICAL_STATE_SCHEMA: Final = "stockdata-forward-collector-logical-state/1"
LEDGER_EVENT_SCHEMA: Final = "stockdata-forward-collector-ledger-event/1"
CLOSURE_SCHEMA: Final = "stockdata-forward-collector-continuity-closure/1"
SNAPSHOT_DATABASE_REFERENCE_SCHEMA: Final = "stockdata-database-identity/1"
SNAPSHOT_DATABASE_REFERENCE_KIND: Final = "stock-data-database"
CONTINUITY_CLOSURE_REFERENCE_KIND: Final = (
    "stock-data-collector-continuity-closure"
)

GENESIS_CLAIM_FIELDS: Final = frozenset(
    {
        "schema_version",
        "database_uuid",
        "cohort_sha256",
        "database_identity",
        "ledger_identity",
        "collector_schema_sha256",
    }
)
PHYSICAL_IDENTITY_FIELDS: Final = frozenset(
    {
        "schema_version",
        "canonical_path",
        "parent_st_dev",
        "parent_st_ino",
        "file_st_dev",
        "file_st_ino",
    }
)
CAPABILITY_FIELDS: Final = frozenset(
    {
        "schema_version",
        "database_path",
        "ledger_path",
        "source",
        "adjustment_mode",
        "adjustment_version",
        "database_identity",
        "ledger_identity",
        "database_uuid",
        "cohort_sha256",
        "collector_schema_sha256",
        "genesis_sha256",
        "ledger_genesis_event_sha256",
    }
)
GENESIS_EVENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "seq",
        "event_type",
        "previous_event_sha256",
        "database_uuid",
        "cohort_sha256",
        "database_identity",
        "ledger_identity",
        "genesis_claim_sha256",
        "collector_schema_sha256",
        "event_sha256",
    }
)
CLOSURE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "live_database_identity",
        "live_ledger_identity",
        "database_uuid",
        "registration_sha256",
        "ledger_head",
        "logical_state",
        "snapshot_database_reference",
    }
)
_CLOSURE_LEDGER_HEAD_FIELDS: Final = frozenset(
    {"seq", "event_type", "event_sha256"}
)
_CLOSURE_REFERENCE_FIELDS: Final = frozenset(
    {"kind", "identifier", "schema_version"}
)

ZERO_SHA256: Final = "0" * 64
COLLECTOR_BUSY_TIMEOUT_MS: Final = 5_000
_GENESIS_TABLE: Final = "forward_collector_genesis"
_SYMBOLS_TABLE: Final = "forward_collector_symbols"


class CollectorContinuityError(ValueError):
    """Raised when collector continuity preconditions cannot be proven."""


_COLLECTOR_CONTINUITY_FATAL_STAGE: str | None = None


def _mark_collector_continuity_fatal(stage: str) -> None:
    """Retire this process from collector authority after unsafe cleanup."""

    global _COLLECTOR_CONTINUITY_FATAL_STAGE
    if _COLLECTOR_CONTINUITY_FATAL_STAGE is None:
        _COLLECTOR_CONTINUITY_FATAL_STAGE = stage


def require_collector_continuity_health() -> None:
    """Reject all collector authority after an unrecoverable local failure."""

    if _COLLECTOR_CONTINUITY_FATAL_STAGE is not None:
        raise CollectorContinuityError(
            "collector continuity process is fatal at "
            f"{_COLLECTOR_CONTINUITY_FATAL_STAGE}"
        )


@dataclass(frozen=True)
class PhysicalIdentity:
    """Stable path and inode identity for one regular file."""

    canonical_path: str
    parent_st_dev: int
    parent_st_ino: int
    file_st_dev: int
    file_st_ino: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.canonical_path, str)
            or not self.canonical_path.startswith("/")
            or self.canonical_path == "/"
            or os.path.normpath(self.canonical_path) != self.canonical_path
        ):
            raise CollectorContinuityError("physical file identity path is invalid")
        if any(
            type(value) is not int or value < 0
            for value in (
                self.parent_st_dev,
                self.parent_st_ino,
                self.file_st_dev,
                self.file_st_ino,
            )
        ):
            raise CollectorContinuityError("physical file identity fields are invalid")

    @property
    def path(self) -> str:
        return self.canonical_path

    @property
    def parent_device(self) -> int:
        return self.parent_st_dev

    @property
    def parent_inode(self) -> int:
        return self.parent_st_ino

    @property
    def device(self) -> int:
        return self.file_st_dev

    @property
    def inode(self) -> int:
        return self.file_st_ino

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHYSICAL_IDENTITY_SCHEMA,
            "canonical_path": self.canonical_path,
            "parent_st_dev": self.parent_st_dev,
            "parent_st_ino": self.parent_st_ino,
            "file_st_dev": self.file_st_dev,
            "file_st_ino": self.file_st_ino,
        }

    to_dict = as_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PhysicalIdentity":
        require_exact_keys(value, set(PHYSICAL_IDENTITY_FIELDS), "physical file identity")
        if value.get("schema_version") != PHYSICAL_IDENTITY_SCHEMA:
            raise CollectorContinuityError("physical file identity schema is unsupported")
        path = value.get("canonical_path")
        numbers = tuple(
            value.get(field)
            for field in ("parent_st_dev", "parent_st_ino", "file_st_dev", "file_st_ino")
        )
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path == "/"
            or os.path.normpath(path) != path
            or any(type(number) is not int or number < 0 for number in numbers)
        ):
            raise CollectorContinuityError("physical file identity fields are invalid")
        return cls(path, *(int(number) for number in numbers))


@dataclass
class OpenedRegularFile:
    """An open no-follow regular file and its captured physical identity."""

    descriptor: int
    identity: PhysicalIdentity

    def close(self) -> None:
        os.close(self.descriptor)


def canonical_json_bytes(value: object) -> bytes:
    """Return the one permitted ASCII JSON representation."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and "Out of range float values" in str(exc):
            raise CollectorContinuityError("canonical JSON rejects non-finite numbers") from exc
        raise CollectorContinuityError("value is not canonical JSON") from exc


def canonical_json_sha256(value: object) -> str:
    """Hash canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def lexical_absolute_path(path: str | os.PathLike[str]) -> str:
    """Normalize a local path lexically without resolving any filesystem link."""

    value = os.fspath(path)
    if not isinstance(value, str) or not value:
        raise CollectorContinuityError("path must be a non-empty string")
    if not value.startswith("/") or value == "/" or os.path.normpath(value) != value:
        raise CollectorContinuityError("path must name a file below root")
    return value


def _no_follow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(flag, int) or flag == 0:
        raise CollectorContinuityError("O_NOFOLLOW is required for collector files")
    return flag


def _directory_flags() -> int:
    return os.O_RDONLY | _no_follow_flag() | int(getattr(os, "O_DIRECTORY", 0))


def _open_parent(path: str | os.PathLike[str]) -> tuple[int, str, str]:
    canonical = lexical_absolute_path(path)
    components = canonical.split("/")[1:]
    if len(components) < 2 or any(not item or item in {".", ".."} for item in components):
        raise CollectorContinuityError("collector path must have a parent and filename")
    descriptor = os.open("/", _directory_flags())
    try:
        for component in components[:-1]:
            next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(next_descriptor).st_mode):
                    raise CollectorContinuityError("collector parent component is not a directory")
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, canonical, components[-1]
    except OSError as exc:
        os.close(descriptor)
        raise CollectorContinuityError("collector parent cannot be opened safely") from exc
    except Exception:
        os.close(descriptor)
        raise


def _identity_from_open_file(
    descriptor: int, parent_descriptor: int, path: str
) -> PhysicalIdentity:
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode):
        raise CollectorContinuityError("collector file must be regular")
    parent_status = os.fstat(parent_descriptor)
    if not stat.S_ISDIR(parent_status.st_mode):
        raise CollectorContinuityError("collector parent must be a directory")
    return PhysicalIdentity(
        canonical_path=path,
        parent_st_dev=int(parent_status.st_dev),
        parent_st_ino=int(parent_status.st_ino),
        file_st_dev=int(file_status.st_dev),
        file_st_ino=int(file_status.st_ino),
    )


def open_nofollow_regular(
    path: str | os.PathLike[str], *, writable: bool = False
) -> OpenedRegularFile:
    """Open an existing regular file through root-to-leaf no-follow traversal."""

    parent_descriptor, canonical, name = _open_parent(path)
    descriptor: int | None = None
    try:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | _no_follow_flag()
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        identity = _identity_from_open_file(descriptor, parent_descriptor, canonical)
        return OpenedRegularFile(descriptor, identity)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise CollectorContinuityError("collector file cannot be opened safely") from exc
    finally:
        os.close(parent_descriptor)


def create_nofollow_regular(path: str | os.PathLike[str]) -> OpenedRegularFile:
    """Exclusively create a regular collector file below a no-follow parent."""

    parent_descriptor, canonical, name = _open_parent(path)
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | _no_follow_flag()
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        identity = _identity_from_open_file(descriptor, parent_descriptor, canonical)
        return OpenedRegularFile(descriptor, identity)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise CollectorContinuityError("collector file cannot be created safely") from exc
    finally:
        os.close(parent_descriptor)


def verify_file_identity(
    path: str | os.PathLike[str], expected: PhysicalIdentity
) -> PhysicalIdentity:
    """Reopen and compare every bound immutable physical identity field."""

    opened = open_nofollow_regular(path)
    try:
        if opened.identity != expected:
            raise CollectorContinuityError("collector file identity changed")
        return opened.identity
    finally:
        opened.close()


def fsync_regular_file(path: str | os.PathLike[str]) -> None:
    """Durably flush a no-follow regular file or fail closed."""

    opened = open_nofollow_regular(path, writable=True)
    try:
        try:
            os.fsync(opened.descriptor)
        except (AttributeError, OSError) as exc:
            raise CollectorContinuityError("collector file fsync failed") from exc
    finally:
        opened.close()


def fsync_parent_directory(path: str | os.PathLike[str]) -> None:
    """Durably flush a collector file's no-follow containing directory."""

    parent_descriptor, _, _ = _open_parent(path)
    try:
        try:
            os.fsync(parent_descriptor)
        except (AttributeError, OSError) as exc:
            raise CollectorContinuityError("collector directory fsync failed") from exc
    finally:
        os.close(parent_descriptor)


def _reject_sqlite_wal_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            os.lstat(path + suffix)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CollectorContinuityError("collector SQLite sidecar cannot be inspected") from exc
        raise CollectorContinuityError("collector SQLite WAL/SHM sidecars are forbidden")


def _verify_collector_connection_contract(connection: sqlite3.Connection, path: str) -> None:
    """Check connection-local and on-disk invariants before releasing authority."""

    try:
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
            raise CollectorContinuityError("collector SQLite journal mode is not DELETE")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise CollectorContinuityError("collector SQLite foreign keys are disabled")
        if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) != COLLECTOR_BUSY_TIMEOUT_MS:
            raise CollectorContinuityError("collector SQLite busy timeout is invalid")
        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            raise CollectorContinuityError("collector SQLite synchronous mode is not FULL")
        if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise CollectorContinuityError("collector SQLite foreign key check failed")
    except (sqlite3.Error, IndexError, TypeError) as exc:
        raise CollectorContinuityError("collector SQLite connection is invalid") from exc
    _reject_sqlite_wal_sidecars(path)


def _sqlite_uri(path: str, mode: str) -> str:
    return f"file:{quote(path, safe='/')}?mode={mode}"


def open_collector_connection(
    path: str | os.PathLike[str], *, readonly: bool = False
) -> sqlite3.Connection:
    """Open an existing collector with its exact durable SQLite connection mode."""

    require_collector_continuity_health()
    if readonly:
        raise CollectorContinuityError(
            "readonly collector access requires open_registered_collector_read_connection"
        )
    opened = open_nofollow_regular(path, writable=not readonly)
    connection: sqlite3.Connection | None = None
    operation_error: BaseException | None = None
    try:
        _reject_sqlite_wal_sidecars(opened.identity.path)
        try:
            connection = sqlite3.connect(
                _sqlite_uri(opened.identity.path, "ro" if readonly else "rw"),
                uri=True,
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}")
            if not readonly:
                connection.execute("PRAGMA synchronous=FULL")
            _verify_collector_connection_contract(connection, opened.identity.path)
        except (sqlite3.Error, IndexError, TypeError) as exc:
            raise CollectorContinuityError("collector SQLite connection is invalid") from exc
        current = open_nofollow_regular(opened.identity.path)
        try:
            if current.identity != opened.identity:
                raise CollectorContinuityError("collector SQLite identity changed while opening")
        finally:
            current.close()
        _reject_sqlite_wal_sidecars(opened.identity.path)
    except BaseException as exc:
        operation_error = exc
    try:
        opened.close()
    except BaseException as exc:
        operation_error = (
            exc
            if operation_error is None
            else _combine_collector_context_errors(operation_error, exc)
        )
    if operation_error is not None:
        if connection is not None:
            try:
                connection.close()
            except BaseException as exc:
                operation_error = _combine_collector_context_errors(operation_error, exc)
        raise operation_error
    if connection is None:
        raise CollectorContinuityError("collector SQLite connection was not created")
    return connection


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CollectorContinuityError("canonical JSON has duplicate keys")
        result[key] = value
    return result


def _decode_exact_mapping(raw: bytes, fields: frozenset[str], label: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorContinuityError(f"{label} is not ASCII JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise CollectorContinuityError(f"{label} fields are not exact")
    if raw != canonical_json_bytes(value):
        raise CollectorContinuityError(f"{label} is not canonical JSON")
    return value


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CollectorContinuityError(f"{label} must be a lower-case SHA-256")
    return value


def _require_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CollectorContinuityError(f"{label} must be non-empty text")
    return value


def _require_positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CollectorContinuityError(f"{label} must be a non-negative integer")
    return value


def decode_physical_identity(raw: bytes) -> dict[str, object]:
    """Decode one exact canonical physical identity object."""

    value = _decode_exact_mapping(raw, PHYSICAL_IDENTITY_FIELDS, "physical identity")
    if value["schema_version"] != PHYSICAL_IDENTITY_SCHEMA:
        raise CollectorContinuityError("physical identity schema is unsupported")
    path = _require_nonempty_text(value["canonical_path"], "physical identity path")
    if not path.startswith("/") or os.path.normpath(path) != path:
        raise CollectorContinuityError("physical identity path is not lexical absolute")
    for field_name in ("parent_st_dev", "parent_st_ino", "file_st_dev", "file_st_ino"):
        _require_positive_integer(value[field_name], f"physical identity {field_name}")
    return value


def decode_genesis_claim(raw: bytes) -> dict[str, object]:
    """Decode one exact canonical immutable genesis claim."""

    value = _decode_exact_mapping(raw, GENESIS_CLAIM_FIELDS, "genesis claim")
    if value["schema_version"] != GENESIS_SCHEMA:
        raise CollectorContinuityError("genesis claim schema is unsupported")
    _require_sha256(value["database_uuid"], "database_uuid")
    _require_sha256(value["cohort_sha256"], "cohort_sha256")
    _require_sha256(value["collector_schema_sha256"], "collector_schema_sha256")
    for field_name in ("database_identity", "ledger_identity"):
        if not isinstance(value[field_name], dict):
            raise CollectorContinuityError(f"{field_name} is invalid")
        decode_physical_identity(canonical_json_bytes(value[field_name]))
    return value


def decode_capability(raw: bytes) -> dict[str, object]:
    """Decode one exact canonical continuity capability."""

    value = _decode_exact_mapping(raw, CAPABILITY_FIELDS, "collector capability")
    if value["schema_version"] != CAPABILITY_SCHEMA:
        raise CollectorContinuityError("collector capability schema is unsupported")
    for field_name in ("database_path", "ledger_path", "source", "adjustment_mode", "adjustment_version"):
        _require_nonempty_text(value[field_name], field_name)
    if (
        not str(value["database_path"]).startswith("/")
        or not str(value["ledger_path"]).startswith("/")
    ):
        raise CollectorContinuityError("collector capability paths are not absolute")
    for field_name in (
        "database_uuid",
        "cohort_sha256",
        "collector_schema_sha256",
        "genesis_sha256",
        "ledger_genesis_event_sha256",
    ):
        _require_sha256(value[field_name], field_name)
    for field_name in ("database_identity", "ledger_identity"):
        if not isinstance(value[field_name], dict):
            raise CollectorContinuityError(f"{field_name} is invalid")
        decoded = decode_physical_identity(canonical_json_bytes(value[field_name]))
        if decoded["canonical_path"] != value[field_name]["canonical_path"]:
            raise CollectorContinuityError(f"{field_name} is invalid")
    return value


def validate_collector_continuity_closure(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Validate the exact negative-provenance closure frozen by tasks 4.1/4.2."""

    if not isinstance(value, Mapping):
        raise CollectorContinuityError("collector continuity closure is invalid")
    require_exact_keys(value, CLOSURE_FIELDS, "collector continuity closure")
    if value.get("schema_version") != CLOSURE_SCHEMA:
        raise CollectorContinuityError("collector continuity closure schema is unsupported")
    for field_name in ("live_database_identity", "live_ledger_identity"):
        identity = value.get(field_name)
        if not isinstance(identity, Mapping):
            raise CollectorContinuityError(
                f"collector continuity closure {field_name} is invalid"
            )
        PhysicalFileIdentity.from_dict(identity)
    _require_event_sha256(value.get("database_uuid"), "closure database_uuid")
    _require_event_sha256(
        value.get("registration_sha256"), "closure registration_sha256"
    )
    head = value.get("ledger_head")
    if not isinstance(head, Mapping):
        raise CollectorContinuityError("collector continuity closure ledger head is invalid")
    require_exact_keys(head, _CLOSURE_LEDGER_HEAD_FIELDS, "closure ledger head")
    if (
        type(head.get("seq")) is not int
        or head["seq"] < 0
        or head.get("event_type") != "ATTEMPT_COMPLETED"
    ):
        raise CollectorContinuityError("collector continuity closure ledger head is invalid")
    _require_event_sha256(head.get("event_sha256"), "closure ledger head hash")
    logical_state = value.get("logical_state")
    if not isinstance(logical_state, Mapping):
        raise CollectorContinuityError("collector continuity closure logical state is invalid")
    validate_collector_step_state(
        logical_state,
        allowed_tables=_STEP_IDENTITY["post_close_prices"][2],
    )
    reference = value.get("snapshot_database_reference")
    if not isinstance(reference, Mapping):
        raise CollectorContinuityError(
            "collector continuity closure database reference is invalid"
        )
    require_exact_keys(
        reference, _CLOSURE_REFERENCE_FIELDS, "closure database reference"
    )
    if (
        reference.get("kind") != SNAPSHOT_DATABASE_REFERENCE_KIND
        or reference.get("schema_version") != SNAPSHOT_DATABASE_REFERENCE_SCHEMA
    ):
        raise CollectorContinuityError(
            "collector continuity closure database reference is invalid"
        )
    _require_event_sha256(
        reference.get("identifier"), "closure database reference identifier"
    )
    return decode_canonical_json_object(canonical_json_bytes(dict(value)))


def decode_collector_continuity_closure(raw: bytes) -> dict[str, object]:
    """Decode one exact canonical continuity closure."""

    return validate_collector_continuity_closure(decode_canonical_json_object(raw))


def build_genesis_claim(
    *,
    database_uuid: str,
    cohort_sha256: str,
    database_identity: PhysicalIdentity,
    ledger_identity: PhysicalIdentity,
    collector_schema_sha256: str,
) -> dict[str, object]:
    """Build the immutable claim shared by the database and genesis ledger event."""

    claim = {
        "schema_version": GENESIS_SCHEMA,
        "database_uuid": database_uuid,
        "cohort_sha256": cohort_sha256,
        "database_identity": database_identity.as_dict(),
        "ledger_identity": ledger_identity.as_dict(),
        "collector_schema_sha256": collector_schema_sha256,
    }
    return decode_genesis_claim(canonical_json_bytes(claim))


def build_genesis_event(claim: Mapping[str, object]) -> dict[str, object]:
    """Reject the retired flat genesis writer.

    The prepared collector's nested ``seq=0`` event is the sole writable
    genesis authority.  The flat A-stage decoder remains read-only migration
    compatibility, but it must not mint a competing event format.
    """

    del claim
    raise CollectorContinuityError("flat genesis writer is retired")


def decode_genesis_event(raw: bytes) -> dict[str, object]:
    """Decode and self-hash-check the one A-stage GENESIS ledger event."""

    value = _decode_exact_mapping(raw, GENESIS_EVENT_FIELDS, "genesis ledger event")
    if (
        value["schema_version"] != LEDGER_EVENT_SCHEMA
        or value["seq"] != 1
        or value["event_type"] != "GENESIS"
        or value["previous_event_sha256"] != ZERO_SHA256
    ):
        raise CollectorContinuityError("genesis ledger event is invalid")
    for field_name in (
        "database_uuid",
        "cohort_sha256",
        "genesis_claim_sha256",
        "collector_schema_sha256",
        "event_sha256",
    ):
        _require_sha256(value[field_name], field_name)
    for field_name in ("database_identity", "ledger_identity"):
        if not isinstance(value[field_name], dict):
            raise CollectorContinuityError(f"genesis ledger {field_name} is invalid")
        decode_physical_identity(canonical_json_bytes(value[field_name]))
    without_hash = {key: item for key, item in value.items() if key != "event_sha256"}
    if value["event_sha256"] != canonical_json_sha256(without_hash):
        raise CollectorContinuityError("genesis ledger event hash is invalid")
    return value


def write_initial_genesis_event(
    ledger_path: str | os.PathLike[str], claim: Mapping[str, object]
) -> str:
    """Reject the retired flat genesis writer."""

    require_collector_continuity_health()
    del ledger_path, claim
    raise CollectorContinuityError("flat genesis writer is retired")


def read_initial_genesis_event(ledger_path: str | os.PathLike[str]) -> dict[str, object]:
    """Read exactly one canonical GENESIS event from an A-stage ledger."""

    require_collector_continuity_health()
    opened = open_nofollow_regular(ledger_path)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 1 or size > 65_536:
            raise CollectorContinuityError("collector genesis ledger size is invalid")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(opened.descriptor, remaining)
            if not chunk:
                raise CollectorContinuityError("collector genesis ledger is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        opened.close()
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise CollectorContinuityError("collector ledger must contain exactly one JSONL event")
    return decode_genesis_event(raw[:-1])


_GUARD_TABLE_SQL: Final = """
CREATE TABLE IF NOT EXISTS forward_collector_genesis (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    schema_version TEXT NOT NULL,
    database_uuid TEXT NOT NULL,
    cohort_sha256 TEXT NOT NULL,
    genesis_claim_sha256 TEXT NOT NULL,
    ledger_genesis_event_sha256 TEXT NOT NULL,
    database_identity_json TEXT NOT NULL,
    ledger_identity_json TEXT NOT NULL,
    collector_schema_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forward_collector_symbols (
    symbol TEXT PRIMARY KEY
);
"""

_GUARD_TRIGGER_SQL: Final = {
    "forward_collector_genesis_no_update": """
        CREATE TRIGGER forward_collector_genesis_no_update
        BEFORE UPDATE ON forward_collector_genesis BEGIN
            SELECT RAISE(ABORT, 'collector genesis is immutable');
        END
    """,
    "forward_collector_genesis_no_delete": """
        CREATE TRIGGER forward_collector_genesis_no_delete
        BEFORE DELETE ON forward_collector_genesis BEGIN
            SELECT RAISE(ABORT, 'collector genesis is immutable');
        END
    """,
    "forward_collector_symbols_no_insert": """
        CREATE TRIGGER forward_collector_symbols_no_insert
        BEFORE INSERT ON forward_collector_symbols
        WHEN EXISTS (SELECT 1 FROM forward_collector_symbols) BEGIN
            SELECT RAISE(ABORT, 'collector symbols are immutable');
        END
    """,
    "forward_collector_symbols_no_update": """
        CREATE TRIGGER forward_collector_symbols_no_update
        BEFORE UPDATE ON forward_collector_symbols BEGIN
            SELECT RAISE(ABORT, 'collector symbols are immutable');
        END
    """,
    "forward_collector_symbols_no_delete": """
        CREATE TRIGGER forward_collector_symbols_no_delete
        BEFORE DELETE ON forward_collector_symbols BEGIN
            SELECT RAISE(ABORT, 'collector symbols are immutable');
        END
    """,
    "collector_daily_insert_guard": """
        CREATE TRIGGER collector_daily_insert_guard
        BEFORE INSERT ON daily
        WHEN NEW.code NOT IN (SELECT symbol FROM forward_collector_symbols)
          OR NEW.source != 'tencent'
          OR NEW.adjustment_mode != 'raw'
          OR NEW.adjustment_version != 'tencent-qt-daily-v1'
          OR NEW.is_final != 1
          OR NEW.receipt_id IS NULL BEGIN
            SELECT RAISE(ABORT, 'collector daily evidence identity is invalid');
        END
    """,
    "collector_daily_final_no_change": """
        CREATE TRIGGER collector_daily_final_no_change
        BEFORE UPDATE ON daily
        WHEN OLD.is_final = 1 AND (
            NEW.code IS NOT OLD.code OR NEW.date IS NOT OLD.date
            OR NEW.open IS NOT OLD.open OR NEW.high IS NOT OLD.high
            OR NEW.low IS NOT OLD.low OR NEW.close IS NOT OLD.close
            OR NEW.volume IS NOT OLD.volume OR NEW.source IS NOT OLD.source
            OR NEW.adjustment_mode IS NOT OLD.adjustment_mode
            OR NEW.adjustment_version IS NOT OLD.adjustment_version
            OR NEW.retrieved_at IS NOT OLD.retrieved_at
            OR NEW.is_final IS NOT OLD.is_final OR NEW.receipt_id IS NOT OLD.receipt_id
        ) BEGIN
            SELECT RAISE(ABORT, 'finalized collector daily evidence is immutable');
        END
    """,
    "collector_daily_final_no_delete": """
        CREATE TRIGGER collector_daily_final_no_delete
        BEFORE DELETE ON daily WHEN OLD.is_final = 1 BEGIN
            SELECT RAISE(ABORT, 'finalized collector daily evidence is immutable');
        END
    """,
    "collector_sync_coverage_insert_guard": """
        CREATE TRIGGER collector_sync_coverage_insert_guard
        BEFORE INSERT ON sync_coverage
        WHEN NEW.code NOT IN (SELECT symbol FROM forward_collector_symbols)
          OR NEW.source != 'tencent'
          OR NEW.adjustment_mode != 'raw'
          OR NEW.adjustment_version != 'tencent-qt-daily-v1'
          OR NEW.start_date > NEW.end_date BEGIN
            SELECT RAISE(ABORT, 'collector sync coverage identity is invalid');
        END
    """,
    "collector_sync_coverage_monotonic": """
        CREATE TRIGGER collector_sync_coverage_monotonic
        BEFORE UPDATE ON sync_coverage
        WHEN NEW.code IS NOT OLD.code OR NEW.source IS NOT OLD.source
          OR NEW.adjustment_mode IS NOT OLD.adjustment_mode
          OR NEW.adjustment_version IS NOT OLD.adjustment_version
          OR NEW.start_date > OLD.start_date OR NEW.end_date < OLD.end_date
          OR NEW.start_date > NEW.end_date BEGIN
            SELECT RAISE(ABORT, 'collector sync coverage must widen monotonically');
        END
    """,
    "collector_sync_coverage_no_delete": """
        CREATE TRIGGER collector_sync_coverage_no_delete
        BEFORE DELETE ON sync_coverage BEGIN
            SELECT RAISE(ABORT, 'collector sync coverage is append-only');
        END
    """,
}


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";").lower()


def install_collector_guards(connection: sqlite3.Connection, symbols: Sequence[str]) -> None:
    """Install immutable collector-only tables and guards on a new database."""

    normalized = tuple(sorted(set(symbols)))
    if not normalized or any(not isinstance(symbol, str) or not symbol for symbol in normalized):
        raise CollectorContinuityError("collector symbols are invalid")
    connection.executescript(_GUARD_TABLE_SQL)
    existing = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT symbol FROM forward_collector_symbols ORDER BY symbol"
        )
    )
    if existing and existing != normalized:
        raise CollectorContinuityError("collector symbols drift")
    if not existing:
        connection.executemany(
            "INSERT INTO forward_collector_symbols(symbol) VALUES (?)",
            ((symbol,) for symbol in normalized),
        )
    for sql in _GUARD_TRIGGER_SQL.values():
        connection.execute(sql.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1))


def verify_collector_guards(connection: sqlite3.Connection, symbols: Sequence[str]) -> None:
    """Verify the collector guard schema and the exact immutable symbol cohort."""

    expected = tuple(sorted(set(symbols)))
    actual = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT symbol FROM forward_collector_symbols ORDER BY symbol"
        )
    )
    if actual != expected:
        raise CollectorContinuityError("collector symbols are invalid")
    triggers = {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT name,sql FROM main.sqlite_master WHERE type='trigger'")
    }
    invalid = [
        name
        for name, sql in _GUARD_TRIGGER_SQL.items()
        if name not in triggers or _normalized_sql(triggers[name]) != _normalized_sql(sql)
    ]
    if invalid:
        raise CollectorContinuityError("collector guard triggers are invalid")


def collector_schema_sha256(connection: sqlite3.Connection) -> str:
    """Hash the exact collector-owned SQLite structural contract."""

    rows = [
        [str(row[0]), str(row[1]), str(row[2] or "")]
        for row in connection.execute(
            "SELECT type,name,sql FROM main.sqlite_master "
            "WHERE type IN ('table','trigger') ORDER BY type,name"
        )
    ]
    return canonical_json_sha256({"sqlite_master": rows})


def persist_genesis_record(
    connection: sqlite3.Connection,
    *,
    claim: Mapping[str, object],
    ledger_genesis_event_sha256: str,
) -> None:
    """Persist the DB half of the genesis cross-binding exactly once."""

    decoded = decode_genesis_claim(canonical_json_bytes(dict(claim)))
    event_hash = _require_sha256(ledger_genesis_event_sha256, "ledger genesis event hash")
    rows = connection.execute("SELECT 1 FROM main.forward_collector_genesis").fetchall()
    if rows:
        raise CollectorContinuityError("collector genesis already exists")
    connection.execute(
        "INSERT INTO main.forward_collector_genesis "
        "(singleton,schema_version,database_uuid,cohort_sha256,genesis_claim_sha256,"
        "ledger_genesis_event_sha256,database_identity_json,ledger_identity_json,"
        "collector_schema_sha256) VALUES (1,?,?,?,?,?,?,?,?)",
        (
            decoded["schema_version"],
            decoded["database_uuid"],
            decoded["cohort_sha256"],
            canonical_json_sha256(decoded),
            event_hash,
            canonical_json_bytes(decoded["database_identity"]).decode("ascii"),
            canonical_json_bytes(decoded["ledger_identity"]).decode("ascii"),
            decoded["collector_schema_sha256"],
        ),
    )


def verify_genesis_binding(
    connection: sqlite3.Connection,
    *,
    database_identity: PhysicalIdentity,
    ledger_path: str | os.PathLike[str],
    ledger_identity: PhysicalIdentity,
    expected_cohort_sha256: str,
) -> dict[str, object]:
    """Verify both directions of the database/ledger immutable genesis binding."""

    rows = connection.execute(
        "SELECT schema_version,database_uuid,cohort_sha256,genesis_claim_sha256,"
        "ledger_genesis_event_sha256,database_identity_json,ledger_identity_json,"
        "collector_schema_sha256 FROM main.forward_collector_genesis"
    ).fetchall()
    if len(rows) != 1:
        raise CollectorContinuityError("collector genesis must have exactly one row")
    row = rows[0]
    try:
        database_value = decode_physical_identity(str(row[5]).encode("ascii"))
        ledger_value = decode_physical_identity(str(row[6]).encode("ascii"))
    except UnicodeEncodeError as exc:
        raise CollectorContinuityError("collector genesis identity JSON is invalid") from exc
    claim = build_genesis_claim(
        database_uuid=_require_sha256(row[1], "database_uuid"),
        cohort_sha256=_require_sha256(row[2], "cohort_sha256"),
        database_identity=database_identity,
        ledger_identity=ledger_identity,
        collector_schema_sha256=_require_sha256(row[7], "collector_schema_sha256"),
    )
    if (
        row[0] != GENESIS_SCHEMA
        or row[2] != expected_cohort_sha256
        or database_value != database_identity.as_dict()
        or ledger_value != ledger_identity.as_dict()
        or row[3] != canonical_json_sha256(claim)
    ):
        raise CollectorContinuityError("collector database genesis claim is invalid")
    event = read_initial_genesis_event(ledger_path)
    if (
        event["event_sha256"] != row[4]
        or event["database_uuid"] != row[1]
        or event["cohort_sha256"] != row[2]
        or event["database_identity"] != database_identity.as_dict()
        or event["ledger_identity"] != ledger_identity.as_dict()
        or event["genesis_claim_sha256"] != row[3]
        or event["collector_schema_sha256"] != row[7]
    ):
        raise CollectorContinuityError("collector ledger genesis is not bound to the database")
    return {
        "database_uuid": str(row[1]),
        "cohort_sha256": str(row[2]),
        "genesis_claim_sha256": str(row[3]),
        "ledger_genesis_event_sha256": str(row[4]),
        "database_schema_sha256": str(row[7]),
    }


def is_collector_database(path: str | os.PathLike[str]) -> bool:
    """Identify a marked collector before Cache chooses its no-migration mode."""

    try:
        connection = sqlite3.connect(_sqlite_uri(lexical_absolute_path(path), "ro"), uri=True)
    except (CollectorContinuityError, sqlite3.Error):
        return False
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='forward_collector_genesis'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        connection.close()


# Prepared collector compatibility ------------------------------------------
#
# A-stage owns the no-follow, SQLite, canonical JSON, and physical-identity
# primitives above.  This compatibility layer exposes those primitives through
# the public preparation contract.  Later attempt-ledger, lease, logical-state,
# and step-state APIs remain deliberately unavailable below.

COLLECTOR_PREPARATION_SCHEMA: Final = "stockdata-forward-collector-preparation/4"
COLLECTOR_GENESIS_SCHEMA: Final = GENESIS_SCHEMA
COLLECTOR_LOGICAL_STATE_SCHEMA: Final = LOGICAL_STATE_SCHEMA
COLLECTOR_LEDGER_EVENT_SCHEMA: Final = LEDGER_EVENT_SCHEMA
PHYSICAL_FILE_IDENTITY_SCHEMA: Final = PHYSICAL_IDENTITY_SCHEMA
COLLECTOR_STEP_STATE_SCHEMA: Final = "stockdata-forward-collector-step-state/1"
COLLECTOR_STEP_RAW_BEFORE_SCHEMA: Final = "stockdata-forward-collector-step-raw-before/1"
COLLECTOR_SQLITE_BUSY_TIMEOUT_MS: Final = COLLECTOR_BUSY_TIMEOUT_MS
COLLECTOR_LEDGER_MAX_BYTES: Final = 128 * 1024 * 1024
COLLECTOR_LEDGER_MAX_LINE_BYTES: Final = 64 * 1024
COLLECTOR_LEDGER_MAX_LINES: Final = 100_000
LEDGER_EVENT_TYPES: Final = (
    "GENESIS",
    "REGISTRATION_BOUND",
    "SQLITE_RECOVERY_STARTED",
    "SQLITE_RECOVERY_COMPLETED",
    "SQLITE_RECOVERY_FAILED",
    "ATTEMPT_STARTED",
    "ATTEMPT_COMPLETED",
    "ATTEMPT_FAILED",
)
COLLECTOR_STATE_TABLES: Final = (
    "collection_receipts",
    "daily",
    "forward_capture_cohort",
    "forward_collector_genesis",
    "forward_context_observations",
    "forward_corporate_action_coverage",
    "forward_corporate_actions",
    "forward_status_observations",
    "forward_universe_observations",
    "sync_coverage",
)

_COLLECTOR_LOGICAL_STATE_DOMAIN: Final = (
    b"stockdata-forward-collector-logical-state/1\x00collector_state_sha256\x00"
)
_COLLECTOR_LOGICAL_STATE_TABLES: Final = (
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

_COLLECTOR_TABLE_NAMES: Final = (
    "collection_receipts",
    "daily",
    "forward_capture_cohort",
    "forward_collector_genesis",
    "forward_context_observations",
    "forward_corporate_action_coverage",
    "forward_corporate_actions",
    "forward_status_observations",
    "forward_universe_observations",
    "sync_coverage",
)
_COLLECTOR_TRIGGER_NAMES: Final = (
    "collection_receipts_no_delete",
    "collection_receipts_no_update",
    "collector_daily_final_no_delete",
    "collector_daily_final_no_update",
    "daily_non_final_insert",
    "collector_sync_coverage_no_delete",
    "collector_sync_coverage_no_update",
    "sync_coverage_exact_noop",
    "forward_capture_cohort_no_delete",
    "forward_capture_cohort_no_update",
    "forward_collector_genesis_no_delete",
    "forward_collector_genesis_no_update",
    "forward_context_observations_no_delete",
    "forward_context_observations_no_update",
    "forward_corporate_action_coverage_no_delete",
    "forward_corporate_action_coverage_no_update",
    "forward_corporate_actions_no_delete",
    "forward_corporate_actions_no_update",
    "forward_status_observations_no_delete",
    "forward_status_observations_no_update",
    "forward_universe_observations_no_delete",
    "forward_universe_observations_no_update",
)

# These maps are filled below from a private in-memory expansion of source-tree
# schema constants.  Candidate collector databases never provide authority SQL.
COLLECTOR_EVIDENCE_TRIGGER_SQL: Final[dict[str, str]] = {}
_COLLECTOR_OWNED_TABLE_SQL: Final[dict[str, str]] = {}
_GENESIS_TRIGGERS: Final[dict[str, str]] = {}


@dataclass(frozen=True)
class _RetiredPhysicalFileIdentity:
    """Retired duplicate identity implementation retained only for source history."""

    canonical_path: str
    parent_st_dev: int
    parent_st_ino: int
    file_st_dev: int
    file_st_ino: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.canonical_path, str)
            or not self.canonical_path.startswith("/")
            or self.canonical_path == "/"
            or os.path.normpath(self.canonical_path) != self.canonical_path
        ):
            raise CollectorContinuityError("physical file identity path is invalid")
        numbers = (
            self.parent_st_dev,
            self.parent_st_ino,
            self.file_st_dev,
            self.file_st_ino,
        )
        if any(type(number) is not int or number < 0 for number in numbers):
            raise CollectorContinuityError("physical file identity fields are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHYSICAL_FILE_IDENTITY_SCHEMA,
            "canonical_path": self.canonical_path,
            "parent_st_dev": self.parent_st_dev,
            "parent_st_ino": self.parent_st_ino,
            "file_st_dev": self.file_st_dev,
            "file_st_ino": self.file_st_ino,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "_RetiredPhysicalFileIdentity":
        required = {
            "schema_version",
            "canonical_path",
            "parent_st_dev",
            "parent_st_ino",
            "file_st_dev",
            "file_st_ino",
        }
        require_exact_keys(value, required, "physical file identity")
        if value["schema_version"] != PHYSICAL_FILE_IDENTITY_SCHEMA:
            raise CollectorContinuityError("physical file identity schema is unsupported")
        numbers = (
            value["parent_st_dev"],
            value["parent_st_ino"],
            value["file_st_dev"],
            value["file_st_ino"],
        )
        if any(type(number) is not int or number < 0 for number in numbers):
            raise CollectorContinuityError("physical file identity fields are invalid")
        return cls(
            value["canonical_path"],
            int(value["parent_st_dev"]),
            int(value["parent_st_ino"]),
            int(value["file_st_dev"]),
            int(value["file_st_ino"]),
        )


# One schema version has one physical-identity wire format.  Keep the public
# successor name as an alias so existing callers cannot choose a second format.
PhysicalFileIdentity = PhysicalIdentity


@dataclass(frozen=True)
class CollectorStepSpec:
    registration_file: str
    registration_sha256: str
    session: str
    phase: str
    step_id: str
    step_ordinal: int
    allowed_tables: frozenset[str]
    selector_source: str
    database_path: str
    symbols: tuple[str, ...]
    command: tuple[str, ...]
    command_sha256: str
    schedule_sha256: str

    @property
    def source(self) -> str:
        """The immutable source selected for this collector step."""

        return self.selector_source


@dataclass(frozen=True)
class _FrozenCollectorStepSchedule:
    """A reconstructed schedule rooted in one persistent `/4` registration."""

    registration_file: str
    registration_sha256: str
    sessions: tuple[str, str, str]
    cohort_start: str
    source: str
    adjustment_mode: str
    adjustment_version: str
    database_path: str
    ledger_path: str
    ledger_identity: PhysicalFileIdentity
    specs: tuple[CollectorStepSpec, ...]


_FROZEN_COLLECTOR_STEP_SCHEDULES: Final[dict[str, _FrozenCollectorStepSchedule]] = {}


@dataclass
class CollectorChildLeaseHandoff:
    """One inheritable duplicate of a live collector phase lease."""

    fd: int
    pass_fds: tuple[int, ...]
    _lease: "CollectorPhaseLease"
    _closed: bool = False

    def __enter__(self) -> "CollectorChildLeaseHandoff":
        require_collector_continuity_health()
        self._lease._verify_owner()
        if self._closed:
            raise CollectorContinuityError("collector lease handoff is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        """Close this duplicate without explicitly unlocking the shared flock."""

        self._lease._verify_owner()
        if self._closed:
            return
        try:
            os.close(self.fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise CollectorContinuityError("collector lease handoff cannot be closed") from exc
        self._closed = True
        self._lease._handoff_fds.discard(self.fd)
        self._lease._release_if_inactive()


@dataclass
class CollectorPhaseLease:
    """A process-owned, non-blocking exclusive lease of one ledger file."""

    ledger: _OpenedRegularFile
    owner_pid: int
    owner_thread_id: int = field(default_factory=threading.get_ident)
    _closed: bool = False
    _entered: bool = False
    _handoff_fds: set[int] = field(default_factory=set)

    def _verify_owner(self) -> None:
        if self.owner_pid != os.getpid():
            raise CollectorContinuityError("collector phase lease belongs to another process")
        if self.owner_thread_id != threading.get_ident():
            raise CollectorContinuityError("collector phase lease belongs to another thread")

    def _release_if_inactive(self) -> None:
        if self._closed and not self._handoff_fds:
            current = _ACTIVE_COLLECTOR_PHASE_LEASES.get(self.ledger.identity)
            if current is self:
                del _ACTIVE_COLLECTOR_PHASE_LEASES[self.ledger.identity]

    def __enter__(self) -> "CollectorPhaseLease":
        require_collector_continuity_health()
        self._verify_owner()
        if self._closed:
            raise CollectorContinuityError("collector phase lease is closed")
        if self._entered:
            raise CollectorContinuityError("collector phase lease is not reentrant")
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def verify(self) -> PhysicalFileIdentity:
        require_collector_continuity_health()
        self._verify_owner()
        if self._closed:
            raise CollectorContinuityError("collector phase lease is closed")
        identity = self.ledger.verify_identity()
        return verify_locked_collector_lease(
            self.ledger.file_fd,
            expected_ledger_identity=identity,
        )

    def child_handoff(self) -> CollectorChildLeaseHandoff:
        require_collector_continuity_health()
        self.verify()
        try:
            duplicate = os.dup(self.ledger.file_fd)
        except OSError as exc:
            raise CollectorContinuityError("collector lease handoff cannot be created") from exc
        try:
            os.set_inheritable(duplicate, True)
        except OSError as primary_exc:
            try:
                os.close(duplicate)
            except OSError as cleanup_exc:
                raise CollectorContinuityError(
                    "collector lease handoff cannot be created: "
                    f"{primary_exc}; duplicate cleanup failed: {cleanup_exc}"
                ) from cleanup_exc
            raise CollectorContinuityError("collector lease handoff cannot be created") from primary_exc
        self._handoff_fds.add(duplicate)
        return CollectorChildLeaseHandoff(duplicate, (duplicate,), self)

    def close(self) -> None:
        """Close the base descriptor; handoffs keep its flock alive when present."""

        self._verify_owner()
        if self._closed:
            return
        try:
            # Close the non-locking parent first.  If it fails, the locking
            # descriptor remains open and this method can be retried honestly.
            if self.ledger.parent_fd >= 0:
                os.close(self.ledger.parent_fd)
                self.ledger.parent_fd = -1
            os.close(self.ledger.file_fd)
            self.ledger.file_fd = -1
        except OSError as exc:
            raise CollectorContinuityError("collector phase lease cannot be closed") from exc
        self._closed = True
        self._release_if_inactive()


_ACTIVE_COLLECTOR_PHASE_LEASES: Final[dict[PhysicalFileIdentity, CollectorPhaseLease]] = {}


def _later_stage_unavailable(name: str) -> None:
    raise CollectorContinuityError(f"{name} is unavailable until later continuity stages are restored")


def require_exact_keys(
    value: Mapping[str, object], expected: set[str] | frozenset[str], label: str
) -> None:
    actual = set(value)
    expected_set = set(expected)
    unknown = sorted(actual - expected_set)
    missing = sorted(expected_set - actual)
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        if missing:
            details.append(f"missing={','.join(missing)}")
        raise CollectorContinuityError(f"{label} fields are not exact: {'; '.join(details)}")


def decode_canonical_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorContinuityError("canonical JSON object is invalid") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise CollectorContinuityError("canonical JSON object is not canonical")
    return value


def canonical_collector_path(path: str | os.PathLike[str]) -> str:
    return lexical_absolute_path(path)


def default_collector_ledger_path(database_path: str | os.PathLike[str]) -> str:
    return f"{canonical_collector_path(database_path)}.collector-ledger.jsonl"


def database_has_collector_genesis(path: str | os.PathLike[str]) -> bool:
    return is_collector_database(path)


def probe_database_collector_genesis_strict(
    path: str | os.PathLike[str],
) -> bool:
    """Return a marker verdict only after one identity-bound SQLite query."""

    require_collector_continuity_health()
    canonical = lexical_absolute_path(path)
    _reject_registered_collector_read_sidecars(canonical)
    opened = open_nofollow_regular(canonical)
    connection: sqlite3.Connection | None = None
    verdict: bool | None = None
    body_error: BaseException | None = None
    try:
        os.set_inheritable(opened.descriptor, False)
        verify_file_identity(canonical, opened.identity)
        locator = f"/dev/fd/{opened.descriptor}"
        connection = sqlite3.connect(
            f"file:{locator}?mode=ro&cache=private",
            uri=True,
            check_same_thread=True,
            isolation_level=None,
        )
        connection.row_factory = None
        connection.text_factory = str
        connection.execute(f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=1")
        database_list = connection.execute("PRAGMA database_list").fetchall()
        query_only = connection.execute("PRAGMA query_only").fetchone()
        marker = connection.execute(
            "SELECT COUNT(*) FROM main.sqlite_master WHERE type='table' "
            "AND name='forward_collector_genesis'"
        ).fetchone()
        if (
            len(database_list) != 1
            or len(database_list[0]) != 3
            or database_list[0][1] != "main"
            or database_list[0][2] != locator
            or query_only != (1,)
            or marker not in {(0,), (1,)}
        ):
            raise CollectorContinuityError(
                "collector genesis marker query is indeterminate"
            )
        _reject_registered_collector_read_sidecars(canonical)
        verify_file_identity(canonical, opened.identity)
        verdict = marker == (1,)
    except BaseException as exc:
        body_error = exc
    cleanup_error: BaseException | None = None
    if connection is not None:
        try:
            connection.close()
        except BaseException as exc:
            cleanup_error = exc
    try:
        opened.close()
    except BaseException as exc:
        cleanup_error = (
            exc
            if cleanup_error is None
            else _combine_collector_context_errors(cleanup_error, exc)
        )
    if body_error is not None:
        if cleanup_error is not None:
            raise _combine_collector_context_errors(body_error, cleanup_error)
        if isinstance(body_error, CollectorContinuityError):
            raise body_error
        raise CollectorContinuityError(
            "collector genesis marker cannot be determined"
        ) from body_error
    if cleanup_error is not None:
        raise CollectorContinuityError(
            "collector genesis marker probe cleanup failed"
        ) from cleanup_error
    if verdict is None:
        raise CollectorContinuityError("collector genesis marker is indeterminate")
    _reject_registered_collector_read_sidecars(canonical)
    verify_file_identity(canonical, opened.identity)
    return verdict


def _split_parent_and_leaf(path: str | os.PathLike[str]) -> tuple[str, str]:
    canonical = canonical_collector_path(path)
    parent, leaf = os.path.split(canonical)
    if not leaf:
        raise CollectorContinuityError("collector path must name a file")
    return parent or "/", leaf


def _legacy_identity(identity: PhysicalIdentity) -> PhysicalFileIdentity:
    return PhysicalFileIdentity(
        identity.path,
        identity.parent_device,
        identity.parent_inode,
        identity.device,
        identity.inode,
    )


@dataclass(slots=True)
class _OpenedRegularFile:
    file_fd: int
    parent_fd: int
    leaf: str
    identity: PhysicalFileIdentity

    def __enter__(self) -> "_OpenedRegularFile":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        stages: list[str] = []
        errors: list[BaseException] = []
        for attribute in ("file_fd", "parent_fd"):
            descriptor = getattr(self, attribute)
            if descriptor < 0:
                continue
            setattr(self, attribute, -1)
            try:
                os.close(descriptor)
            except BaseException as exc:
                stages.append(attribute)
                errors.append(exc)
        if errors:
            raise _collector_cleanup_error(
                "collector regular file close failed", stages, errors
            )

    def verify_identity(self) -> PhysicalFileIdentity:
        if self.file_fd < 0 or self.parent_fd < 0:
            raise CollectorContinuityError("collector file is closed")
        try:
            opened = open_existing_regular_file(self.identity.canonical_path)
        except CollectorContinuityError:
            raise
        try:
            if opened.identity != self.identity:
                raise CollectorContinuityError("collector physical identity changed")
            current = os.fstat(self.file_fd)
            parent = os.fstat(self.parent_fd)
            if (
                (int(current.st_dev), int(current.st_ino))
                != (self.identity.file_st_dev, self.identity.file_st_ino)
                or (int(parent.st_dev), int(parent.st_ino))
                != (self.identity.parent_st_dev, self.identity.parent_st_ino)
            ):
                raise CollectorContinuityError("collector physical identity changed")
            return self.identity
        finally:
            opened.close()


@dataclass(slots=True)
class _OpenedCollectorFiles:
    database: _OpenedRegularFile
    ledger: _OpenedRegularFile

    def __enter__(self) -> "_OpenedCollectorFiles":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        stages: list[str] = []
        errors: list[BaseException] = []
        for stage, opened in (("database", self.database), ("ledger", self.ledger)):
            try:
                opened.close()
            except BaseException as exc:
                stages.append(stage)
                errors.append(exc)
        if errors:
            raise _collector_cleanup_error(
                "collector files close failed", stages, errors
            )

    def verify_identities(self) -> tuple[PhysicalFileIdentity, PhysicalFileIdentity]:
        database = self.database.verify_identity()
        ledger = self.ledger.verify_identity()
        if (
            database.parent_st_dev,
            database.parent_st_ino,
        ) != (
            ledger.parent_st_dev,
            ledger.parent_st_ino,
        ):
            raise CollectorContinuityError("collector files do not share a canonical parent")
        return database, ledger


def create_exclusive_regular_file(parent_fd: int, leaf: str) -> int:
    if not isinstance(leaf, str) or not leaf or leaf in {".", ".."} or "/" in leaf:
        raise CollectorContinuityError("collector leaf is invalid")
    try:
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise CollectorContinuityError("collector parent descriptor is not a directory")
        return os.open(
            leaf,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NONBLOCK | _no_follow_flag(),
            0o600,
            dir_fd=parent_fd,
        )
    except CollectorContinuityError:
        raise
    except OSError as exc:
        raise CollectorContinuityError("collector file cannot be created exclusively") from exc


def open_existing_regular_file(path: str | os.PathLike[str]) -> _OpenedRegularFile:
    parent_fd, canonical, leaf = _open_parent(path)
    file_fd: int | None = None
    try:
        file_fd = os.open(
            leaf,
            os.O_RDWR | os.O_NONBLOCK | _no_follow_flag(),
            dir_fd=parent_fd,
        )
        identity = _legacy_identity(_identity_from_open_file(file_fd, parent_fd, canonical))
        return _OpenedRegularFile(file_fd, parent_fd, leaf, identity)
    except CollectorContinuityError:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
        raise
    except OSError as exc:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
        raise CollectorContinuityError("collector file cannot be opened safely") from exc


def open_existing_collector_files(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str]
) -> _OpenedCollectorFiles:
    require_collector_continuity_health()
    database_parent, _ = _split_parent_and_leaf(database_path)
    ledger_parent, _ = _split_parent_and_leaf(ledger_path)
    if database_parent != ledger_parent:
        raise CollectorContinuityError("collector files must share a canonical parent")
    database = open_existing_regular_file(database_path)
    try:
        ledger = open_existing_regular_file(ledger_path)
    except Exception:
        database.close()
        raise
    opened = _OpenedCollectorFiles(database, ledger)
    try:
        opened.verify_identities()
        return opened
    except Exception:
        opened.close()
        raise


@contextmanager
def acquire_collector_registration_lock(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str]
) -> Iterator[_OpenedCollectorFiles]:
    """Hold the one non-blocking ledger lock needed for registration binding.

    This intentionally exposes no phase or child authority: task 3.1/3.2 only
    needs serialization across immutable registration-file creation and the
    first ledger binding.
    """

    require_collector_continuity_health()
    opened = open_existing_collector_files(
        database_path=database_path,
        ledger_path=ledger_path,
    )
    locked = False
    try:
        try:
            fcntl.flock(opened.ledger.file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise CollectorContinuityError("collector registration ledger is busy") from exc
        opened.verify_identities()
        yield opened
        opened.verify_identities()
    finally:
        if locked:
            try:
                fcntl.flock(opened.ledger.file_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        opened.close()


def create_exclusive_collector_files(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str]
) -> _OpenedCollectorFiles:
    require_collector_continuity_health()
    database_parent, database_leaf = _split_parent_and_leaf(database_path)
    ledger_parent, ledger_leaf = _split_parent_and_leaf(ledger_path)
    if database_parent != ledger_parent:
        raise CollectorContinuityError("collector files must share a canonical parent")
    parent_fd, _, _ = _open_parent(database_path)
    database_fd: int | None = None
    ledger_fd: int | None = None
    try:
        database_fd = create_exclusive_regular_file(parent_fd, database_leaf)
        ledger_fd = create_exclusive_regular_file(parent_fd, ledger_leaf)
        database_identity = _legacy_identity(
            _identity_from_open_file(database_fd, parent_fd, database_path)
        )
        ledger_identity = _legacy_identity(
            _identity_from_open_file(ledger_fd, parent_fd, ledger_path)
        )
        os.fsync(database_fd)
        os.fsync(ledger_fd)
        os.fsync(parent_fd)
        opened = _OpenedCollectorFiles(
            _OpenedRegularFile(database_fd, os.dup(parent_fd), database_leaf, database_identity),
            _OpenedRegularFile(ledger_fd, os.dup(parent_fd), ledger_leaf, ledger_identity),
        )
        database_fd = None
        ledger_fd = None
        return opened
    except Exception:
        if ledger_fd is not None:
            os.unlink(ledger_leaf, dir_fd=parent_fd)
        if database_fd is not None:
            os.unlink(database_leaf, dir_fd=parent_fd)
        raise
    finally:
        if database_fd is not None:
            os.close(database_fd)
        if ledger_fd is not None:
            os.close(ledger_fd)
        os.close(parent_fd)


def remove_created_collector_artifacts(
    *, database_identity: PhysicalFileIdentity, ledger_identity: PhysicalFileIdentity
) -> None:
    if (
        database_identity.parent_st_dev,
        database_identity.parent_st_ino,
    ) != (
        ledger_identity.parent_st_dev,
        ledger_identity.parent_st_ino,
    ):
        raise CollectorContinuityError("collector files do not share a canonical parent")
    parent_path, _ = _split_parent_and_leaf(database_identity.canonical_path)
    parent_fd, _, _ = _open_parent(os.path.join(parent_path, ".placeholder"))
    try:
        for identity in (database_identity, ledger_identity):
            _, leaf = _split_parent_and_leaf(identity.canonical_path)
            try:
                opened = open_existing_regular_file(identity.canonical_path)
            except CollectorContinuityError:
                continue
            try:
                if opened.identity == identity:
                    os.unlink(leaf, dir_fd=parent_fd)
                    for suffix in ("-journal", "-wal", "-shm"):
                        try:
                            os.unlink(f"{leaf}{suffix}", dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
            finally:
                opened.close()
    finally:
        os.close(parent_fd)


@contextmanager
def open_exact_collector_sqlite(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str]
):
    require_collector_continuity_health()
    opened = open_existing_collector_files(
        database_path=database_path,
        ledger_path=ledger_path,
    )
    connection: sqlite3.Connection | None = None
    body_error: BaseException | None = None
    entered = False
    try:
        connection = open_collector_connection(opened.database.identity.canonical_path)
        opened.verify_identities()
        entered = True
        yield connection, opened
    except BaseException as exc:
        body_error = exc
    finally:
        validation_error: BaseException | None = None
        close_error: BaseException | None = None
        if connection is not None:
            if entered:
                try:
                    _verify_collector_connection_contract(
                        connection, opened.database.identity.canonical_path
                    )
                except BaseException as exc:
                    validation_error = exc
            try:
                connection.close()
            except BaseException as exc:
                close_error = (
                    exc
                    if close_error is None
                    else _combine_collector_context_errors(close_error, exc)
                )
        if entered and validation_error is None and close_error is None:
            try:
                _reject_sqlite_wal_sidecars(opened.database.identity.canonical_path)
                opened.verify_identities()
            except BaseException as exc:
                validation_error = exc
        try:
            opened.close()
        except BaseException as exc:
            close_error = (
                exc
                if close_error is None
                else _combine_collector_context_errors(close_error, exc)
            )
        if validation_error is not None and body_error is not None:
            validation_error.__cause__ = body_error
        primary_error = validation_error or body_error
        if close_error is not None:
            if primary_error is not None:
                raise _combine_collector_context_errors(
                    primary_error, close_error
                ) from None
            raise close_error
        if validation_error is not None:
            raise validation_error
        if body_error is not None:
            raise body_error


_PREPARED_GENESIS_TABLE_SQL: Final = """
CREATE TABLE forward_collector_genesis (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    database_uuid TEXT NOT NULL,
    cohort_sha256 TEXT NOT NULL,
    genesis_json TEXT NOT NULL,
    genesis_sha256 TEXT NOT NULL,
    ledger_genesis_event_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
_PREPARED_GENESIS_TRIGGER_SQL: Final = {
    "forward_collector_genesis_no_update": """
        CREATE TRIGGER forward_collector_genesis_no_update
        BEFORE UPDATE ON forward_collector_genesis BEGIN
            SELECT RAISE(ABORT, 'collector genesis is immutable');
        END
    """,
    "forward_collector_genesis_no_delete": """
        CREATE TRIGGER forward_collector_genesis_no_delete
        BEFORE DELETE ON forward_collector_genesis BEGIN
            SELECT RAISE(ABORT, 'collector genesis is immutable');
        END
    """,
}
_PREPARED_EVIDENCE_TRIGGER_SQL: Final = {
    "daily_non_final_insert": """
        CREATE TRIGGER daily_non_final_insert
        BEFORE INSERT ON daily WHEN NEW.is_final != 1 BEGIN
            SELECT RAISE(ABORT, 'daily evidence must be finalized on insert');
        END
    """,
    "collector_daily_final_no_update": """
        CREATE TRIGGER collector_daily_final_no_update
        BEFORE UPDATE ON daily WHEN OLD.is_final=1 AND (
            NEW.code IS NOT OLD.code OR NEW.date IS NOT OLD.date
            OR NEW.open IS NOT OLD.open OR NEW.high IS NOT OLD.high
            OR NEW.low IS NOT OLD.low OR NEW.close IS NOT OLD.close
            OR NEW.volume IS NOT OLD.volume OR NEW.source IS NOT OLD.source
            OR NEW.adjustment_mode IS NOT OLD.adjustment_mode
            OR NEW.adjustment_version IS NOT OLD.adjustment_version
            OR NEW.retrieved_at IS NOT OLD.retrieved_at
            OR NEW.is_final IS NOT OLD.is_final OR NEW.receipt_id IS NOT OLD.receipt_id
        ) BEGIN
            SELECT RAISE(ABORT, 'finalized daily evidence is immutable');
        END
    """,
    "collector_daily_final_no_delete": """
        CREATE TRIGGER collector_daily_final_no_delete
        BEFORE DELETE ON daily WHEN OLD.is_final=1 BEGIN
            SELECT RAISE(ABORT, 'finalized daily evidence is immutable');
        END
    """,
    "collector_sync_coverage_no_update": """
        CREATE TRIGGER collector_sync_coverage_no_update
        BEFORE UPDATE ON sync_coverage WHEN
            NEW.code IS NOT OLD.code OR NEW.source IS NOT OLD.source
            OR NEW.adjustment_mode IS NOT OLD.adjustment_mode
            OR NEW.adjustment_version IS NOT OLD.adjustment_version
            OR NEW.start_date > OLD.start_date OR NEW.end_date < OLD.end_date
            OR NEW.start_date > NEW.end_date BEGIN
            SELECT RAISE(ABORT, 'collector sync coverage must retain identity');
        END
    """,
    "sync_coverage_exact_noop": """
        CREATE TRIGGER sync_coverage_exact_noop
        BEFORE UPDATE ON sync_coverage WHEN NEW.code IS OLD.code
            AND NEW.source IS OLD.source
            AND NEW.adjustment_mode IS OLD.adjustment_mode
            AND NEW.adjustment_version IS OLD.adjustment_version
            AND NEW.start_date IS OLD.start_date AND NEW.end_date IS OLD.end_date BEGIN
            SELECT RAISE(IGNORE);
        END
    """,
    "collector_sync_coverage_no_delete": """
        CREATE TRIGGER collector_sync_coverage_no_delete
        BEFORE DELETE ON sync_coverage BEGIN
            SELECT RAISE(ABORT, 'collector sync coverage is append-only');
        END
    """,
}


def _canonical_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";")


def _frozen_authority_contract() -> tuple[dict[str, str], dict[str, str]]:
    """Expand only version-controlled schema installers into an isolated database.

    SQLite records the submitted DDL verbatim enough that hand-normalising source
    strings would make the authority brittle.  This expansion is intentionally
    performed from local module constants, never from a collector candidate.
    """

    from .cache import _RECEIPT_SCHEMA, _SCHEMA, _SYNC_SCHEMA
    from .forward_capture import _bind_cohort
    from .forward_context import _ensure_schema as ensure_context_schema
    from .forward_corporate_actions import _ensure_schema as ensure_action_schema

    class _FrozenSchemaCache:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._conn = connection

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SCHEMA)
        connection.executescript(_SYNC_SCHEMA)
        connection.executescript(_RECEIPT_SCHEMA)
        cache = _FrozenSchemaCache(connection)
        ensure_context_schema(cache)  # type: ignore[arg-type]
        ensure_action_schema(cache)  # type: ignore[arg-type]
        _bind_cohort(
            cache,  # type: ignore[arg-type]
            {
                "symbols": ["000001.SZ"],
                "start": "2099-01-05",
                "source": "tencent",
                "adjustment_mode": "raw",
                "adjustment_version": "tencent-qt-daily-v1",
            },
        )
        connection.execute(_PREPARED_GENESIS_TABLE_SQL)
        for sql in {**_PREPARED_GENESIS_TRIGGER_SQL, **_PREPARED_EVIDENCE_TRIGGER_SQL}.values():
            connection.execute(sql)
        rows = {
            (str(kind), str(name)): str(sql or "")
            for kind, name, sql in connection.execute(
                "SELECT type,name,sql FROM main.sqlite_master "
                "WHERE type IN ('table','trigger')"
            )
        }
        tables = {name: rows[("table", name)] for name in _COLLECTOR_TABLE_NAMES}
        triggers = {name: rows[("trigger", name)] for name in _COLLECTOR_TRIGGER_NAMES}
        return tables, triggers
    except (KeyError, sqlite3.Error, ValueError) as exc:
        raise RuntimeError("frozen collector authority schema cannot be expanded") from exc
    finally:
        connection.close()


_FROZEN_TABLE_SQL, _FROZEN_TRIGGER_SQL = _frozen_authority_contract()
_COLLECTOR_OWNED_TABLE_SQL.update(_FROZEN_TABLE_SQL)
COLLECTOR_EVIDENCE_TRIGGER_SQL.update(_FROZEN_TRIGGER_SQL)
_GENESIS_TRIGGERS.update(
    {
        name: COLLECTOR_EVIDENCE_TRIGGER_SQL[name]
        for name in (
            "forward_collector_genesis_no_update",
            "forward_collector_genesis_no_delete",
        )
    }
)


def _refresh_collector_schema_contract(connection: sqlite3.Connection) -> None:
    """Reject authority drift; never learn authority from a candidate database."""

    verify_collector_authority_schema(connection)


def install_collector_evidence_triggers(connection: sqlite3.Connection) -> None:
    """Install the collector-only immutability and monotonicity guards."""

    genesis_exists = connection.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='forward_collector_genesis'"
    ).fetchone()
    if genesis_exists is None:
        connection.execute(_PREPARED_GENESIS_TABLE_SQL)
    installed = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM main.sqlite_master WHERE type='trigger'"
        )
    }
    for name, sql in COLLECTOR_EVIDENCE_TRIGGER_SQL.items():
        if name not in installed:
            connection.execute(sql)


def verify_collector_evidence_triggers(
    connection: sqlite3.Connection, *, require_exact_set: bool = True
) -> None:
    """Reject missing, replaced, or unexpected collector evidence triggers."""

    actual = {
        str(name): str(sql or "")
        for name, sql in connection.execute(
            "SELECT name,sql FROM main.sqlite_master WHERE type='trigger'"
        )
    }
    if require_exact_set and set(actual) != set(COLLECTOR_EVIDENCE_TRIGGER_SQL):
        raise CollectorContinuityError("collector evidence triggers are not exact")
    for name, expected in COLLECTOR_EVIDENCE_TRIGGER_SQL.items():
        if name in actual and expected and _canonical_sql(actual[name]) != _canonical_sql(expected):
            raise CollectorContinuityError("collector evidence triggers are invalid")
        if require_exact_set and name not in actual:
            raise CollectorContinuityError("collector evidence triggers are invalid")


def verify_collector_authority_schema(connection: sqlite3.Connection) -> None:
    """Verify every collector-owned table and trigger against its frozen SQL."""

    actual_tables = {
        str(name): str(sql or "")
        for name, sql in connection.execute(
            "SELECT name,sql FROM main.sqlite_master WHERE type='table'"
        )
    }
    for name, expected in _COLLECTOR_OWNED_TABLE_SQL.items():
        if not expected or name not in actual_tables or _canonical_sql(actual_tables[name]) != _canonical_sql(expected):
            raise CollectorContinuityError("collector-owned table schemas are invalid")
    verify_collector_evidence_triggers(connection)


def _read_prepared_cohort(connection: sqlite3.Connection) -> tuple[dict[str, object], str]:
    rows = connection.execute(
        "SELECT spec_json,spec_sha256 FROM main.forward_capture_cohort WHERE singleton=1"
    ).fetchall()
    if len(rows) != 1:
        raise CollectorContinuityError("collector cohort must have exactly one row")
    try:
        raw = str(rows[0][0]).encode("ascii")
    except UnicodeEncodeError as exc:
        raise CollectorContinuityError("collector cohort JSON is invalid") from exc
    cohort = decode_canonical_json_object(raw)
    digest = _require_sha256(rows[0][1], "collector cohort hash")
    if canonical_json_sha256(cohort) != digest:
        raise CollectorContinuityError("collector cohort hash is invalid")
    return cohort, digest


def _load_cohort_sha256(connection: sqlite3.Connection) -> str:
    """Return the verified prepared cohort digest for compatibility callers."""

    return _read_prepared_cohort(connection)[1]


def _prepared_schema_sha256(connection: sqlite3.Connection) -> str:
    rows = [
        [str(kind), str(name), str(sql or "")]
        for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM main.sqlite_master "
            "WHERE type IN ('table','trigger') ORDER BY type,name"
        )
    ]
    return canonical_json_sha256({"sqlite_master": rows})


def _prepared_genesis(
    *, database_uuid: str, cohort_sha256: str, database_identity: PhysicalFileIdentity,
    ledger_identity: PhysicalFileIdentity, created_at: str, collector_schema_sha256: str,
) -> dict[str, object]:
    value = {
        "schema_version": COLLECTOR_GENESIS_SCHEMA,
        "database_uuid": database_uuid,
        "cohort_sha256": cohort_sha256,
        "database_identity": database_identity.to_dict(),
        "ledger_identity": ledger_identity.to_dict(),
        "created_at": created_at,
        "collector_schema_sha256": collector_schema_sha256,
    }
    return _validate_prepared_genesis(value)


def _validate_prepared_genesis(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the one nested GENESIS payload shared with prepared collectors."""

    expected = {
        "schema_version",
        "database_uuid",
        "cohort_sha256",
        "database_identity",
        "ledger_identity",
        "created_at",
        "collector_schema_sha256",
    }
    require_exact_keys(value, expected, "prepared genesis")
    if value["schema_version"] != COLLECTOR_GENESIS_SCHEMA:
        raise CollectorContinuityError("prepared genesis schema is invalid")
    _require_sha256(value["database_uuid"], "prepared genesis database_uuid")
    _require_sha256(value["cohort_sha256"], "prepared genesis cohort_sha256")
    _require_sha256(value["collector_schema_sha256"], "prepared genesis collector_schema_sha256")
    _require_event_text(value["created_at"], "prepared genesis created_at")
    for field_name in ("database_identity", "ledger_identity"):
        if not isinstance(value[field_name], Mapping):
            raise CollectorContinuityError(f"prepared genesis {field_name} is invalid")
        PhysicalFileIdentity.from_dict(value[field_name])
    return decode_canonical_json_object(canonical_json_bytes(dict(value)))


def build_collector_genesis_ledger_event(genesis: Mapping[str, object]) -> dict[str, object]:
    """Build the sole event needed to bind an empty prepared collector."""

    decoded = decode_canonical_json_object(canonical_json_bytes(dict(genesis)))
    payload: dict[str, object] = {
        "schema_version": COLLECTOR_LEDGER_EVENT_SCHEMA,
        "seq": 0,
        "event_type": "GENESIS",
        "previous_event_sha256": ZERO_SHA256,
        "event": {"genesis": decoded},
    }
    return validate_collector_ledger_event(
        {**payload, "event_sha256": canonical_json_sha256(payload)}
    )


def decode_collector_ledger_event(raw: bytes) -> dict[str, object]:
    value = decode_canonical_json_object(raw)
    return validate_collector_ledger_event(value)


def _ledger_event_without_hash(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "seq", "event_type", "previous_event_sha256", "event", "event_sha256"
    }
    require_exact_keys(value, required, "collector ledger event")
    if (
        value["schema_version"] != COLLECTOR_LEDGER_EVENT_SCHEMA
        or type(value["seq"]) is not int
        or not isinstance(value["event_type"], str)
        or not isinstance(value["event"], dict)
    ):
        raise CollectorContinuityError("collector ledger event is invalid")
    _require_sha256(value["previous_event_sha256"], "previous event hash")
    _require_sha256(value["event_sha256"], "event hash")
    without_hash = {key: item for key, item in value.items() if key != "event_sha256"}
    if value["event_sha256"] != canonical_json_sha256(without_hash):
        raise CollectorContinuityError("collector ledger event hash is invalid")
    return without_hash


def _ledger_source_bytes(source: object) -> bytes:
    opened: OpenedRegularFile | _OpenedRegularFile | None = None
    close_after = False
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, os.PathLike)):
        opened = open_nofollow_regular(source)
        close_after = True
    elif isinstance(source, (OpenedRegularFile, _OpenedRegularFile)):
        opened = source
    else:
        raise CollectorContinuityError("collector ledger source is invalid")
    try:
        descriptor = _verify_opened_ledger_identity(opened)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size < 0:
            raise CollectorContinuityError("collector ledger source is not a regular file")
        if status.st_size > COLLECTOR_LEDGER_MAX_BYTES:
            raise CollectorContinuityError("collector ledger exceeds byte limit")
        data = bytearray()
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(descriptor, min(64 * 1024, status.st_size - offset), offset)
            if not chunk:
                raise CollectorContinuityError("collector ledger was truncated while reading")
            data.extend(chunk)
            offset += len(chunk)
        if os.fstat(descriptor).st_size != status.st_size:
            raise CollectorContinuityError("collector ledger changed while reading")
        _verify_opened_ledger_identity(opened)
        return bytes(data)
    except OSError as exc:
        raise CollectorContinuityError("collector ledger cannot be read") from exc
    finally:
        if close_after and opened is not None:
            opened.close()


def _verify_opened_ledger_identity(opened: OpenedRegularFile | _OpenedRegularFile) -> int:
    """Prove both the held descriptor and its canonical path still name one file."""

    if isinstance(opened, _OpenedRegularFile):
        opened.verify_identity()
        return opened.file_fd
    status = os.fstat(opened.descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or (int(status.st_dev), int(status.st_ino))
        != (opened.identity.file_st_dev, opened.identity.file_st_ino)
    ):
        raise CollectorContinuityError("collector ledger physical identity changed")
    verify_file_identity(opened.identity.canonical_path, opened.identity)
    return opened.descriptor


def _validated_ledger_lines(data: bytes) -> tuple[dict[str, object], ...]:
    if not data or len(data) > COLLECTOR_LEDGER_MAX_BYTES or not data.endswith(b"\n"):
        raise CollectorContinuityError("collector ledger JSONL framing is invalid")
    lines = data[:-1].split(b"\n")
    if not lines or len(lines) > COLLECTOR_LEDGER_MAX_LINES:
        raise CollectorContinuityError("collector ledger line count is invalid")
    events: list[dict[str, object]] = []
    for line in lines:
        if not line or len(line) > COLLECTOR_LEDGER_MAX_LINE_BYTES:
            raise CollectorContinuityError("collector ledger line is invalid")
        events.append(decode_collector_ledger_event(line))
    _validate_ledger_chain(events)
    return tuple(events)


def parse_collector_ledger(source: object) -> tuple[dict[str, object], ...]:
    opened: OpenedRegularFile | _OpenedRegularFile | None = None
    close_after = False
    if isinstance(source, bytes):
        return _validated_ledger_lines(source)
    if isinstance(source, (str, os.PathLike)):
        opened = open_nofollow_regular(source)
        close_after = True
    elif isinstance(source, (OpenedRegularFile, _OpenedRegularFile)):
        opened = source
    else:
        raise CollectorContinuityError("collector ledger source is invalid")
    try:
        _verify_opened_ledger_identity(opened)
        parsed = _validated_ledger_lines(_ledger_source_bytes(opened))
        _verify_opened_ledger_identity(opened)
        return parsed
    finally:
        if close_after:
            opened.close()


def _parse_retained_bound_collector_ledger(
    path: str | os.PathLike[str], expected_identity: PhysicalFileIdentity
) -> tuple[dict[str, object], ...]:
    """Parse a ledger only through one retained no-follow descriptor."""

    canonical_path = lexical_absolute_path(path)
    if canonical_path != expected_identity.canonical_path:
        raise CollectorContinuityError("collector ledger authority path drifted")
    opened = open_nofollow_regular(canonical_path)
    try:
        if opened.identity != expected_identity:
            raise CollectorContinuityError("collector ledger authority identity drifted")
        parsed = parse_collector_ledger(opened)
        if opened.identity != expected_identity:
            raise CollectorContinuityError("collector ledger authority identity drifted")
        _verify_opened_ledger_identity(opened)
        return parsed
    finally:
        opened.close()


def build_collector_ledger_event(
    *, previous_event: Mapping[str, object] | None, event_type: str, event: Mapping[str, object]
) -> dict[str, object]:
    if previous_event is None or event_type not in LEDGER_EVENT_TYPES or event_type == "GENESIS":
        raise CollectorContinuityError("collector ledger event type is invalid")
    previous = decode_collector_ledger_event(canonical_json_bytes(dict(previous_event)))
    if previous["event_type"] == "GENESIS" and previous["seq"] != 0:
        raise CollectorContinuityError("collector ledger predecessor is invalid")
    detail = dict(event)
    # Persist the exact terminal schema even when an in-process caller uses a
    # pre-correction detail fixture. Parsed ledger records never use a default.
    if event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"} and "process_launch_state" not in detail:
        if detail.get("process_result_known") is True:
            detail["process_launch_state"] = "handle_obtained"
        elif detail.get("recovered") is True:
            detail["process_launch_state"] = "indeterminate"
        else:
            detail["process_launch_state"] = "not_invoked"
    payload: dict[str, object] = {
        "schema_version": COLLECTOR_LEDGER_EVENT_SCHEMA,
        "seq": int(previous["seq"]) + 1,
        "event_type": event_type,
        "previous_event_sha256": previous["event_sha256"],
        "event": detail,
    }
    return validate_collector_ledger_event(
        {**payload, "event_sha256": canonical_json_sha256(payload)}
    )


def _write_all(descriptor: int, payload: bytes, *, label: str) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except OSError as exc:
            raise CollectorContinuityError(f"{label} write failed") from exc
        if written <= 0:
            raise CollectorContinuityError(f"{label} short write")
        remaining = remaining[written:]


def initialize_prepared_collector(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str], created_at: str
) -> dict[str, object]:
    """Bind an empty 12-by-3 schema to one immutable genesis event."""

    require_collector_continuity_health()
    with open_exact_collector_sqlite(
        database_path=database_path, ledger_path=ledger_path
    ) as (connection, opened):
        database_identity, ledger_identity = opened.verify_identities()
        cohort, cohort_sha256 = _read_prepared_cohort(connection)
        symbols = cohort.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 12:
            raise CollectorContinuityError("collector cohort is not 12 symbols")
        install_collector_evidence_triggers(connection)
        _refresh_collector_schema_contract(connection)
        schema_sha256 = _prepared_schema_sha256(connection)
        genesis = _prepared_genesis(
            database_uuid=secrets.token_hex(32),
            cohort_sha256=cohort_sha256,
            database_identity=database_identity,
            ledger_identity=ledger_identity,
            created_at=created_at,
            collector_schema_sha256=schema_sha256,
        )
        genesis_sha256 = canonical_json_sha256(genesis)
        ledger_event = build_collector_genesis_ledger_event(genesis)
        ledger_payload = canonical_json_bytes(ledger_event) + b"\n"
        if os.fstat(opened.ledger.file_fd).st_size != 0:
            raise CollectorContinuityError("collector genesis ledger is not empty")
        _write_all(opened.ledger.file_fd, ledger_payload, label="collector genesis ledger")
        try:
            os.fsync(opened.ledger.file_fd)
        except (AttributeError, OSError) as exc:
            raise CollectorContinuityError("collector genesis ledger fsync failed") from exc
        connection.execute(
            "INSERT INTO main.forward_collector_genesis "
            "(singleton,database_uuid,cohort_sha256,genesis_json,genesis_sha256,"
            "ledger_genesis_event_sha256,created_at) VALUES (1,?,?,?,?,?,?)",
            (
                genesis["database_uuid"], cohort_sha256,
                canonical_json_bytes(genesis).decode("ascii"), genesis_sha256,
                ledger_event["event_sha256"], created_at,
            ),
        )
        connection.commit()
        try:
            os.fsync(opened.database.file_fd)
            os.fsync(opened.ledger.file_fd)
            os.fsync(opened.database.parent_fd)
            os.fsync(opened.ledger.parent_fd)
        except (AttributeError, OSError) as exc:
            raise CollectorContinuityError("collector genesis database fsync failed") from exc
        opened.verify_identities()
    return {
        "schema_version": COLLECTOR_PREPARATION_SCHEMA,
        "database_path": database_identity.canonical_path,
        "ledger_path": ledger_identity.canonical_path,
        "database_identity": database_identity.to_dict(),
        "ledger_identity": ledger_identity.to_dict(),
        "database_uuid": genesis["database_uuid"],
        "cohort_sha256": cohort_sha256,
        "genesis_sha256": genesis_sha256,
        "ledger_genesis_event_sha256": ledger_event["event_sha256"],
        "collector_schema_sha256": schema_sha256,
    }


def load_verified_prepared_collector(
    *, database_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str] | None = None
) -> dict[str, object]:
    require_collector_continuity_health()
    ledger_path = ledger_path or default_collector_ledger_path(database_path)
    with open_exact_collector_sqlite(
        database_path=database_path, ledger_path=ledger_path
    ) as (connection, opened):
        database_identity, ledger_identity = opened.verify_identities()
        verify_collector_authority_schema(connection)
        cohort, cohort_sha256 = _read_prepared_cohort(connection)
        rows = connection.execute(
            "SELECT database_uuid,cohort_sha256,genesis_json,genesis_sha256,"
            "ledger_genesis_event_sha256,created_at FROM main.forward_collector_genesis"
        ).fetchall()
        if len(rows) != 1:
            raise CollectorContinuityError("collector genesis must have exactly one row")
        row = rows[0]
        try:
            genesis = decode_canonical_json_object(str(row[2]).encode("ascii"))
        except UnicodeEncodeError as exc:
            raise CollectorContinuityError("prepared genesis is invalid") from exc
        require_exact_keys(
            genesis,
            {
                "schema_version",
                "database_uuid",
                "cohort_sha256",
                "database_identity",
                "ledger_identity",
                "created_at",
                "collector_schema_sha256",
            },
            "prepared genesis",
        )
        if (
            genesis["schema_version"] != COLLECTOR_GENESIS_SCHEMA
            or genesis["database_uuid"] != row[0]
            or genesis["cohort_sha256"] != row[1]
            or row[1] != cohort_sha256
            or canonical_json_sha256(genesis) != row[3]
            or genesis["database_identity"] != database_identity.to_dict()
            or genesis["ledger_identity"] != ledger_identity.to_dict()
            or genesis["created_at"] != row[5]
            or genesis["collector_schema_sha256"] != _prepared_schema_sha256(connection)
        ):
            raise CollectorContinuityError("prepared genesis drifted")
        ledger = _parse_retained_bound_collector_ledger(ledger_path, ledger_identity)
        if not ledger:
            raise CollectorContinuityError("prepared genesis ledger is empty")
        event = ledger[0]
        if (
            event["seq"] != 0 or event["event_type"] != "GENESIS"
            or event["previous_event_sha256"] != ZERO_SHA256
            or event["event"].get("genesis") != genesis
            or event["event_sha256"] != row[4]
        ):
            raise CollectorContinuityError("prepared genesis drifted")
        return {
            "schema_version": COLLECTOR_PREPARATION_SCHEMA,
            "database_path": database_identity.canonical_path,
            "ledger_path": ledger_identity.canonical_path,
            "database_identity": database_identity.to_dict(),
            "ledger_identity": ledger_identity.to_dict(),
            "database_uuid": row[0],
            "cohort_sha256": cohort_sha256,
            "genesis_sha256": row[3],
            "ledger_genesis_event_sha256": row[4],
            "collector_schema_sha256": genesis["collector_schema_sha256"],
        }


def compute_collector_logical_state(connection: sqlite3.Connection) -> dict[str, object]:
    if not isinstance(connection, sqlite3.Connection):
        raise CollectorContinuityError("collector logical state connection is invalid")
    if connection.in_transaction:
        raise CollectorContinuityError("collector logical state requires no active transaction")

    original_text_factory = connection.text_factory
    original_row_factory = connection.row_factory
    query_only: int | None = None
    began = False
    try:
        connection.text_factory = bytes
        connection.row_factory = None
        query_only_row = connection.execute("PRAGMA query_only").fetchone()
        if query_only_row is None or type(query_only_row[0]) is not int:
            raise CollectorContinuityError("collector logical state query-only mode is invalid")
        query_only = query_only_row[0]
        connection.execute("PRAGMA query_only=1")
        connection.execute("BEGIN")
        began = True
        digest, table_counts = _compute_collector_logical_digest(connection)
        connection.rollback()
        began = False
        return validate_collector_logical_state(
            {
                "schema_version": COLLECTOR_LOGICAL_STATE_SCHEMA,
                "collector_state_sha256": digest.hexdigest(),
                "table_counts": table_counts,
            }
        )
    except CollectorContinuityError as exc:
        raise CollectorContinuityError("collector logical state cannot be computed") from exc
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        raise CollectorContinuityError("collector logical state cannot be computed") from exc
    finally:
        cleanup_error: BaseException | None = None
        if began or connection.in_transaction:
            try:
                connection.rollback()
            except sqlite3.Error as exc:
                cleanup_error = exc
            else:
                if connection.in_transaction:
                    cleanup_error = CollectorContinuityError(
                        "collector logical state transaction did not close"
                    )
        try:
            if query_only is not None:
                connection.execute(f"PRAGMA query_only={query_only}")
        except sqlite3.Error as exc:
            cleanup_error = cleanup_error or exc
        try:
            connection.row_factory = original_row_factory
            connection.text_factory = original_text_factory
        except (AttributeError, TypeError) as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise CollectorContinuityError(
                "collector logical state connection restoration failed"
            ) from cleanup_error


def _compute_collector_logical_digest(
    connection: sqlite3.Connection,
) -> tuple["hashlib._Hash", dict[str, int]]:
    digest = hashlib.sha256()
    digest.update(_COLLECTOR_LOGICAL_STATE_DOMAIN)
    table_counts: dict[str, int] = {}
    for table, columns, primary_key in _COLLECTOR_LOGICAL_STATE_TABLES:
        count_row = connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()
        if count_row is None or type(count_row[0]) is not int or count_row[0] < 0:
            raise CollectorContinuityError("collector logical state table count is invalid")
        count = count_row[0]
        table_counts[table] = count
        _hash_collector_logical_record(
            digest,
            {
                "columns": list(columns),
                "count": count,
                "kind": "table",
                "primary_key": list(primary_key),
                "table": table,
            },
        )
        for values in _iter_collector_encoded_rows(connection, table, columns, primary_key):
            _hash_collector_logical_record(digest, {"kind": "row", "values": values})
    return digest, table_counts


def _iter_collector_encoded_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
) -> Iterator[list[str]]:
    select_items = ", ".join(
        f'typeof("{column}"), "{column}" + 0, CAST("{column}" AS BLOB)'
        for column in columns
    )
    ordering = ", ".join(f'"{column}" COLLATE BINARY ASC' for column in primary_key)
    rows = connection.execute(
        f'SELECT {select_items} FROM main."{table}" ORDER BY {ordering}'
    )
    for row in rows:
        yield [
            _encode_collector_logical_cell(
                row[index * 3], row[index * 3 + 1], row[index * 3 + 2]
            )
            for index in range(len(columns))
        ]


def validate_collector_logical_state(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CollectorContinuityError("collector logical state is invalid")
    require_exact_keys(
        value,
        {"schema_version", "collector_state_sha256", "table_counts"},
        "collector logical state",
    )
    if value["schema_version"] != COLLECTOR_LOGICAL_STATE_SCHEMA:
        raise CollectorContinuityError("collector logical state schema is invalid")
    _require_event_sha256(value["collector_state_sha256"], "logical state hash")
    counts = value["table_counts"]
    expected_tables = set(COLLECTOR_STATE_TABLES)
    if not isinstance(counts, Mapping) or set(counts) != expected_tables:
        raise CollectorContinuityError("collector logical state table counts are invalid")
    for count in counts.values():
        if type(count) is not int or count < 0:
            raise CollectorContinuityError("collector logical state table count is invalid")
    return decode_canonical_json_object(canonical_json_bytes(dict(value)))


def _hash_collector_logical_record(
    digest: "hashlib._Hash", value: Mapping[str, object]
) -> None:
    encoded = canonical_json_bytes(dict(value))
    digest.update(struct.pack(">Q", len(encoded)))
    digest.update(encoded)
    digest.update(b"\n")


def _encode_collector_logical_cell(
    storage_class: object, numeric_value: object, raw_value: object
) -> list[str]:
    if isinstance(storage_class, bytes):
        try:
            storage = storage_class.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CollectorContinuityError("collector logical state storage class is invalid") from exc
    elif isinstance(storage_class, str):
        storage = storage_class
    else:
        raise CollectorContinuityError("collector logical state storage class is invalid")
    if storage == "null":
        return ["null"]
    if storage == "integer":
        if type(numeric_value) is not int:
            raise CollectorContinuityError("collector logical state integer is invalid")
        return ["integer", str(numeric_value)]
    if storage == "real":
        if type(numeric_value) is not float or not math.isfinite(numeric_value):
            raise CollectorContinuityError("collector logical state real is invalid")
        return ["real", "0x0.0p+0" if numeric_value == 0.0 else numeric_value.hex()]
    if storage == "text":
        if not isinstance(raw_value, bytes):
            raise CollectorContinuityError("collector logical state text is invalid")
        try:
            return ["text", raw_value.decode("utf-8")]
        except UnicodeDecodeError as exc:
            raise CollectorContinuityError("collector logical state text is not valid UTF-8") from exc
    if storage == "blob":
        raise CollectorContinuityError("collector logical state cannot encode BLOB")
    raise CollectorContinuityError("collector logical state storage class is invalid")


def validate_collector_ledger_event(value: Mapping[str, object]) -> dict[str, object]:
    payload = _ledger_event_without_hash(value)
    event_type = payload["event_type"]
    if event_type not in LEDGER_EVENT_TYPES:
        raise CollectorContinuityError("collector ledger event type is invalid")
    seq = payload["seq"]
    previous = payload["previous_event_sha256"]
    details = payload["event"]
    if type(seq) is not int or seq < 0 or not isinstance(details, dict):
        raise CollectorContinuityError("collector ledger event is invalid")
    if event_type == "GENESIS":
        if seq != 0 or previous != ZERO_SHA256:
            raise CollectorContinuityError("collector ledger genesis position is invalid")
        require_exact_keys(details, {"genesis"}, "collector ledger genesis event")
        if not isinstance(details["genesis"], dict):
            raise CollectorContinuityError("collector ledger genesis is invalid")
        _validate_prepared_genesis(details["genesis"])
    else:
        if seq == 0 or previous == ZERO_SHA256:
            raise CollectorContinuityError("collector ledger non-genesis position is invalid")
        _validate_ledger_detail(str(event_type), details)
    return decode_canonical_json_object(
        canonical_json_bytes({**payload, "event_sha256": value["event_sha256"]})
    )


def _require_event_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CollectorContinuityError(f"collector ledger {label} is invalid")
    return value


def _require_event_sha256(value: object, label: str) -> str:
    return _require_sha256(value, f"collector ledger {label}")


_STEP_IDENTITY: Final[dict[str, tuple[str, int, frozenset[str]]]] = {
    "pre_open_context": (
        "pre_open",
        0,
        frozenset({
            "collection_receipts",
            "forward_context_observations",
            "forward_universe_observations",
            "forward_status_observations",
        }),
    ),
    "pre_open_corporate_actions": (
        "pre_open",
        1,
        frozenset({
            "collection_receipts",
            "forward_corporate_action_coverage",
            "forward_corporate_actions",
        }),
    ),
    "post_close_context": (
        "post_close",
        2,
        frozenset({
            "collection_receipts",
            "forward_context_observations",
            "forward_universe_observations",
            "forward_status_observations",
        }),
    ),
    "post_close_prices": (
        "post_close",
        3,
        frozenset({"collection_receipts", "daily", "sync_coverage"}),
    ),
}

_ATTEMPT_FAILURE_RETRYABILITY: Final[dict[str, bool]] = {
    "child_launch_failed": True,
    "child_no_commit": True,
    "child_partial_prices": True,
    "interrupted_no_commit": True,
    "interrupted_partial_prices": True,
    "child_process_failed_after_complete": False,
    "forbidden_drift": False,
    "postflight_authority_failure": False,
    "rollback_journal_recovery_failed": False,
}

_RECOVERY_START_FIELDS: Final[frozenset[str]] = frozenset(
    {
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
)
_RECOVERY_TERMINAL_FIELDS: Final[frozenset[str]] = (
    _RECOVERY_START_FIELDS
    | {
        "recovery_started_event_sha256",
        "state_after_sha256",
        "step_state_after",
    }
)


def _validate_step_state_shape(
    value: object, *, allowed_tables: frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CollectorContinuityError("collector step state is invalid")
    return validate_collector_step_state(value, allowed_tables=allowed_tables)


def _validate_ledger_detail(event_type: str, detail: Mapping[str, object]) -> None:
    registration_fields = {
        "registration_sha256", "panel_sha256", "sessions", "sessions_sha256",
        "prerequisites_sha256", "bound_at",
    }
    attempt_identity = {
        "registration_sha256", "database_uuid", "state_before_sha256", "session", "phase",
        "step_id", "step_ordinal", "attempt_id", "command_sha256", "started_at",
        "step_state_before", "step_raw_before",
    }
    if event_type == "REGISTRATION_BOUND":
        require_exact_keys(detail, registration_fields, "collector registration event")
        for key in ("registration_sha256", "panel_sha256", "sessions_sha256", "prerequisites_sha256"):
            _require_event_sha256(detail[key], key)
        sessions = _validate_registration_sessions(detail["sessions"])
        if detail["sessions_sha256"] != canonical_json_sha256(list(sessions)):
            raise CollectorContinuityError("collector registration sessions hash is invalid")
        _require_event_text(detail["bound_at"], "bound_at")
        return
    if event_type == "SQLITE_RECOVERY_STARTED":
        _validate_recovery_start_detail(detail)
        return
    if event_type in {"SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"}:
        _validate_recovery_terminal_detail(event_type, detail)
        return
    if event_type == "ATTEMPT_STARTED":
        require_exact_keys(
            detail, attempt_identity | {"lease_nonce_sha256"}, "collector attempt start"
        )
        _validate_attempt_identity(detail, require_nonce=True)
        return
    if event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
        fields = attempt_identity | {
            "state_after_sha256", "step_state_after", "returncode", "stdout_sha256", "stdout_bytes",
            "stderr_sha256", "stderr_bytes", "process_result_known", "process_launch_state",
            "recovered", "verifier_id",
        }
        if event_type == "ATTEMPT_COMPLETED":
            fields |= {"completed_at"}
        else:
            fields |= {"failed_at", "failure_classification", "retryable"}
        require_exact_keys(detail, fields, "collector attempt terminal")
        _validate_attempt_identity(detail, require_nonce=False)
        _require_event_sha256(detail["state_after_sha256"], "state_after_sha256")
        allowed_tables = _attempt_allowed_tables(detail)
        if _validate_step_state_shape(
            detail["step_state_after"], allowed_tables=allowed_tables
        )["collector_state_sha256"] != detail["state_after_sha256"]:
            raise CollectorContinuityError("collector attempt state_after hash is invalid")
        launch_state = detail["process_launch_state"]
        if launch_state not in {"not_invoked", "handle_obtained", "indeterminate"}:
            raise CollectorContinuityError("collector attempt launch state is invalid")
        if type(detail["process_result_known"]) is not bool:
            raise CollectorContinuityError("collector attempt process-result marker is invalid")
        if detail["process_result_known"]:
            if (
                type(detail["returncode"]) is not int
                or type(detail["stdout_bytes"]) is not int
                or type(detail["stderr_bytes"]) is not int
            ):
                raise CollectorContinuityError("collector attempt output metadata is invalid")
            if detail["stdout_bytes"] < 0 or detail["stderr_bytes"] < 0:
                raise CollectorContinuityError("collector attempt output bytes are invalid")
            _require_event_sha256(detail["stdout_sha256"], "stdout_sha256")
            _require_event_sha256(detail["stderr_sha256"], "stderr_sha256")
        elif any(
            detail[field] is not None
            for field in (
                "returncode", "stdout_sha256", "stdout_bytes", "stderr_sha256", "stderr_bytes"
            )
        ):
            raise CollectorContinuityError("collector unknown attempt result is invalid")
        _require_event_text(
            detail["completed_at"] if event_type.endswith("COMPLETED") else detail["failed_at"],
            "attempt terminal timestamp",
        )
        if type(detail["recovered"]) is not bool:
            raise CollectorContinuityError("collector attempt recovered is invalid")
        _require_event_text(detail["verifier_id"], "verifier_id")
        if detail["recovered"] and detail["verifier_id"] != _RAW_POSTCONDITION_SCHEMA:
            raise CollectorContinuityError("collector recovered verifier identity is invalid")
        if launch_state == "handle_obtained":
            if not detail["process_result_known"] or detail["recovered"]:
                raise CollectorContinuityError("collector handled attempt result is invalid")
        elif launch_state == "not_invoked":
            if detail["process_result_known"] or detail["recovered"]:
                raise CollectorContinuityError("collector unlaunched attempt result is invalid")
            if event_type != "ATTEMPT_FAILED" or detail["failure_classification"] != "child_launch_failed" or detail["retryable"] is not True:
                raise CollectorContinuityError("collector unlaunched attempt terminal is invalid")
        elif detail["process_result_known"] or not detail["recovered"]:
            raise CollectorContinuityError("collector indeterminate attempt result is invalid")
        if event_type.endswith("COMPLETED"):
            if launch_state == "not_invoked" or (
                detail["process_result_known"] and detail["returncode"] != 0
            ):
                raise CollectorContinuityError("collector completed attempt returncode is invalid")
        else:
            classification = detail["failure_classification"]
            if (
                not isinstance(classification, str)
                or classification not in _ATTEMPT_FAILURE_RETRYABILITY
                or type(detail["retryable"]) is not bool
                or detail["retryable"]
                != _ATTEMPT_FAILURE_RETRYABILITY[classification]
            ):
                raise CollectorContinuityError("collector attempt retryable is invalid")
            if classification == "child_launch_failed" and launch_state != "not_invoked":
                raise CollectorContinuityError("collector launch failure state is invalid")
        return
    raise CollectorContinuityError("collector ledger event type is invalid")


def _validate_recovery_start_detail(detail: Mapping[str, object]) -> None:
    require_exact_keys(detail, _RECOVERY_START_FIELDS, "collector recovery start")
    _validate_common_detail(detail)
    _require_event_text(detail["attempt_id"], "recovery attempt_id")
    _require_event_sha256(
        detail["attempt_started_event_sha256"], "recovery attempt start hash"
    )
    _require_event_sha256(detail["recovery_id"], "recovery id")
    if detail["recovery_kind"] != "hot_delete_journal":
        raise CollectorContinuityError("collector recovery kind is invalid")
    journal_identity = detail["journal_identity"]
    if not isinstance(journal_identity, Mapping):
        raise CollectorContinuityError("collector recovery journal identity is invalid")
    PhysicalFileIdentity.from_dict(journal_identity)
    if type(detail["journal_bytes"]) is not int or detail["journal_bytes"] < 0:
        raise CollectorContinuityError("collector recovery journal bytes are invalid")
    _require_event_sha256(detail["journal_sha256"], "recovery journal hash")
    _require_event_text(detail["started_at"], "recovery started_at")


def _validate_recovery_step_state(value: object) -> dict[str, object]:
    for allowed_tables in {identity[2] for identity in _STEP_IDENTITY.values()}:
        try:
            return _validate_step_state_shape(value, allowed_tables=allowed_tables)
        except CollectorContinuityError:
            continue
    raise CollectorContinuityError("collector recovery step state is invalid")


def _validate_recovery_terminal_detail(event_type: str, detail: Mapping[str, object]) -> None:
    fields = _RECOVERY_TERMINAL_FIELDS
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        fields |= {"completed_at", "recovery_classification"}
    else:
        fields |= {"failed_at", "failure_classification", "retryable"}
    require_exact_keys(detail, fields, "collector recovery terminal")
    _validate_recovery_start_detail(
        {field: detail[field] for field in _RECOVERY_START_FIELDS}
    )
    _require_event_sha256(
        detail["recovery_started_event_sha256"], "recovery start event hash"
    )
    _require_event_sha256(detail["state_after_sha256"], "state_after_sha256")
    if (
        _validate_recovery_step_state(detail["step_state_after"])[
            "collector_state_sha256"
        ]
        != detail["state_after_sha256"]
    ):
        raise CollectorContinuityError("collector recovery state_after hash is invalid")
    timestamp = detail["completed_at"] if event_type.endswith("COMPLETED") else detail["failed_at"]
    _require_event_text(timestamp, "recovery terminal timestamp")
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        if detail["recovery_classification"] != "hot_delete_journal_recovered":
            raise CollectorContinuityError("collector recovery classification is invalid")
    elif (
        detail["failure_classification"] != "rollback_journal_recovery_failed"
        or detail["retryable"] is not False
    ):
        raise CollectorContinuityError("collector recovery failure classification is invalid")


def _validate_common_detail(detail: Mapping[str, object]) -> None:
    _require_event_sha256(detail["registration_sha256"], "registration_sha256")
    _require_event_sha256(detail["database_uuid"], "database_uuid")
    _require_event_sha256(detail["state_before_sha256"], "state_before_sha256")


def _validate_registration_sessions(value: object) -> tuple[str, str, str]:
    if type(value) is not list or len(value) != 3:
        raise CollectorContinuityError("collector registration sessions are invalid")
    sessions: list[str] = []
    for session in value:
        sessions.append(_validate_collector_session(session))
    if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
        raise CollectorContinuityError("collector registration sessions are not strictly ordered")
    return sessions[0], sessions[1], sessions[2]


def _validate_collector_session(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or not value.isascii()
        or value[4] != "-"
        or value[7] != "-"
        or not (value[:4] + value[5:7] + value[8:]).isdigit()
    ):
        raise CollectorContinuityError("collector registration session is invalid")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise CollectorContinuityError("collector registration session is invalid")
    except ValueError as error:
        raise CollectorContinuityError("collector registration session is invalid") from error
    return value


def _validate_attempt_identity(detail: Mapping[str, object], *, require_nonce: bool) -> None:
    _validate_common_detail(detail)
    for key in ("session", "phase", "step_id", "attempt_id", "started_at"):
        _require_event_text(detail[key], key)
    if not _attempt_allowed_tables(detail):
        raise CollectorContinuityError("collector attempt step identity is invalid")
    _require_event_sha256(detail["command_sha256"], "command_sha256")
    if require_nonce:
        _require_event_sha256(detail["lease_nonce_sha256"], "lease_nonce_sha256")
    state_before = _validate_step_state_shape(
        detail["step_state_before"], allowed_tables=_attempt_allowed_tables(detail)
    )
    if state_before["collector_state_sha256"] != detail["state_before_sha256"]:
        raise CollectorContinuityError("collector attempt state_before hash is invalid")
    _validate_step_raw_before(
        detail["step_raw_before"], allowed_tables=_attempt_allowed_tables(detail)
    )


def _attempt_allowed_tables(detail: Mapping[str, object]) -> frozenset[str]:
    step_id = detail.get("step_id")
    phase = detail.get("phase")
    ordinal = detail.get("step_ordinal")
    if not isinstance(step_id, str) or type(ordinal) is not int:
        raise CollectorContinuityError("collector attempt step identity is invalid")
    expected = _STEP_IDENTITY.get(step_id)
    if (
        expected is None
        or phase != expected[0]
        or ordinal < 0
        or ordinal > 11
        or ordinal % 4 != expected[1]
    ):
        raise CollectorContinuityError("collector attempt step identity is invalid")
    return expected[2]


def _matching_detail(left: Mapping[str, object], right: Mapping[str, object], fields: frozenset[str]) -> bool:
    return all(left.get(field) == right.get(field) for field in fields)


def _validate_registered_attempt_session(
    detail: Mapping[str, object], sessions: tuple[str, str, str]
) -> None:
    ordinal = detail["step_ordinal"]
    if type(ordinal) is not int or detail["session"] != sessions[ordinal // 4]:
        raise CollectorContinuityError("collector attempt session is invalid")


def _validate_ledger_chain(events: Sequence[dict[str, object]]) -> None:
    if not events:
        raise CollectorContinuityError("collector ledger is empty")
    registration: dict[str, object] | None = None
    open_attempt: dict[str, object] | None = None
    open_attempt_event: dict[str, object] | None = None
    open_recovery: dict[str, object] | None = None
    open_recovery_event: dict[str, object] | None = None
    recovery_seen = False
    recovery_terminal_type: str | None = None
    recovery_terminal: dict[str, object] | None = None
    attempt_ids: set[str] = set()
    quarantined = False
    database_uuid: str | None = None
    sessions: tuple[str, str, str] | None = None
    previous: dict[str, object] | None = None
    for index, current in enumerate(events):
        current = validate_collector_ledger_event(current)
        details = current["event"]
        if not isinstance(details, dict):
            raise CollectorContinuityError("collector ledger detail is invalid")
        if current["seq"] != index:
            raise CollectorContinuityError("collector ledger sequence is invalid")
        if index == 0:
            if current["event_type"] != "GENESIS" or current["previous_event_sha256"] != ZERO_SHA256:
                raise CollectorContinuityError("collector ledger genesis is invalid")
            genesis = details.get("genesis")
            if not isinstance(genesis, dict):
                raise CollectorContinuityError("collector ledger genesis is invalid")
            database_uuid = _validate_prepared_genesis(genesis)["database_uuid"]
        else:
            if current["event_type"] == "GENESIS" or previous is None or current["previous_event_sha256"] != previous["event_sha256"]:
                raise CollectorContinuityError("collector ledger chain is invalid")
        event_type = str(current["event_type"])
        if index and registration is None and event_type != "REGISTRATION_BOUND":
            raise CollectorContinuityError("collector ledger registration is missing")
        if open_recovery is not None and event_type not in {"SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"}:
            raise CollectorContinuityError("collector recovery is already open")
        if event_type == "REGISTRATION_BOUND":
            if registration is not None or index != 1:
                raise CollectorContinuityError("collector registration is invalid")
            registration = details
            sessions = _validate_registration_sessions(details["sessions"])
        elif event_type == "SQLITE_RECOVERY_STARTED":
            if (
                open_recovery is not None
                or open_attempt is None
                or open_attempt_event is None
                or recovery_seen
            ):
                raise CollectorContinuityError("collector recovery is already open")
            if (
                registration is None
                or details["registration_sha256"] != registration["registration_sha256"]
                or details["database_uuid"] != database_uuid
                or not _matching_detail(
                    open_attempt,
                    details,
                    frozenset(
                        {
                            "registration_sha256",
                            "database_uuid",
                            "state_before_sha256",
                            "attempt_id",
                        }
                    ),
                )
                or details["attempt_started_event_sha256"]
                != open_attempt_event["event_sha256"]
            ):
                raise CollectorContinuityError("collector recovery registration is invalid")
            open_recovery = details
            open_recovery_event = current
            recovery_seen = True
        elif event_type in {"SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"}:
            if (
                open_recovery is None
                or open_recovery_event is None
                or open_attempt is None
                or not _matching_detail(
                    open_recovery, details, _RECOVERY_START_FIELDS
                )
                or details["recovery_started_event_sha256"]
                != open_recovery_event["event_sha256"]
            ):
                raise CollectorContinuityError("collector recovery terminal does not match start")
            allowed_tables = _attempt_allowed_tables(open_attempt)
            state_after = _validate_step_state_shape(
                details["step_state_after"], allowed_tables=allowed_tables
            )
            if state_after["collector_state_sha256"] != details["state_after_sha256"]:
                raise CollectorContinuityError("collector recovery terminal state is invalid")
            open_recovery = None
            open_recovery_event = None
            recovery_terminal_type = event_type
            recovery_terminal = details
        elif event_type == "ATTEMPT_STARTED":
            if (
                registration is None
                or open_attempt is not None
                or open_recovery is not None
                or quarantined
                or sessions is None
                or details["database_uuid"] != database_uuid
                or details["attempt_id"] in attempt_ids
            ):
                raise CollectorContinuityError("collector attempt start is invalid")
            if details["registration_sha256"] != registration["registration_sha256"]:
                raise CollectorContinuityError("collector attempt registration is invalid")
            _validate_registered_attempt_session(details, sessions)
            open_attempt = details
            open_attempt_event = current
            attempt_ids.add(str(details["attempt_id"]))
            recovery_seen = False
            recovery_terminal_type = None
            recovery_terminal = None
        elif event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
            identity_fields = frozenset({
                "registration_sha256", "database_uuid", "state_before_sha256", "session", "phase",
                "step_id", "step_ordinal", "attempt_id", "command_sha256", "started_at", "step_state_before",
                "step_raw_before",
            })
            if open_attempt is None or open_recovery is not None or not _matching_detail(open_attempt, details, identity_fields):
                raise CollectorContinuityError("collector attempt terminal does not match start")
            if details["database_uuid"] != database_uuid:
                raise CollectorContinuityError("collector attempt database UUID is invalid")
            if sessions is None:
                raise CollectorContinuityError("collector registration sessions are missing")
            _validate_registered_attempt_session(details, sessions)
            if recovery_terminal is not None:
                if (
                    details["state_after_sha256"]
                    != recovery_terminal["state_after_sha256"]
                    or details["step_state_after"]
                    != recovery_terminal["step_state_after"]
                ):
                    raise CollectorContinuityError(
                        "collector recovery and attempt state diverged"
                    )
                if recovery_terminal_type == "SQLITE_RECOVERY_FAILED" and (
                    event_type != "ATTEMPT_FAILED"
                    or details["failure_classification"]
                    != "rollback_journal_recovery_failed"
                    or details["retryable"] is not False
                ):
                    raise CollectorContinuityError(
                        "collector recovery failure terminal is invalid"
                    )
            if event_type == "ATTEMPT_FAILED" and details["retryable"] is False:
                quarantined = True
            open_attempt = None
            open_attempt_event = None
            recovery_seen = False
            recovery_terminal_type = None
            recovery_terminal = None
        previous = current
    if len(events) > 1 and registration is None:
        raise CollectorContinuityError("collector ledger registration is missing")


def append_collector_genesis_event(opened: object, event: Mapping[str, object]) -> dict[str, object]:
    require_collector_continuity_health()
    if not isinstance(opened, _OpenedRegularFile):
        raise CollectorContinuityError("collector ledger writer authority is invalid")
    candidate = validate_collector_ledger_event(event)
    if candidate["event_type"] != "GENESIS":
        raise CollectorContinuityError("collector ledger genesis event is invalid")
    opened.verify_identity()
    if os.fstat(opened.file_fd).st_size != 0:
        raise CollectorContinuityError("collector ledger genesis requires an empty file")
    payload = canonical_json_bytes(candidate) + b"\n"
    _append_verified_ledger_payload(opened, payload, (candidate,))
    return candidate["event_sha256"]


def append_collector_ledger_event(
    opened: object, *, event_type: str, event: Mapping[str, object]
) -> dict[str, object]:
    """Append only non-phase events through the ordinary ledger writer."""

    require_collector_continuity_health()
    if not isinstance(opened, _OpenedRegularFile):
        raise CollectorContinuityError("collector ledger writer authority is invalid")
    if event_type in {
        "ATTEMPT_STARTED",
        "ATTEMPT_COMPLETED",
        "ATTEMPT_FAILED",
        "SQLITE_RECOVERY_STARTED",
        "SQLITE_RECOVERY_COMPLETED",
        "SQLITE_RECOVERY_FAILED",
    }:
        raise CollectorContinuityError("collector phase event requires a held phase lease")
    return _append_collector_ledger_event_on_opened(
        opened, event_type=event_type, event=event
    )


def _append_collector_ledger_event_on_opened(
    opened: _OpenedRegularFile, *, event_type: str, event: Mapping[str, object]
) -> dict[str, object]:
    """The sole append-and-fsync path after authority has been established."""

    before_size = os.fstat(opened.file_fd).st_size
    history = parse_collector_ledger(opened)
    candidate = build_collector_ledger_event(
        previous_event=history[-1], event_type=event_type, event=event
    )
    complete = (*history, candidate)
    _validate_ledger_chain(complete)
    if os.fstat(opened.file_fd).st_size != before_size:
        raise CollectorContinuityError("collector ledger changed before append")
    opened.verify_identity()
    _append_verified_ledger_payload(opened, canonical_json_bytes(candidate) + b"\n", complete)
    return candidate


def _phase_ledger_history(lease: object) -> tuple[dict[str, object], ...]:
    """Reprove the live phase authority immediately before a phase append."""

    require_collector_continuity_health()
    if type(lease) is not CollectorPhaseLease:
        raise CollectorContinuityError("collector phase event lease is invalid")
    lease._verify_owner()
    identity = lease.verify()
    if _ACTIVE_COLLECTOR_PHASE_LEASES.get(identity) is not lease:
        raise CollectorContinuityError("collector phase event lease is not active")
    history = parse_collector_ledger(lease.ledger)
    if len(history) < 2 or history[1]["event_type"] != "REGISTRATION_BOUND":
        raise CollectorContinuityError("collector phase ledger is not registration-bound")
    binding = history[1]["event"]
    if not isinstance(binding, Mapping):
        raise CollectorContinuityError("collector phase ledger registration is invalid")
    _require_event_sha256(binding.get("registration_sha256"), "registration_sha256")
    return history


def _append_collector_phase_event(
    lease: CollectorPhaseLease, *, event_type: str, event: Mapping[str, object]
) -> dict[str, object]:
    """Append one attempt/recovery event through an already-held phase lease."""

    if event_type not in {
        "ATTEMPT_STARTED",
        "ATTEMPT_COMPLETED",
        "ATTEMPT_FAILED",
        "SQLITE_RECOVERY_STARTED",
        "SQLITE_RECOVERY_COMPLETED",
        "SQLITE_RECOVERY_FAILED",
    }:
        raise CollectorContinuityError("collector phase event type is invalid")
    if not isinstance(event, Mapping):
        raise CollectorContinuityError("collector phase event is invalid")
    history = _phase_ledger_history(lease)
    binding = history[1]["event"]
    if not isinstance(binding, Mapping) or event.get("registration_sha256") != binding.get(
        "registration_sha256"
    ):
        raise CollectorContinuityError("collector phase event registration drifted")
    tail_type = history[-1]["event_type"]
    if event_type == "ATTEMPT_STARTED":
        if tail_type not in {"REGISTRATION_BOUND", "ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
            raise CollectorContinuityError("collector phase ledger has a dangling event")
    elif event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
        if tail_type not in {
            "ATTEMPT_STARTED",
            "SQLITE_RECOVERY_COMPLETED",
            "SQLITE_RECOVERY_FAILED",
        }:
            raise CollectorContinuityError("collector phase terminal has no active attempt")
        if tail_type == "SQLITE_RECOVERY_FAILED" and event_type != "ATTEMPT_FAILED":
            raise CollectorContinuityError("collector recovery failure cannot complete an attempt")
    elif event_type == "SQLITE_RECOVERY_STARTED" and tail_type != "ATTEMPT_STARTED":
        raise CollectorContinuityError("collector recovery has no active attempt")
    elif event_type in {"SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"}:
        if tail_type != "SQLITE_RECOVERY_STARTED":
            raise CollectorContinuityError("collector recovery terminal has no active recovery")
    return _append_collector_ledger_event_on_opened(
        lease.ledger, event_type=event_type, event=event
    )


def _append_verified_ledger_payload(
    opened: _OpenedRegularFile, payload: bytes, complete: Sequence[dict[str, object]]
) -> None:
    if len(payload) - 1 > COLLECTOR_LEDGER_MAX_LINE_BYTES:
        raise CollectorContinuityError("collector ledger line limit would be exceeded")
    original_size = os.fstat(opened.file_fd).st_size
    total_bytes = original_size + len(payload)
    if total_bytes > COLLECTOR_LEDGER_MAX_BYTES or len(complete) > COLLECTOR_LEDGER_MAX_LINES:
        raise CollectorContinuityError("collector ledger limit would be exceeded")
    opened.verify_identity()
    if os.fstat(opened.file_fd).st_size != original_size:
        raise CollectorContinuityError("collector ledger changed before write")
    try:
        os.lseek(opened.file_fd, 0, os.SEEK_END)
        _write_all(opened.file_fd, payload, label="collector ledger")
        os.fsync(opened.file_fd)
        opened.verify_identity()
    except (CollectorContinuityError, OSError) as exc:
        try:
            os.ftruncate(opened.file_fd, original_size)
            os.fsync(opened.file_fd)
        except OSError as rollback_exc:
            raise CollectorContinuityError("collector ledger append rollback failed") from rollback_exc
        if isinstance(exc, CollectorContinuityError):
            raise
        raise CollectorContinuityError("collector ledger append failed") from exc


def _verify_lease_fd_identity(
    descriptor: int, expected_ledger_identity: PhysicalFileIdentity
) -> PhysicalFileIdentity:
    """Bind an inherited descriptor to the current no-follow ledger path."""

    if type(descriptor) is not int or descriptor < 3:
        raise CollectorContinuityError("collector lease descriptor is invalid")
    if not isinstance(expected_ledger_identity, PhysicalFileIdentity):
        raise CollectorContinuityError("collector lease identity is invalid")
    try:
        status = os.fstat(descriptor)
    except OSError as exc:
        raise CollectorContinuityError("collector lease descriptor is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or (
        int(status.st_dev),
        int(status.st_ino),
    ) != (
        expected_ledger_identity.file_st_dev,
        expected_ledger_identity.file_st_ino,
    ):
        raise CollectorContinuityError("collector lease descriptor identity changed")
    opened = open_existing_regular_file(expected_ledger_identity.canonical_path)
    try:
        if opened.identity != expected_ledger_identity:
            raise CollectorContinuityError("collector lease path identity changed")
        return opened.verify_identity()
    finally:
        opened.close()


def _child_observes_collector_lock(
    descriptor: int, expected_ledger_identity: PhysicalFileIdentity
) -> bool:
    """Check contention from a process without an inherited lease descriptor."""

    try:
        child_pid = os.fork()
    except OSError as exc:
        raise CollectorContinuityError("collector lease lock cannot be probed") from exc
    if child_pid == 0:
        try:
            descriptors = {descriptor}
            for lease in _ACTIVE_COLLECTOR_PHASE_LEASES.values():
                descriptors.add(lease.ledger.file_fd)
                descriptors.update(lease._handoff_fds)
            for inherited in descriptors:
                if inherited >= 3:
                    try:
                        os.close(inherited)
                    except OSError:
                        pass
            opened = open_existing_regular_file(expected_ledger_identity.canonical_path)
            try:
                if opened.identity != expected_ledger_identity:
                    os._exit(2)
                try:
                    fcntl.flock(opened.file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os._exit(1)
                fcntl.flock(opened.file_fd, fcntl.LOCK_UN)
                os._exit(0)
            finally:
                opened.close()
        except BaseException:
            os._exit(2)
    try:
        _, status = os.waitpid(child_pid, 0)
    except OSError as exc:
        raise CollectorContinuityError("collector lease lock probe failed") from exc
    if not os.WIFEXITED(status):
        raise CollectorContinuityError("collector lease lock probe did not exit")
    result = os.WEXITSTATUS(status)
    if result == 1:
        return True
    if result == 0:
        return False
    raise CollectorContinuityError("collector lease lock probe is invalid")


def acquire_collector_phase_lease(ledger_path: str | os.PathLike[str]) -> CollectorPhaseLease:
    """Acquire one non-blocking exclusive, identity-bound collector ledger lease."""

    require_collector_continuity_health()
    opened = open_existing_regular_file(ledger_path)
    locked = False
    try:
        identity = opened.verify_identity()
        if identity in _ACTIVE_COLLECTOR_PHASE_LEASES:
            raise CollectorContinuityError("collector phase lease is already active")
        try:
            fcntl.flock(opened.file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise CollectorContinuityError("collector phase ledger is busy") from exc
        os.set_inheritable(opened.file_fd, False)
        opened.verify_identity()
        lease = CollectorPhaseLease(
            opened,
            os.getpid(),
            owner_thread_id=threading.get_ident(),
        )
        _ACTIVE_COLLECTOR_PHASE_LEASES[identity] = lease
        return lease
    except Exception:
        if locked:
            try:
                fcntl.flock(opened.file_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        opened.close()
        raise


def verify_locked_collector_lease(
    descriptor: int, *, expected_ledger_identity: PhysicalFileIdentity
) -> PhysicalFileIdentity:
    """Prove a descriptor is identity-bound and currently shares an exclusive flock.

    This remains a lock primitive only.  It does not establish an attempt,
    writer, registration, or release authority.
    """

    require_collector_continuity_health()
    identity = _verify_lease_fd_identity(descriptor, expected_ledger_identity)
    if not _child_observes_collector_lock(descriptor, identity):
        raise CollectorContinuityError("collector lease descriptor is not locked")
    try:
        # On platforms with flock locks associated with open file descriptions,
        # this succeeds only for the locked descriptor (or a true duplicate).
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise CollectorContinuityError("collector lease descriptor is not exclusively locked") from exc
    _verify_lease_fd_identity(descriptor, identity)
    return identity


def verify_locked_collector_phase_lease(
    lease_or_descriptor: CollectorPhaseLease | int,
    *,
    expected_ledger_identity: PhysicalFileIdentity | None = None,
) -> PhysicalFileIdentity:
    """Validate either a parent phase lease or a child inherited descriptor."""

    require_collector_continuity_health()
    if isinstance(lease_or_descriptor, CollectorPhaseLease):
        if expected_ledger_identity is not None:
            raise CollectorContinuityError("collector phase lease identity must not be supplied twice")
        return lease_or_descriptor.verify()
    if expected_ledger_identity is None:
        raise CollectorContinuityError("collector phase lease identity is required")
    return verify_locked_collector_lease(
        lease_or_descriptor,
        expected_ledger_identity=expected_ledger_identity,
    )


def validate_collector_step_state(value: Mapping[str, object], *, allowed_tables: frozenset[str]) -> dict[str, object]:
    if (
        not isinstance(allowed_tables, frozenset)
        or allowed_tables not in {identity[2] for identity in _STEP_IDENTITY.values()}
    ):
        raise CollectorContinuityError("collector step state allowed tables are invalid")
    expected = {
        "schema_version", "collector_state_sha256", "table_counts", "table_sha256",
        "outside_scope_sha256", "receipt_id_high_water",
    }
    require_exact_keys(value, expected, "collector step state")
    if value["schema_version"] != COLLECTOR_STEP_STATE_SCHEMA:
        raise CollectorContinuityError("collector step state schema is invalid")
    _require_event_sha256(value["collector_state_sha256"], "step state hash")
    counts = value["table_counts"]
    digests = value["table_sha256"]
    outside = value["outside_scope_sha256"]
    tables = set(COLLECTOR_STATE_TABLES)
    if (
        not isinstance(counts, Mapping) or not isinstance(digests, Mapping) or not isinstance(outside, Mapping)
        or set(counts) != tables or set(digests) != tables or set(outside) != set(allowed_tables)
        or not set(allowed_tables).issubset(tables)
    ):
        raise CollectorContinuityError("collector step state table scope is invalid")
    for table in tables:
        if type(counts[table]) is not int or counts[table] < 0:
            raise CollectorContinuityError("collector step state table count is invalid")
        _require_event_sha256(digests[table], "step state table hash")
    for digest in outside.values():
        _require_event_sha256(digest, "step state outside scope hash")
    if type(value["receipt_id_high_water"]) is not int or value["receipt_id_high_water"] < 0:
        raise CollectorContinuityError("collector step state receipt high water is invalid")
    return decode_canonical_json_object(canonical_json_bytes(dict(value)))


_REGISTRATION_V4_FIELDS: Final = frozenset(
    {
        "schema_version", "registered_at", "as_of", "symbols", "sessions", "source",
        "adjustment_mode", "adjustment_version", "database_path", "panel_sha256",
        "workspace_count", "outcome_feedback_used", "status", "prerequisite_files",
        "prerequisites", "prerequisites_sha256",
    }
)
_REGISTRATION_V4_SCHEMA: Final = "rqgm-forward-panel-registration/4"


def _read_registered_schedule_authority(
    registration_file: str | os.PathLike[str],
) -> dict[str, object]:
    """Rebuild the registered schedule authority without opening SQLite."""

    path = lexical_absolute_path(registration_file)
    opened = open_nofollow_regular(path)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise CollectorContinuityError("collector registration size is invalid")
        raw = os.pread(opened.descriptor, size, 0)
        if len(raw) != size:
            raise CollectorContinuityError("collector registration was truncated while reading")
        verify_file_identity(path, opened.identity)
    except OSError as exc:
        raise CollectorContinuityError("collector registration cannot be read") from exc
    finally:
        opened.close()
    authority = _decode_registered_schedule_authority(raw)
    ledger = _parse_retained_bound_collector_ledger(
        str(authority["ledger_path"]),
        authority["ledger_identity"],
    )
    _validate_registered_schedule_ledger(authority, ledger)
    return {**authority, "registration_file": path}


def _decode_registered_schedule_authority(raw: bytes) -> dict[str, object]:
    """Decode schedule authority from canonical registration bytes only."""

    registration = decode_canonical_json_object(raw)
    require_exact_keys(registration, _REGISTRATION_V4_FIELDS, "collector registration")
    if registration["schema_version"] != _REGISTRATION_V4_SCHEMA:
        raise CollectorContinuityError("collector registration schema is unsupported")
    registration_sha256 = hashlib.sha256(raw).hexdigest()
    _require_event_sha256(registration["panel_sha256"], "registration panel_sha256")
    _require_event_sha256(registration["prerequisites_sha256"], "registration prerequisites_sha256")
    if (
        registration["adjustment_mode"] != "raw"
        or registration["source"] != "tencent"
        or registration["workspace_count"] != 36
        or registration["outcome_feedback_used"] is not False
        or registration["status"] != "AWAITING_FULL_SNAPSHOT_READINESS"
        or not isinstance(registration["adjustment_version"], str)
        or not registration["adjustment_version"]
        or not isinstance(registration["prerequisites"], Mapping)
        or registration["prerequisites_sha256"]
        != canonical_json_sha256(dict(registration["prerequisites"]))
    ):
        raise CollectorContinuityError("collector registration is invalid")
    try:
        registered_at = datetime.fromisoformat(str(registration["registered_at"]))
    except ValueError as exc:
        raise CollectorContinuityError("collector registration timestamp is invalid") from exc
    if (
        registered_at.tzinfo is None
        or registered_at.utcoffset() is None
        or registered_at.isoformat() != registration["registered_at"]
    ):
        raise CollectorContinuityError("collector registration timestamp is invalid")
    registered_local = registered_at.astimezone(_SHANGHAI)
    if registration["as_of"] != registered_local.date().isoformat():
        raise CollectorContinuityError("collector registration timestamp is invalid")
    sessions = _validate_registration_sessions(registration["sessions"])
    if any(
        session <= registered_local.date().isoformat()
        or date.fromisoformat(session).weekday() >= 5
        for session in sessions
    ):
        raise CollectorContinuityError("collector registration is not prospective")
    symbols_value = registration["symbols"]
    if (
        not isinstance(symbols_value, list)
        or len(symbols_value) != 12
        or any(not isinstance(symbol, str) or not symbol for symbol in symbols_value)
        or symbols_value != sorted(symbols_value)
        or len(set(symbols_value)) != 12
    ):
        raise CollectorContinuityError("collector registration symbols are invalid")
    symbols = tuple(symbols_value)
    panel = tuple(sorted(f"{symbol}@{session}" for symbol in symbols for session in sessions))
    if registration["panel_sha256"] != canonical_json_sha256(list(panel)):
        raise CollectorContinuityError("collector registration panel is invalid")
    prerequisites = registration["prerequisites"]
    collector = prerequisites.get("collector")
    if not isinstance(collector, Mapping):
        raise CollectorContinuityError("collector registration capability is invalid")
    capability = decode_capability(canonical_json_bytes(dict(collector)))
    database = lexical_absolute_path(registration["database_path"])
    if (
        capability["database_path"] != database
        or capability["source"] != registration["source"]
        or capability["adjustment_mode"] != registration["adjustment_mode"]
        or capability["adjustment_version"] != registration["adjustment_version"]
    ):
        raise CollectorContinuityError("collector registration capability drifted")
    database_identity = PhysicalFileIdentity.from_dict(capability["database_identity"])
    ledger_identity = PhysicalFileIdentity.from_dict(capability["ledger_identity"])
    ledger_path = lexical_absolute_path(capability["ledger_path"])
    if (
        database_identity.canonical_path != database
        or ledger_identity.canonical_path != ledger_path
    ):
        raise CollectorContinuityError("collector registration capability drifted")
    return {
        "registration_sha256": registration_sha256,
        "sessions": sessions,
        "symbols": symbols,
        "cohort_start": sessions[0],
        "source": registration["source"],
        "adjustment_mode": registration["adjustment_mode"],
        "adjustment_version": registration["adjustment_version"],
        "database_path": database,
        "ledger_path": ledger_path,
        "ledger_identity": ledger_identity,
        "capability": capability,
        "registration": registration,
        "registered_at": registered_at,
    }


def _validate_registered_schedule_ledger(
    authority: Mapping[str, object],
    ledger: Sequence[dict[str, object]],
) -> None:
    """Bind a parsed complete ledger to decoded registration authority."""

    if len(ledger) < 2 or ledger[1]["event_type"] != "REGISTRATION_BOUND":
        raise CollectorContinuityError("collector registration is not ledger-bound")
    capability = authority.get("capability")
    registration = authority.get("registration")
    sessions = authority.get("sessions")
    if (
        not isinstance(capability, Mapping)
        or not isinstance(registration, Mapping)
        or not isinstance(sessions, tuple)
    ):
        raise CollectorContinuityError("collector registration authority is invalid")
    genesis_event = ledger[0]
    genesis_detail = genesis_event.get("event")
    genesis = genesis_detail.get("genesis") if isinstance(genesis_detail, Mapping) else None
    if (
        not isinstance(genesis, Mapping)
        or genesis_event.get("event_sha256")
        != capability["ledger_genesis_event_sha256"]
        or canonical_json_sha256(dict(genesis)) != capability["genesis_sha256"]
        or genesis.get("database_uuid") != capability["database_uuid"]
        or genesis.get("cohort_sha256") != capability["cohort_sha256"]
        or genesis.get("database_identity") != capability["database_identity"]
        or genesis.get("ledger_identity") != capability["ledger_identity"]
        or genesis.get("collector_schema_sha256")
        != capability["collector_schema_sha256"]
    ):
        raise CollectorContinuityError("collector registration genesis binding drifted")
    binding = ledger[1]["event"]
    if not isinstance(binding, Mapping):
        raise CollectorContinuityError("collector registration binding is invalid")
    expected_binding = {
        "registration_sha256": authority["registration_sha256"],
        "panel_sha256": registration["panel_sha256"],
        "sessions": list(sessions),
        "sessions_sha256": canonical_json_sha256(list(sessions)),
        "prerequisites_sha256": registration["prerequisites_sha256"],
    }
    if any(binding.get(field) != value for field, value in expected_binding.items()):
        raise CollectorContinuityError("collector registration binding drifted")
    try:
        bound_at = datetime.fromisoformat(str(binding.get("bound_at")))
    except ValueError as exc:
        raise CollectorContinuityError("collector registration binding is invalid") from exc
    if bound_at.tzinfo is None or bound_at.utcoffset() is None:
        raise CollectorContinuityError("collector registration binding is invalid")


def _read_bound_registration(registration_file: str | os.PathLike[str]) -> dict[str, object]:
    """Load one canonical `/4` registration and reverify its SQLite binding."""

    authority = _read_registered_schedule_authority(registration_file)
    capability = authority["capability"]
    if not isinstance(capability, Mapping):
        raise CollectorContinuityError("collector registration capability is invalid")
    prepared = load_verified_prepared_collector(
        database_path=authority["database_path"],
        ledger_path=authority["ledger_path"],
    )
    for field_name in (
        "database_path", "ledger_path", "database_identity", "ledger_identity",
        "database_uuid", "cohort_sha256", "genesis_sha256",
        "ledger_genesis_event_sha256", "collector_schema_sha256",
    ):
        if capability[field_name] != prepared[field_name]:
            raise CollectorContinuityError("collector registration capability drifted")
    return {**authority, "prepared": prepared}


def _build_collector_step_schedule(
    authority: Mapping[str, object], *, registration_file: str
) -> _FrozenCollectorStepSchedule:
    """Build the deterministic 12-step schedule from decoded authority."""

    registration_sha256 = str(authority["registration_sha256"])
    normalized_sessions = authority["sessions"]
    symbols = authority["symbols"]
    source = str(authority["source"])
    adjustment_mode = str(authority["adjustment_mode"])
    adjustment_version = str(authority["adjustment_version"])
    database = str(authority["database_path"])
    if not isinstance(normalized_sessions, tuple) or not isinstance(symbols, tuple):
        raise CollectorContinuityError("collector registration authority is invalid")
    panel = tuple(sorted(f"{symbol}@{session}" for symbol in symbols for session in normalized_sessions))
    executable = os.path.realpath(sys.executable)

    identity = (
        ("pre_open_context", "pre_open", "sina-market-center-hs-a-v1"),
        ("pre_open_corporate_actions", "pre_open", "baostock-query-dividend-data-v1"),
        ("post_close_context", "post_close", "sina-market-center-hs-a-v1"),
        ("post_close_prices", "post_close", source),
    )
    schedule_sha256 = canonical_json_sha256(
        {
            "schema_version": "stockdata-forward-collector-step-schedule/1",
            "registration_file": registration_file,
            "registration_sha256": registration_sha256,
            "sessions": list(normalized_sessions),
            "panel": list(panel),
            "database_path": database,
            "source": source,
            "adjustment_mode": adjustment_mode,
            "adjustment_version": adjustment_version,
            "python_executable": executable,
        }
    )
    schedule: list[CollectorStepSpec] = []
    base = (executable, "-m", "stockdata.cli")
    for session_index, session in enumerate(normalized_sessions):
        for local_ordinal, (step_id, phase, selector_source) in enumerate(identity):
            if step_id == "pre_open_context" or step_id == "post_close_context":
                command = base + ("forward-context-capture", "--database", database, "--date", session)
            elif step_id == "pre_open_corporate_actions":
                command = base + (
                    "forward-corporate-actions-capture", "--database", database, "--date", session
                )
            else:
                command = base + (
                    "forward-capture", "--database", database, "--codes", ",".join(symbols),
                    "--start", normalized_sessions[0], "--end", session, "--source", source,
                    "--adjustment-version", adjustment_version,
                )
            schedule.append(
                CollectorStepSpec(
                    registration_file=registration_file,
                    registration_sha256=registration_sha256,
                    session=session,
                    phase=phase,
                    step_id=step_id,
                    step_ordinal=session_index * 4 + local_ordinal,
                    allowed_tables=_STEP_IDENTITY[step_id][2],
                    selector_source=selector_source,
                    database_path=database,
                    symbols=symbols,
                    command=command,
                    command_sha256=canonical_json_sha256(
                        {"schema_version": "stockdata-forward-collector-command/1", "argv": list(command)}
                    ),
                    schedule_sha256=schedule_sha256,
                )
            )
    ledger_identity = authority.get("ledger_identity")
    if not isinstance(ledger_identity, PhysicalFileIdentity):
        raise CollectorContinuityError("collector registration ledger identity is invalid")
    return _FrozenCollectorStepSchedule(
        registration_file=registration_file,
        registration_sha256=registration_sha256,
        sessions=normalized_sessions,
        cohort_start=str(authority["cohort_start"]),
        source=source,
        adjustment_mode=adjustment_mode,
        adjustment_version=adjustment_version,
        database_path=database,
        ledger_path=str(authority["ledger_path"]),
        ledger_identity=ledger_identity,
        specs=tuple(schedule),
    )


def freeze_collector_step_schedule(
    *, registration_file: str | os.PathLike[str]
) -> tuple[CollectorStepSpec, ...]:
    """Freeze the 12 commands from one persistent, ledger-bound registration."""

    require_collector_continuity_health()
    authority = _read_registered_schedule_authority(registration_file)
    frozen_schedule = _build_collector_step_schedule(
        authority, registration_file=str(authority["registration_file"])
    )
    frozen_specs = frozen_schedule.specs
    schedule_sha256 = frozen_specs[0].schedule_sha256
    existing = _FROZEN_COLLECTOR_STEP_SCHEDULES.get(schedule_sha256)
    if existing is not None and existing != frozen_schedule:
        raise CollectorContinuityError("collector frozen schedule authority conflicts")
    _FROZEN_COLLECTOR_STEP_SCHEDULES[schedule_sha256] = frozen_schedule
    return frozen_specs


def _validate_collector_step_spec(spec: CollectorStepSpec) -> _FrozenCollectorStepSchedule:
    if not isinstance(spec, CollectorStepSpec):
        raise CollectorContinuityError("collector step specification is invalid")
    _require_event_sha256(spec.schedule_sha256, "collector step schedule_sha256")
    refreshed = freeze_collector_step_schedule(registration_file=spec.registration_file)
    if spec.step_ordinal < 0 or spec.step_ordinal >= len(refreshed) or refreshed[spec.step_ordinal] != spec:
        raise CollectorContinuityError("collector step specification is not frozen")
    frozen_schedule = _FROZEN_COLLECTOR_STEP_SCHEDULES.get(spec.schedule_sha256)
    if frozen_schedule is None:
        raise CollectorContinuityError("collector step specification is not frozen")
    expected = _STEP_IDENTITY.get(spec.step_id)
    if (
        expected is None
        or spec.phase != expected[0]
        or spec.allowed_tables != expected[2]
        or spec.selector_source != (
            "tencent" if spec.step_id == "post_close_prices" else (
                "sina-market-center-hs-a-v1" if spec.step_id.endswith("context")
                else "baostock-query-dividend-data-v1"
            )
        )
        or type(spec.step_ordinal) is not int
        or spec.step_ordinal < 0
        or spec.step_ordinal > 11
        or spec.step_ordinal % 4 != expected[1]
        or not os.path.isabs(spec.database_path)
        or tuple(sorted(spec.symbols)) != spec.symbols
        or not spec.symbols
        or not all(isinstance(symbol, str) and symbol for symbol in spec.symbols)
        or not isinstance(spec.command, tuple)
        or not all(isinstance(argument, str) for argument in spec.command)
        or not spec.command
        or spec.command[0] != os.path.realpath(sys.executable)
        or spec.command_sha256 != canonical_json_sha256(
            {"schema_version": "stockdata-forward-collector-command/1", "argv": list(spec.command)}
        )
        or spec.step_ordinal >= len(frozen_schedule.specs)
        or frozen_schedule.specs[spec.step_ordinal] != spec
    ):
        raise CollectorContinuityError("collector step specification is invalid")
    _validate_collector_session(spec.session)
    return frozen_schedule


_STEP_TABLE_DOMAIN: Final = b"stockdata-forward-collector-step-state/1\x00table_sha256\x00"
_STEP_OUTSIDE_DOMAIN: Final = b"stockdata-forward-collector-step-state/1\x00outside_scope_sha256\x00"


def _encoded_cell_value(value: list[str]) -> object:
    if value == ["null"]:
        return None
    if value[0] == "integer":
        return int(value[1])
    if value[0] == "real":
        return float.fromhex(value[1])
    return value[1]


def _read_collector_step_records(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[dict[str, object], list[str]], ...]]:
    tables: dict[str, tuple[tuple[dict[str, object], list[str]], ...]] = {}
    for table, columns, primary_key in _COLLECTOR_LOGICAL_STATE_TABLES:
        select_items = ", ".join(
            f'typeof("{column}"), "{column}" + 0, CAST("{column}" AS BLOB)'
            for column in columns
        )
        ordering = ", ".join(f'"{column}" COLLATE BINARY ASC' for column in primary_key)
        records: list[tuple[dict[str, object], list[str]]] = []
        for row in connection.execute(
            f'SELECT {select_items} FROM main."{table}" ORDER BY {ordering}'
        ):
            cells = [
                _encode_collector_logical_cell(row[index * 3], row[index * 3 + 1], row[index * 3 + 2])
                for index in range(len(columns))
            ]
            records.append((dict(zip(columns, (_encoded_cell_value(cell) for cell in cells))), cells))
        tables[table] = tuple(records)
    return tables


def _step_row_selected(
    table: str,
    row: Mapping[str, object],
    spec: CollectorStepSpec,
    frozen_schedule: _FrozenCollectorStepSchedule,
) -> bool:
    source = row.get("source")
    if source != spec.selector_source:
        return False
    symbols = set(spec.symbols)
    if table == "daily":
        return (
            row.get("code") in symbols and row.get("date") == spec.session
            and row.get("adjustment_mode") == frozen_schedule.adjustment_mode
            and row.get("adjustment_version") == frozen_schedule.adjustment_version
            and row.get("is_final") == 1
            and row.get("receipt_id") is not None
        )
    if table == "sync_coverage":
        return (
            row.get("code") in symbols
            and row.get("adjustment_mode") == frozen_schedule.adjustment_mode
            and row.get("adjustment_version") == frozen_schedule.adjustment_version
            and row.get("start_date") == frozen_schedule.cohort_start
            and isinstance(row.get("end_date"), str)
            and frozen_schedule.cohort_start <= row["end_date"] <= spec.session
        )
    if table == "forward_context_observations":
        return row.get("effective_date") == spec.session and row.get("observation_phase") == spec.phase
    if table == "forward_universe_observations":
        return (
            row.get("effective_date") == spec.session
            and row.get("observation_phase") == spec.phase
        )
    if table == "forward_status_observations":
        return (
            row.get("effective_date") == spec.session and row.get("observation_phase") == spec.phase
            and row.get("symbol") in symbols
        )
    if table in {"forward_corporate_action_coverage", "forward_corporate_actions"}:
        return row.get("observation_date") == spec.session and row.get("symbol") in symbols
    return False


def _hash_step_records(
    domain: bytes, table: str, records: Sequence[tuple[dict[str, object], list[str]]], *, kind: str
) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    _hash_collector_logical_record(digest, {"kind": kind, "table": table})
    for _, values in records:
        _hash_collector_logical_record(digest, {"kind": "row", "values": values})
    return digest.hexdigest()


def _snapshot_collector_step_state_from_connection(
    connection: sqlite3.Connection, spec: CollectorStepSpec
) -> dict[str, object]:
    """Capture a point-in-time state from an already borrowed private connection."""

    frozen_schedule = _validate_raw_step_spec(spec)
    return _snapshot_collector_step_state_for_schedule(
        connection, spec, frozen_schedule
    )


def _snapshot_collector_step_state_for_schedule(
    connection: sqlite3.Connection,
    spec: CollectorStepSpec,
    frozen_schedule: _FrozenCollectorStepSchedule,
) -> dict[str, object]:
    """Capture state using an already decoded, path-independent schedule."""

    if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
        raise CollectorContinuityError("collector step snapshot requires no active transaction")
    if spec not in frozen_schedule.specs:
        raise CollectorContinuityError("collector step snapshot schedule is invalid")
    original_text_factory = connection.text_factory
    original_row_factory = connection.row_factory
    factories_restored = False
    began = False
    try:
        connection.text_factory = bytes
        connection.row_factory = None
        connection.execute("BEGIN")
        began = True
        if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise CollectorContinuityError("collector step snapshot foreign key check failed")
        aggregate, counts = _compute_collector_logical_digest(connection)
        records = _read_collector_step_records(connection)
        table_sha256 = {
            table: _hash_step_records(_STEP_TABLE_DOMAIN + table.encode("ascii") + b"\x00", table, rows, kind="table")
            for table, rows in records.items()
        }
        receipt_sources = {
            row["receipt_id"]: row["source"]
            for row, _ in records["collection_receipts"]
            if type(row.get("receipt_id")) is int and isinstance(row.get("source"), str)
        }
        references: dict[int, list[bool]] = {}
        for table, rows in records.items():
            if table == "collection_receipts":
                continue
            for row, _ in rows:
                receipt_id = row.get("receipt_id")
                if receipt_id is not None:
                    if type(receipt_id) is not int or receipt_id not in receipt_sources:
                        raise CollectorContinuityError("collector step snapshot receipt reference is invalid")
                    references.setdefault(receipt_id, []).append(
                        _step_row_selected(table, row, spec, frozen_schedule)
                    )

        def in_scope(table: str, row: Mapping[str, object]) -> bool:
            if table == "collection_receipts":
                receipt_id = row.get("receipt_id")
                linked = references.get(receipt_id) if type(receipt_id) is int else None
                return row.get("source") == spec.selector_source and bool(linked) and all(linked)
            return _step_row_selected(table, row, spec, frozen_schedule)

        outside_scope = {
            table: _hash_step_records(
                _STEP_OUTSIDE_DOMAIN + table.encode("ascii") + b"\x00",
                table,
                tuple(record for record in records[table] if not in_scope(table, record[0])),
                kind="outside_scope",
            )
            for table in spec.allowed_tables
        }
        high_water_row = connection.execute("SELECT COALESCE(MAX(receipt_id),0) FROM main.collection_receipts").fetchone()
        if high_water_row is None or type(high_water_row[0]) is not int or high_water_row[0] < 0:
            raise CollectorContinuityError("collector step snapshot receipt high water is invalid")
        connection.rollback()
        began = False
        state = validate_collector_step_state(
            {
                "schema_version": COLLECTOR_STEP_STATE_SCHEMA,
                "collector_state_sha256": aggregate.hexdigest(),
                "table_counts": counts,
                "table_sha256": table_sha256,
                "outside_scope_sha256": outside_scope,
                "receipt_id_high_water": high_water_row[0],
            },
            allowed_tables=spec.allowed_tables,
        )
        connection.row_factory = original_row_factory
        connection.text_factory = original_text_factory
        factories_restored = True
        return state
    except CollectorContinuityError:
        raise
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        raise CollectorContinuityError("collector step snapshot cannot be computed") from exc
    finally:
        cleanup_error: BaseException | None = None
        if began or connection.in_transaction:
            try:
                connection.rollback()
            except sqlite3.Error as exc:
                cleanup_error = exc
            else:
                if connection.in_transaction:
                    cleanup_error = CollectorContinuityError("collector step snapshot transaction did not close")
        if not factories_restored:
            try:
                connection.row_factory = original_row_factory
                connection.text_factory = original_text_factory
            except (AttributeError, TypeError) as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise CollectorContinuityError("collector step snapshot connection restoration failed") from cleanup_error


def snapshot_collector_step_state(
    token: CollectorReadToken, spec: CollectorStepSpec
) -> dict[str, object]:
    """Capture step state through an opaque, registered read token only."""

    require_collector_continuity_health()
    with _borrow_registered_collector_read_connection(token, spec) as connection:
        return _snapshot_collector_step_state_from_connection(connection, spec)


# Raw postcondition verification ------------------------------------------------

_RAW_POSTCONDITION_SCHEMA: Final = "stockdata-forward-collector-raw-postcondition/1"
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_PREOPEN_START: Final = time(8, 30)
_PREOPEN_END: Final = time(9, 25)
_POST_CLOSE_START: Final = time(15, 0)

@dataclass(frozen=True)
class CollectorStepBaseline:
    """One immutable before-image for a single frozen collector step."""

    step_state: dict[str, object]
    selector_rows: dict[str, tuple[dict[str, object], ...]]
    _prior_receipt_rows: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _rehydrated: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True)
class CollectorRawPostconditionResult:
    """Read-only classification of one collector subprocess's persisted output."""

    verifier_id: str
    raw_class: Literal["complete", "unchanged", "partial_prices", "forbidden"]
    code: str
    retryable: bool
    step_state_after: dict[str, object]
    new_receipt_ids: tuple[int, ...]
    request_sha256: tuple[str, ...]
    response_sha256: tuple[str, ...]
    verified_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]


def _collector_table_columns(table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    for candidate, columns, primary_key in _COLLECTOR_LOGICAL_STATE_TABLES:
        if candidate == table:
            return columns, primary_key
    raise CollectorContinuityError("collector raw-before table is invalid")


def _validate_step_raw_before(
    value: object, *, allowed_tables: frozenset[str]
) -> dict[str, object]:
    """Validate the canonical, primary-key-ordered raw before-image wire form."""

    if not isinstance(value, Mapping):
        raise CollectorContinuityError("collector step raw before-image is invalid")
    require_exact_keys(
        value,
        {"schema_version", "selector_rows"},
        "collector step raw before-image",
    )
    if value["schema_version"] != COLLECTOR_STEP_RAW_BEFORE_SCHEMA:
        raise CollectorContinuityError("collector step raw before-image schema is unsupported")
    rows_by_table = value["selector_rows"]
    if not isinstance(rows_by_table, Mapping):
        raise CollectorContinuityError("collector step raw before-image rows are invalid")
    require_exact_keys(
        rows_by_table,
        set(allowed_tables),
        "collector step raw before-image tables",
    )
    normalized: dict[str, object] = {}
    for table in sorted(allowed_tables):
        rows = rows_by_table[table]
        if not isinstance(rows, list):
            raise CollectorContinuityError("collector step raw before-image rows are invalid")
        columns, primary_key = _collector_table_columns(table)
        normalized_rows: list[dict[str, object]] = []
        prior_key: tuple[object, ...] | None = None
        for row in rows:
            if not isinstance(row, Mapping):
                raise CollectorContinuityError("collector step raw before-image row is invalid")
            require_exact_keys(row, set(columns), "collector step raw before-image row")
            plain = dict(row)
            try:
                canonical_json_bytes(plain)
            except CollectorContinuityError as exc:
                raise CollectorContinuityError(
                    "collector step raw before-image row is invalid"
                ) from exc
            key = tuple(plain[column] for column in primary_key)
            if prior_key is not None and key <= prior_key:
                raise CollectorContinuityError(
                    "collector step raw before-image rows are not primary-key ordered"
                )
            prior_key = key
            normalized_rows.append(plain)
        normalized[table] = normalized_rows
    return decode_canonical_json_object(
        canonical_json_bytes(
            {
                "schema_version": COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
                "selector_rows": normalized,
            }
        )
    )


def _step_raw_before_from_baseline(
    baseline: CollectorStepBaseline, spec: CollectorStepSpec
) -> dict[str, object]:
    """Serialize the single raw snapshot that produced an attempt's state hash."""

    if not isinstance(baseline, CollectorStepBaseline):
        raise CollectorContinuityError("collector raw baseline is invalid")
    rows = {
        table: [dict(row) for row in baseline.selector_rows.get(table, ())]
        for table in sorted(spec.allowed_tables)
    }
    return _validate_step_raw_before(
        {
            "schema_version": COLLECTOR_STEP_RAW_BEFORE_SCHEMA,
            "selector_rows": rows,
        },
        allowed_tables=spec.allowed_tables,
    )


def _rehydrate_collector_step_baseline(
    start: Mapping[str, object], spec: CollectorStepSpec
) -> CollectorStepBaseline:
    """Rebuild a raw baseline solely from one persisted attempt start event."""

    _validate_raw_step_spec(spec)
    detail: Mapping[str, object] = start
    if start.get("event_type") == "ATTEMPT_STARTED":
        event = start.get("event")
        if not isinstance(event, Mapping):
            raise CollectorContinuityError("collector recovery attempt start is invalid")
        detail = event
    _validate_attempt_identity(detail, require_nonce=True)
    expected = {
        "registration_sha256": spec.registration_sha256,
        "session": spec.session,
        "phase": spec.phase,
        "step_id": spec.step_id,
        "step_ordinal": spec.step_ordinal,
        "command_sha256": spec.command_sha256,
    }
    if any(detail.get(field) != value for field, value in expected.items()):
        raise CollectorContinuityError("collector recovery attempt start drifted")
    state = validate_collector_step_state(
        detail["step_state_before"], allowed_tables=spec.allowed_tables
    )
    if state["collector_state_sha256"] != detail["state_before_sha256"]:
        raise CollectorContinuityError("collector recovery state_before hash is invalid")
    raw_before = _validate_step_raw_before(
        detail["step_raw_before"], allowed_tables=spec.allowed_tables
    )
    rows = raw_before["selector_rows"]
    if not isinstance(rows, Mapping):
        raise CollectorContinuityError("collector recovery raw before-image is invalid")
    selector_rows = {
        table: tuple(dict(row) for row in rows[table])
        for table in sorted(spec.allowed_tables)
        if isinstance(rows.get(table), list)
    }
    if set(selector_rows) != set(spec.allowed_tables):
        raise CollectorContinuityError("collector recovery raw before-image is invalid")
    return CollectorStepBaseline(
        step_state=state,
        selector_rows=selector_rows,
        _rehydrated=True,
    )


@dataclass(frozen=True)
class _RegisteredCollectorReadAuthority:
    """Immutable authority needed to bind one registered read-only database."""

    registration_file: str
    registration_sha256: str
    canonical_path: str
    expected_identity: PhysicalFileIdentity
    ledger_path: str
    ledger_identity: PhysicalFileIdentity
    database_uuid: str
    cohort_sha256: str
    genesis_sha256: str
    ledger_genesis_event_sha256: str
    collector_schema_sha256: str

    def prepared(self) -> dict[str, object]:
        return {
            "schema_version": COLLECTOR_PREPARATION_SCHEMA,
            "database_path": self.canonical_path,
            "ledger_path": self.ledger_path,
            "database_identity": self.expected_identity.to_dict(),
            "ledger_identity": self.ledger_identity.to_dict(),
            "database_uuid": self.database_uuid,
            "cohort_sha256": self.cohort_sha256,
            "genesis_sha256": self.genesis_sha256,
            "ledger_genesis_event_sha256": self.ledger_genesis_event_sha256,
            "collector_schema_sha256": self.collector_schema_sha256,
        }


class CollectorReadToken:
    """Opaque capability accepted only by the registered collector read APIs."""

    __slots__ = ()

    def __copy__(self) -> "CollectorReadToken":
        raise TypeError("collector read tokens cannot be copied")

    def __deepcopy__(self, memo: object) -> "CollectorReadToken":
        del memo
        raise TypeError("collector read tokens cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("collector read tokens cannot be serialized")

    def __reduce__(self) -> object:
        raise TypeError("collector read tokens cannot be serialized")

    def __repr__(self) -> str:
        return "CollectorReadToken()"


@dataclass
class _RegisteredCollectorReadBinding:
    """Private state retained behind one opaque registered-read token."""

    token: CollectorReadToken | None
    connection: sqlite3.Connection | None
    control_fd: int
    guard_fd: int
    owner_pid: int
    owner_thread_id: int
    authority: _RegisteredCollectorReadAuthority
    spec: CollectorStepSpec
    registry_lock: object
    state: Literal["OPEN", "CLOSING", "POISONED", "CLOSED"] = "OPEN"
    active_operations: int = 0


def _registered_collector_read_sql_is_forbidden(statement: str) -> bool:
    """Reject connection-local escapes before SQLite receives the statement."""

    normalized = statement.lstrip().lower()
    if normalized.startswith(("attach ", "attach\n", "detach ", "detach\n")):
        return True
    if not normalized.startswith("pragma"):
        return False
    pragma = normalized[6:].lstrip()
    name, separator, _ = pragma.partition("=")
    if not separator:
        name, separator, _ = pragma.partition("(")
    return separator and name.strip().rsplit(".", 1)[-1] == "query_only"


class _RegisteredCollectorReadCursor(sqlite3.Cursor):
    """Cursor surface that cannot bypass its owning registered-read binding."""

    def _require_bound(self) -> None:
        connection = self.connection
        if not isinstance(connection, _RegisteredCollectorReadConnection):
            raise CollectorContinuityError("registered collector read cursor is invalid")
        connection._require_bound()

    def execute(
        self, statement: str, parameters: Sequence[object] = ()
    ) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(statement):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().execute(statement, parameters)

    def executemany(
        self, statement: str, parameters: object
    ) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(statement):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().executemany(statement, parameters)

    def executescript(self, script: str) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(script):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().executescript(script)


class _RegisteredCollectorReadConnection(sqlite3.Connection):
    """Raw SQLite handle whose direct entry points remain binding-gated."""

    _registered_collector_read_binding: _RegisteredCollectorReadBinding | None = None

    def _bind(self, binding: _RegisteredCollectorReadBinding) -> None:
        self._registered_collector_read_binding = binding

    def _require_bound(self) -> None:
        binding = self._registered_collector_read_binding
        if (
            binding is None
            or binding.connection is not self
            or not _registered_collector_read_binding_is_active(binding)
        ):
            raise CollectorContinuityError("registered collector read connection is invalid")

    def execute(
        self, statement: str, parameters: Sequence[object] = ()
    ) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(statement):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().execute(statement, parameters)

    def executemany(
        self, statement: str, parameters: object
    ) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(statement):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().executemany(statement, parameters)

    def executescript(self, script: str) -> sqlite3.Cursor:
        self._require_bound()
        if _registered_collector_read_sql_is_forbidden(script):
            raise CollectorContinuityError("registered collector read SQL is forbidden")
        return super().executescript(script)

    def cursor(self, factory: type[sqlite3.Cursor] | None = None) -> sqlite3.Cursor:
        self._require_bound()
        if factory is not None:
            raise CollectorContinuityError("registered collector read cursor factory is forbidden")
        return sqlite3.Connection.cursor(self, _RegisteredCollectorReadCursor)

    def commit(self) -> None:
        self._require_bound()
        sqlite3.Connection.commit(self)

    def rollback(self) -> None:
        self._require_bound()
        sqlite3.Connection.rollback(self)


@dataclass(frozen=True)
class _RegisteredCollectorReadForkPlan:
    """Ownership facts frozen under the registry lock immediately before fork."""

    binding: _RegisteredCollectorReadBinding
    connection: sqlite3.Connection | None
    control_fd: int
    guard_fd: int
    ownership_proven: bool


_REGISTERED_COLLECTOR_READ_LOCK = threading.RLock()
_REGISTERED_COLLECTOR_READ_BINDINGS: dict[
    CollectorReadToken, _RegisteredCollectorReadBinding
] = {}
_REGISTERED_COLLECTOR_READ_FORK_GUARD = False
_REGISTERED_COLLECTOR_READ_FORK_PLAN: tuple[_RegisteredCollectorReadForkPlan, ...] = ()
_REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD = False
_REGISTERED_COLLECTOR_READ_FORK_QUARANTINED = False
_REGISTERED_COLLECTOR_READ_QUARANTINE: list[sqlite3.Connection] = []


def _collector_cleanup_error(
    label: str, stages: Sequence[str], errors: Sequence[BaseException]
) -> CollectorContinuityError:
    """Return one fail-closed error without discarding any cleanup failure."""

    message = f"{label}: {', '.join(stages)}"
    result = CollectorContinuityError(message)
    if len(errors) == 1:
        result.__cause__ = errors[0]
    elif errors:
        group_type = (
            _ExceptionGroup
            if all(isinstance(error, Exception) for error in errors)
            else _BaseExceptionGroup
        )
        if group_type is not None:
            result.__cause__ = group_type(label, list(errors))
        else:
            additional = errors[1:]
            classes = ", ".join(type(error).__name__ for error in additional)
            result = CollectorContinuityError(
                f"{message}; additional cleanup failures: {len(additional)} ({classes})"
            )
            result.__cause__ = errors[0]
    return result


def _combine_collector_context_errors(
    body_error: BaseException, cleanup_error: BaseException
) -> BaseException:
    """Keep both context-body and finalization failures observable."""

    label = "registered collector read body and cleanup failed"
    group_type = (
        _ExceptionGroup
        if isinstance(body_error, Exception) and isinstance(cleanup_error, Exception)
        else _BaseExceptionGroup
    )
    if group_type is not None:
        return group_type(label, [body_error, cleanup_error])
    result = CollectorContinuityError(
        f"{label}; additional cleanup failures: 1 ({type(cleanup_error).__name__})"
    )
    result.__cause__ = body_error
    return result


def _sqlite_authorizer_actions(*names: str) -> frozenset[int]:
    values: set[int] = set()
    for name in names:
        value = getattr(sqlite3, name, None)
        if isinstance(value, int):
            values.add(value)
    return frozenset(values)


_REGISTERED_COLLECTOR_READ_DENIED_ACTIONS: Final = _sqlite_authorizer_actions(
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_VTABLE",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
)
_REGISTERED_COLLECTOR_READ_PRAGMA_ACTION: Final = getattr(sqlite3, "SQLITE_PRAGMA", -1)
_REGISTERED_COLLECTOR_READ_PRAGMAS: Final = frozenset(
    {
        "busy_timeout",
        "database_list",
        "foreign_key_check",
        "foreign_keys",
        "journal_mode",
        "query_only",
        "synchronous",
    }
)


def _registered_collector_read_binding_is_active(
    binding: _RegisteredCollectorReadBinding,
) -> bool:
    if binding.owner_pid != os.getpid() or binding.owner_thread_id != threading.get_ident():
        return False
    with _REGISTERED_COLLECTOR_READ_LOCK:
        token = binding.token
        return (
            token is not None
            and _REGISTERED_COLLECTOR_READ_BINDINGS.get(token) is binding
            and binding.state == "OPEN"
            and binding.connection is not None
        )


def _registered_collector_read_authorizer(
    binding: _RegisteredCollectorReadBinding,
):
    """Return the fixed SQLite authorizer for one live registered read binding."""

    def authorize(
        action: int,
        argument_1: str | None,
        argument_2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if not _registered_collector_read_binding_is_active(binding):
            return sqlite3.SQLITE_DENY
        if action in _REGISTERED_COLLECTOR_READ_DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == _REGISTERED_COLLECTOR_READ_PRAGMA_ACTION:
            if argument_1 not in _REGISTERED_COLLECTOR_READ_PRAGMAS:
                # This connection-local planner switch changes neither schema
                # nor data and is required by the deterministic snapshot test.
                if argument_1 != "reverse_unordered_selects" or argument_2 not in {"ON", "on", "1"}:
                    return sqlite3.SQLITE_DENY
            elif argument_2 is not None:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _require_registered_collector_read_environment() -> None:
    """Allow this fd-backed binding only on the reviewed local POSIX platform."""

    require_collector_continuity_health()
    if os.name != "posix":
        raise CollectorContinuityError("registered collector reads require local POSIX")
    try:
        system = os.uname().sysname
        fd_status = os.stat("/dev/fd")
    except (AttributeError, OSError) as exc:
        raise CollectorContinuityError("registered collector read environment is unavailable") from exc
    if system != "Darwin" or not stat.S_ISDIR(fd_status.st_mode):
        raise CollectorContinuityError("registered collector reads require local macOS /dev/fd")
    for name in ("O_NOFOLLOW", "O_CLOEXEC"):
        flag = getattr(os, name, None)
        if not isinstance(flag, int) or flag == 0:
            raise CollectorContinuityError("registered collector read flags are unavailable")
    if not _REGISTERED_COLLECTOR_READ_FORK_GUARD:
        raise CollectorContinuityError("registered collector read fork guard is unavailable")


def _fd_matches_expected_file(status: os.stat_result, expected: PhysicalFileIdentity) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and int(status.st_dev) == expected.file_st_dev
        and int(status.st_ino) == expected.file_st_ino
    )


def _verify_private_registered_read_anchors(
    control_fd: int, guard_fd: int, expected: PhysicalFileIdentity
) -> None:
    """Prove two private descriptors retain one shared open file description."""

    if control_fd < 0 or guard_fd < 0 or control_fd == guard_fd:
        raise CollectorContinuityError("registered collector private descriptors are invalid")
    control_offset: int | None = None
    guard_offset: int | None = None
    failure: BaseException | None = None
    try:
        if os.get_inheritable(control_fd) or os.get_inheritable(guard_fd):
            raise CollectorContinuityError("registered collector private descriptors are inheritable")
        control_status = os.fstat(control_fd)
        guard_status = os.fstat(guard_fd)
        if (
            not _fd_matches_expected_file(control_status, expected)
            or not _fd_matches_expected_file(guard_status, expected)
        ):
            raise CollectorContinuityError("registered collector private descriptor identity drifted")
        control_offset = os.lseek(control_fd, 0, os.SEEK_CUR)
        guard_offset = os.lseek(guard_fd, 0, os.SEEK_CUR)
        if control_offset != guard_offset:
            raise CollectorContinuityError("registered collector private descriptor ownership drifted")
        first_challenge = 1 if control_offset == 0 else 0
        os.lseek(control_fd, first_challenge, os.SEEK_SET)
        if os.lseek(guard_fd, 0, os.SEEK_CUR) != first_challenge:
            raise CollectorContinuityError("registered collector control descriptor was reused")
        second_challenge = 0 if first_challenge else 1
        os.lseek(guard_fd, second_challenge, os.SEEK_SET)
        if os.lseek(control_fd, 0, os.SEEK_CUR) != second_challenge:
            raise CollectorContinuityError("registered collector guard descriptor was reused")
    except (CollectorContinuityError, OSError) as exc:
        failure = exc
    finally:
        if control_offset is not None:
            try:
                os.lseek(control_fd, control_offset, os.SEEK_SET)
            except OSError as exc:
                failure = failure or exc
        if guard_offset is not None:
            try:
                os.lseek(guard_fd, guard_offset, os.SEEK_SET)
            except OSError as exc:
                failure = failure or exc
    if failure is not None:
        if isinstance(failure, CollectorContinuityError):
            raise failure
        raise CollectorContinuityError(
            "registered collector private descriptor ownership cannot be proven"
        ) from failure


def _poison_registered_collector_read_binding(
    binding: _RegisteredCollectorReadBinding,
) -> None:
    binding.state = "POISONED"


def _verify_registered_collector_read_anchors_locked(
    binding: _RegisteredCollectorReadBinding,
) -> None:
    if binding.active_operations:
        _poison_registered_collector_read_binding(binding)
        raise CollectorContinuityError("registered collector read anchors are busy")
    try:
        _verify_private_registered_read_anchors(
            binding.control_fd,
            binding.guard_fd,
            binding.authority.expected_identity,
        )
    except CollectorContinuityError:
        _poison_registered_collector_read_binding(binding)
        raise


def _probe_registered_collector_fd_locator_locked(
    binding: _RegisteredCollectorReadBinding,
) -> None:
    """Verify the locator through one fresh descriptor and one close attempt."""

    _verify_registered_collector_read_anchors_locked(binding)
    probe_fd = -1
    probe_is_fresh = False
    errors: list[BaseException] = []
    stages: list[str] = []
    try:
        probe_fd = os.open(
            f"/dev/fd/{binding.control_fd}",
            os.O_RDONLY | int(getattr(os, "O_CLOEXEC")),
        )
        if probe_fd in {binding.control_fd, binding.guard_fd}:
            probe_fd = -1
            raise CollectorContinuityError(
                "registered collector fd locator reused a private descriptor"
            )
        probe_is_fresh = True
        os.set_inheritable(probe_fd, False)
        if not _fd_matches_expected_file(
            os.fstat(probe_fd), binding.authority.expected_identity
        ):
            raise CollectorContinuityError("registered collector fd locator identity drifted")
    except (CollectorContinuityError, OSError) as exc:
        stages.append("locator_probe")
        errors.append(exc)
    if probe_is_fresh and probe_fd >= 0:
        descriptor = probe_fd
        probe_fd = -1
        try:
            os.close(descriptor)
        except OSError as exc:
            stages.append("locator_close")
            errors.append(exc)
    if errors:
        _poison_registered_collector_read_binding(binding)
        raise _collector_cleanup_error(
            "registered collector fd locator is invalid", stages, errors
        )


def _retire_registered_collector_read_binding_locked(
    binding: _RegisteredCollectorReadBinding,
) -> BaseException | None:
    """Drop registry authority before one-time, ownership-proven cleanup."""

    token = binding.token
    if token is not None:
        _REGISTERED_COLLECTOR_READ_BINDINGS.pop(token, None)
    binding.token = None
    binding.state = "CLOSING"
    stages: list[str] = []
    errors: list[BaseException] = []
    connection = binding.connection
    binding.connection = None
    if binding.active_operations:
        binding.active_operations = 0
        if connection is not None:
            _REGISTERED_COLLECTOR_READ_QUARANTINE.append(connection)
        stages.append("active_operation")
        errors.append(CollectorContinuityError("registered collector read operation is still active"))
        _mark_collector_continuity_fatal("registered-read-retirement-active-operation")
        binding.state = "POISONED"
        return _collector_cleanup_error(
            "registered collector read retirement failed", stages, errors
        )
    if connection is not None:
        try:
            sqlite3.Connection.close(connection)
        except BaseException as exc:
            _REGISTERED_COLLECTOR_READ_QUARANTINE.append(connection)
            stages.append("connection_close")
            errors.append(exc)
    ownership_proven = False
    try:
        _verify_private_registered_read_anchors(
            binding.control_fd,
            binding.guard_fd,
            binding.authority.expected_identity,
        )
        ownership_proven = True
    except BaseException as exc:
        stages.append("anchor_ownership")
        errors.append(exc)
    if ownership_proven:
        for attribute in ("control_fd", "guard_fd"):
            descriptor = getattr(binding, attribute)
            setattr(binding, attribute, -1)
            try:
                os.close(descriptor)
            except OSError as exc:
                stages.append(f"{attribute}_close")
                errors.append(exc)
    if errors:
        _mark_collector_continuity_fatal(
            "registered-read-retirement-" + "+".join(stages)
        )
        binding.state = "POISONED"
        return _collector_cleanup_error(
            "registered collector read retirement failed", stages, errors
        )
    binding.state = "CLOSED"
    return None


def _prepare_registered_read_fork() -> None:
    global _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD
    global _REGISTERED_COLLECTOR_READ_FORK_PLAN
    _REGISTERED_COLLECTOR_READ_LOCK.acquire()
    _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD = True
    try:
        plan: list[_RegisteredCollectorReadForkPlan] = []
        for binding in _REGISTERED_COLLECTOR_READ_BINDINGS.values():
            ownership_proven = False
            if binding.active_operations == 0:
                try:
                    _verify_private_registered_read_anchors(
                        binding.control_fd,
                        binding.guard_fd,
                        binding.authority.expected_identity,
                    )
                    ownership_proven = True
                except CollectorContinuityError:
                    pass
            plan.append(
                _RegisteredCollectorReadForkPlan(
                    binding=binding,
                    connection=binding.connection,
                    control_fd=binding.control_fd,
                    guard_fd=binding.guard_fd,
                    ownership_proven=ownership_proven,
                )
            )
        _REGISTERED_COLLECTOR_READ_FORK_PLAN = tuple(plan)
    except BaseException:
        _REGISTERED_COLLECTOR_READ_FORK_PLAN = ()
        if _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD:
            _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD = False
            _REGISTERED_COLLECTOR_READ_LOCK.release()
        raise


def _restore_registered_read_parent_after_fork() -> None:
    global _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD
    global _REGISTERED_COLLECTOR_READ_FORK_PLAN
    _REGISTERED_COLLECTOR_READ_FORK_PLAN = ()
    if _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD:
        _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD = False
        _REGISTERED_COLLECTOR_READ_LOCK.release()


def _invalidate_registered_read_connections_after_fork() -> None:
    """Child-only fork hook: discard tokens and anchors without SQLite calls."""

    global _REGISTERED_COLLECTOR_READ_BINDINGS, _REGISTERED_COLLECTOR_READ_LOCK
    global _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD
    global _REGISTERED_COLLECTOR_READ_FORK_PLAN
    global _REGISTERED_COLLECTOR_READ_FORK_QUARANTINED
    inherited = _REGISTERED_COLLECTOR_READ_FORK_PLAN
    _REGISTERED_COLLECTOR_READ_BINDINGS = {}
    _REGISTERED_COLLECTOR_READ_LOCK = threading.RLock()
    _REGISTERED_COLLECTOR_READ_FORK_LOCK_HELD = False
    _REGISTERED_COLLECTOR_READ_FORK_PLAN = ()
    _REGISTERED_COLLECTOR_READ_FORK_QUARANTINED = bool(inherited)
    for entry in inherited:
        binding = entry.binding
        binding.state = "POISONED"
        binding.active_operations = 0
        binding.token = None
        connection = binding.connection
        binding.connection = None
        if connection is not None:
            _REGISTERED_COLLECTOR_READ_QUARANTINE.append(connection)
        if not entry.ownership_proven:
            _mark_collector_continuity_fatal(
                "registered-read-fork-child-ownership-unproven"
            )
            continue
        for attribute, descriptor in (
            ("control_fd", entry.control_fd),
            ("guard_fd", entry.guard_fd),
        ):
            setattr(binding, attribute, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    _mark_collector_continuity_fatal(
                        f"registered-read-fork-child-{attribute}-close"
                    )


try:
    os.register_at_fork(
        before=_prepare_registered_read_fork,
        after_in_parent=_restore_registered_read_parent_after_fork,
        after_in_child=_invalidate_registered_read_connections_after_fork,
    )
except (AttributeError, RuntimeError):
    pass
else:
    _REGISTERED_COLLECTOR_READ_FORK_GUARD = True


def _open_registered_collector_read_control_fds(
    authority: _RegisteredCollectorReadAuthority,
    spec: CollectorStepSpec,
) -> tuple[int, int]:
    """Open private no-follow control and guard descriptors for one database."""

    parent_fd, canonical_path, name = _open_parent(authority.canonical_path)
    control_fd = -1
    guard_fd = -1
    temporary: _RegisteredCollectorReadBinding | None = None
    try:
        flags = os.O_RDONLY | _no_follow_flag() | int(getattr(os, "O_CLOEXEC"))
        control_fd = os.open(name, flags, dir_fd=parent_fd)
        os.set_inheritable(control_fd, False)
        identity = _identity_from_open_file(control_fd, parent_fd, canonical_path)
        if identity != authority.expected_identity:
            raise CollectorContinuityError("registered collector database identity drifted")
        guard_fd = os.dup(control_fd)
        temporary = _RegisteredCollectorReadBinding(
            token=CollectorReadToken(),
            connection=None,
            control_fd=control_fd,
            guard_fd=guard_fd,
            owner_pid=os.getpid(),
            owner_thread_id=threading.get_ident(),
            authority=authority,
            spec=spec,
            registry_lock=_REGISTERED_COLLECTOR_READ_LOCK,
        )
        os.set_inheritable(guard_fd, False)
        _verify_private_registered_read_anchors(
            control_fd, guard_fd, authority.expected_identity
        )
        _probe_registered_collector_fd_locator_locked(temporary)
        return control_fd, guard_fd
    except BaseException as body_error:
        cleanup_error: BaseException | None = None
        if temporary is not None:
            cleanup_error = _retire_registered_collector_read_binding_locked(temporary)
        elif control_fd >= 0:
            descriptor = control_fd
            control_fd = -1
            try:
                os.close(descriptor)
            except OSError as exc:
                _mark_collector_continuity_fatal(
                    "registered-read-control-open-cleanup-close"
                )
                cleanup_error = _collector_cleanup_error(
                    "registered collector control descriptor cannot be closed",
                    ("control_fd_close",),
                    (exc,),
                )
        if cleanup_error is not None:
            raise _combine_collector_context_errors(body_error, cleanup_error) from None
        raise
    finally:
        try:
            os.close(parent_fd)
        except OSError as exc:
            raise CollectorContinuityError(
                "registered collector parent descriptor cannot be closed"
            ) from exc


def _read_registered_collector_read_authority(
    spec: CollectorStepSpec,
) -> _RegisteredCollectorReadAuthority:
    """Reconstruct read authority from the bound `/4` registration without SQLite."""

    _require_registered_collector_read_environment()
    frozen = _validate_raw_step_spec(spec)
    registration_file = lexical_absolute_path(spec.registration_file)
    opened = open_nofollow_regular(registration_file)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise CollectorContinuityError("registered collector registration size is invalid")
        raw = os.pread(opened.descriptor, size, 0)
        if len(raw) != size:
            raise CollectorContinuityError("registered collector registration was truncated")
        verify_file_identity(registration_file, opened.identity)
    except OSError as exc:
        raise CollectorContinuityError("registered collector registration cannot be read") from exc
    finally:
        opened.close()
    registration = decode_canonical_json_object(raw)
    require_exact_keys(registration, _REGISTRATION_V4_FIELDS, "registered collector registration")
    registration_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        registration.get("schema_version") != _REGISTRATION_V4_SCHEMA
        or registration_sha256 != spec.registration_sha256
        or registration_sha256 != frozen.registration_sha256
    ):
        raise CollectorContinuityError("registered collector registration identity drifted")
    prerequisites = registration.get("prerequisites")
    if not isinstance(prerequisites, Mapping):
        raise CollectorContinuityError("registered collector prerequisites are invalid")
    collector = prerequisites.get("collector")
    if not isinstance(collector, Mapping):
        raise CollectorContinuityError("registered collector capability is invalid")
    capability = decode_capability(canonical_json_bytes(dict(collector)))
    try:
        canonical_path = lexical_absolute_path(capability["database_path"])
        ledger_path = lexical_absolute_path(capability["ledger_path"])
        registration_path = lexical_absolute_path(registration["database_path"])
        expected_identity = PhysicalFileIdentity.from_dict(capability["database_identity"])
        ledger_identity = PhysicalFileIdentity.from_dict(capability["ledger_identity"])
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("registered collector capability is invalid") from exc
    if (
        canonical_path != expected_identity.canonical_path
        or ledger_path != ledger_identity.canonical_path
        or registration_path != canonical_path
        or canonical_path != spec.database_path
        or canonical_path != frozen.database_path
        or capability["source"] != frozen.source
        or capability["adjustment_mode"] != frozen.adjustment_mode
        or capability["adjustment_version"] != frozen.adjustment_version
        or registration.get("source") != frozen.source
        or registration.get("adjustment_mode") != frozen.adjustment_mode
        or registration.get("adjustment_version") != frozen.adjustment_version
    ):
        raise CollectorContinuityError("registered collector capability drifted")
    ledger = _parse_retained_bound_collector_ledger(ledger_path, ledger_identity)
    if len(ledger) < 2 or ledger[1].get("event_type") != "REGISTRATION_BOUND":
        raise CollectorContinuityError("registered collector registration is not ledger-bound")
    binding = ledger[1].get("event")
    expected_binding = {
        "registration_sha256": registration_sha256,
        "panel_sha256": registration.get("panel_sha256"),
        "sessions": list(frozen.sessions),
        "sessions_sha256": canonical_json_sha256(list(frozen.sessions)),
        "prerequisites_sha256": registration.get("prerequisites_sha256"),
    }
    if not isinstance(binding, Mapping) or any(
        binding.get(field) != value for field, value in expected_binding.items()
    ):
        raise CollectorContinuityError("registered collector ledger binding drifted")
    return _RegisteredCollectorReadAuthority(
        registration_file=registration_file,
        registration_sha256=registration_sha256,
        canonical_path=canonical_path,
        expected_identity=expected_identity,
        ledger_path=ledger_path,
        ledger_identity=ledger_identity,
        database_uuid=str(capability["database_uuid"]),
        cohort_sha256=str(capability["cohort_sha256"]),
        genesis_sha256=str(capability["genesis_sha256"]),
        ledger_genesis_event_sha256=str(capability["ledger_genesis_event_sha256"]),
        collector_schema_sha256=str(capability["collector_schema_sha256"]),
    )


def _install_registered_collector_read_binding_locked(
    token: CollectorReadToken,
    connection: sqlite3.Connection,
    authority: _RegisteredCollectorReadAuthority,
    spec: CollectorStepSpec,
    control_fd: int,
    guard_fd: int,
) -> _RegisteredCollectorReadBinding:
    """Keep the raw connection and private fds behind one exact token object."""

    if token in _REGISTERED_COLLECTOR_READ_BINDINGS:
        raise CollectorContinuityError("registered collector read token is already bound")
    binding = _RegisteredCollectorReadBinding(
        token=token,
        connection=connection,
        control_fd=control_fd,
        guard_fd=guard_fd,
        owner_pid=os.getpid(),
        owner_thread_id=threading.get_ident(),
        authority=authority,
        spec=spec,
        registry_lock=_REGISTERED_COLLECTOR_READ_LOCK,
    )
    _REGISTERED_COLLECTOR_READ_BINDINGS[token] = binding
    return binding


def _bound_registered_collector_read_token_locked(
    token: object, spec: CollectorStepSpec
) -> _RegisteredCollectorReadBinding:
    if type(token) is not CollectorReadToken:
        raise CollectorContinuityError("collector read token is invalid")
    binding = _REGISTERED_COLLECTOR_READ_BINDINGS.get(token)
    if (
        binding is None
        or binding.token is not token
        or binding.state != "OPEN"
        or binding.connection is None
        or binding.registry_lock is not _REGISTERED_COLLECTOR_READ_LOCK
        or binding.spec != spec
    ):
        raise CollectorContinuityError("collector read token binding is invalid")
    if binding.owner_pid != os.getpid():
        raise CollectorContinuityError("collector read token belongs to another process")
    if binding.owner_thread_id != threading.get_ident():
        raise CollectorContinuityError("collector read token belongs to another thread")
    return binding


def _verify_bound_collector_read_token_locked(
    token: object, spec: CollectorStepSpec
) -> _RegisteredCollectorReadBinding:
    """Reprove token, fd, path, SQLite, and retained-ledger authority together."""

    _require_registered_collector_read_environment()
    binding = _bound_registered_collector_read_token_locked(token, spec)
    try:
        authority = _read_registered_collector_read_authority(spec)
        if binding.authority != authority:
            raise CollectorContinuityError("registered collector read authority drifted")
        _verify_registered_collector_read_anchors_locked(binding)
        _probe_registered_collector_fd_locator_locked(binding)
        verify_file_identity(
            binding.authority.canonical_path, binding.authority.expected_identity
        )
        _reject_registered_collector_read_sidecars(binding.authority.canonical_path)
        connection = binding.connection
        if connection is None:
            raise CollectorContinuityError("registered collector read connection is unavailable")
        _verify_registered_collector_read_connection_contract(connection, binding)
        _verify_registered_collector_read_authority(connection, binding)
        _verify_registered_collector_read_anchors_locked(binding)
        verify_file_identity(
            binding.authority.canonical_path, binding.authority.expected_identity
        )
        _reject_registered_collector_read_sidecars(binding.authority.canonical_path)
        return binding
    except (CollectorContinuityError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        _poison_registered_collector_read_binding(binding)
        if isinstance(exc, CollectorContinuityError):
            raise
        raise CollectorContinuityError("registered collector read token verification failed") from exc


@contextmanager
def _borrow_registered_collector_read_connection(
    token: object, spec: CollectorStepSpec
) -> Iterator[sqlite3.Connection]:
    """Use the hidden connection only while the registry lock pins ownership."""

    with _REGISTERED_COLLECTOR_READ_LOCK:
        binding = _verify_bound_collector_read_token_locked(token, spec)
        connection = binding.connection
        if connection is None:
            _poison_registered_collector_read_binding(binding)
            raise CollectorContinuityError("registered collector read connection is unavailable")
        binding.active_operations += 1
        try:
            yield connection
        finally:
            binding.active_operations -= 1
            if binding.state == "OPEN":
                _verify_bound_collector_read_token_locked(token, spec)


def _close_registered_collector_read_token(token: CollectorReadToken) -> None:
    """Close the hidden SQLite handle, then proven-private descriptors, then registry."""

    with _REGISTERED_COLLECTOR_READ_LOCK:
        binding = _REGISTERED_COLLECTOR_READ_BINDINGS.get(token)
        if binding is None or binding.token is not token:
            return
        if binding.owner_pid != os.getpid() or binding.owner_thread_id != threading.get_ident():
            raise CollectorContinuityError("collector read token close owner is invalid")
        cleanup_error = _retire_registered_collector_read_binding_locked(binding)
        if cleanup_error is not None:
            raise cleanup_error


def _reject_registered_collector_read_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.lstat(path + suffix)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CollectorContinuityError("registered collector sidecar cannot be inspected") from exc
        raise CollectorContinuityError("registered collector sidecars are forbidden")


def _verify_registered_collector_read_connection_contract(
    connection: sqlite3.Connection,
    binding: _RegisteredCollectorReadBinding,
) -> None:
    """Validate the fixed read-only connection settings and sole main locator."""

    if connection.in_transaction:
        raise CollectorContinuityError("collector read connection has an active transaction")
    if connection.row_factory is not None or connection.text_factory is not str:
        raise CollectorContinuityError("collector read connection factories drifted")
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as exc:
        raise CollectorContinuityError("collector read database list is unavailable") from exc
    if len(rows) != 1:
        raise CollectorContinuityError("collector read connection has attached databases")
    row = rows[0]
    if len(row) != 3:
        raise CollectorContinuityError("collector read database list is invalid")
    schema, locator = row[1], row[2]
    if schema != "main" or locator != f"/dev/fd/{binding.control_fd}":
        raise CollectorContinuityError("collector read database locator drifted")
    try:
        query_only = connection.execute("PRAGMA query_only").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        foreign_key_error = connection.execute("PRAGMA main.foreign_key_check").fetchone()
    except sqlite3.Error as exc:
        raise CollectorContinuityError("collector read connection pragmas are invalid") from exc
    if (
        query_only is None
        or foreign_keys is None
        or busy_timeout is None
        or journal_mode is None
        or int(query_only[0]) != 1
        or int(foreign_keys[0]) != 1
        or int(busy_timeout[0]) != COLLECTOR_BUSY_TIMEOUT_MS
        or str(journal_mode[0]).lower() != "delete"
        or foreign_key_error is not None
    ):
        raise CollectorContinuityError("collector read connection contract drifted")


def _verify_registered_collector_read_authority(
    connection: sqlite3.Connection,
    binding: _RegisteredCollectorReadBinding,
) -> None:
    """Verify schema, genesis, cohort, and ledger facts through the bound read fd."""

    authority = binding.authority
    try:
        verify_collector_authority_schema(connection)
        _, cohort_sha256 = _read_prepared_cohort(connection)
        rows = connection.execute(
            "SELECT database_uuid,cohort_sha256,genesis_json,genesis_sha256,"
            "ledger_genesis_event_sha256,created_at FROM main.forward_collector_genesis"
        ).fetchall()
    except sqlite3.Error as exc:
        raise CollectorContinuityError("registered collector database authority is invalid") from exc
    if len(rows) != 1 or len(rows[0]) != 6:
        raise CollectorContinuityError("registered collector genesis is invalid")
    row = rows[0]
    try:
        genesis = decode_canonical_json_object(str(row[2]).encode("ascii"))
        validated = _validate_prepared_genesis(genesis)
    except (UnicodeEncodeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("registered collector genesis is invalid") from exc
    schema_sha256 = _prepared_schema_sha256(connection)
    if (
        row[0] != authority.database_uuid
        or row[1] != cohort_sha256
        or cohort_sha256 != authority.cohort_sha256
        or row[3] != authority.genesis_sha256
        or row[4] != authority.ledger_genesis_event_sha256
        or schema_sha256 != authority.collector_schema_sha256
        or validated["database_uuid"] != authority.database_uuid
        or validated["cohort_sha256"] != authority.cohort_sha256
        or canonical_json_sha256(validated) != authority.genesis_sha256
        or validated["database_identity"] != authority.expected_identity.to_dict()
        or validated["ledger_identity"] != authority.ledger_identity.to_dict()
        or validated["created_at"] != row[5]
        or validated["collector_schema_sha256"] != authority.collector_schema_sha256
    ):
        raise CollectorContinuityError("registered collector prepared authority drifted")
    ledger = _parse_retained_bound_collector_ledger(
        authority.ledger_path, authority.ledger_identity
    )
    if not ledger:
        raise CollectorContinuityError("registered collector ledger is empty")
    event = ledger[0]
    if (
        event.get("seq") != 0
        or event.get("event_type") != "GENESIS"
        or event.get("previous_event_sha256") != ZERO_SHA256
        or event.get("event", {}).get("genesis") != validated
        or event.get("event_sha256") != authority.ledger_genesis_event_sha256
    ):
        raise CollectorContinuityError("registered collector ledger genesis drifted")


def _discard_unpublished_registered_read_binding_locked(
    binding: _RegisteredCollectorReadBinding,
) -> None:
    """Best-effort cleanup before a token ever reaches a caller."""

    cleanup_error = _retire_registered_collector_read_binding_locked(binding)
    if cleanup_error is not None:
        raise cleanup_error


def require_bound_collector_read_connection(
    token: CollectorReadToken, spec: CollectorStepSpec
) -> None:
    """Prove a private registered-read token without exposing its authority."""

    require_collector_continuity_health()
    with _REGISTERED_COLLECTOR_READ_LOCK:
        _verify_bound_collector_read_token_locked(token, spec)
    return None


@contextmanager
def open_registered_collector_read_connection(
    spec: CollectorStepSpec,
) -> Iterator[CollectorReadToken]:
    """Yield an opaque token backed by hidden SQLite and no-follow descriptors."""

    require_collector_continuity_health()
    token = CollectorReadToken()
    binding: _RegisteredCollectorReadBinding | None = None
    try:
        with _REGISTERED_COLLECTOR_READ_LOCK:
            _require_registered_collector_read_environment()
            authority = _read_registered_collector_read_authority(spec)
            control_fd, guard_fd = _open_registered_collector_read_control_fds(authority, spec)
            binding = _RegisteredCollectorReadBinding(
                token=token,
                connection=None,
                control_fd=control_fd,
                guard_fd=guard_fd,
                owner_pid=os.getpid(),
                owner_thread_id=threading.get_ident(),
                authority=authority,
                spec=spec,
                registry_lock=_REGISTERED_COLLECTOR_READ_LOCK,
            )
            locator = f"/dev/fd/{control_fd}"
            connection = sqlite3.connect(
                f"file:{locator}?mode=ro&cache=private",
                uri=True,
                check_same_thread=True,
                isolation_level=None,
                factory=_RegisteredCollectorReadConnection,
            )
            binding.connection = connection
            connection.row_factory = None
            connection.text_factory = str
            sqlite3.Connection.execute(connection, "PRAGMA foreign_keys=ON")
            sqlite3.Connection.execute(
                connection, f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}"
            )
            sqlite3.Connection.execute(connection, "PRAGMA query_only=1")
            sqlite3.Connection.execute(connection, "PRAGMA journal_mode=DELETE")
            binding = _install_registered_collector_read_binding_locked(
                token, connection, authority, spec, control_fd, guard_fd
            )
            connection._bind(binding)
            sqlite3.Connection.set_authorizer(
                connection, _registered_collector_read_authorizer(binding)
            )
            _verify_bound_collector_read_token_locked(token, spec)
    except CollectorContinuityError:
        if binding is not None:
            with _REGISTERED_COLLECTOR_READ_LOCK:
                _discard_unpublished_registered_read_binding_locked(binding)
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        if binding is not None:
            with _REGISTERED_COLLECTOR_READ_LOCK:
                _discard_unpublished_registered_read_binding_locked(binding)
        raise CollectorContinuityError("registered collector read connection cannot be opened") from exc
    body_error: BaseException | None = None
    try:
        yield token
    except BaseException as exc:
        body_error = exc
    cleanup_error: BaseException | None = None
    try:
        _close_registered_collector_read_token(token)
    except BaseException as exc:
        cleanup_error = exc
    if body_error is not None:
        if cleanup_error is not None:
            raise _combine_collector_context_errors(body_error, cleanup_error)
        raise body_error
    if cleanup_error is not None:
        raise cleanup_error


def _validate_raw_step_spec(spec: CollectorStepSpec) -> _FrozenCollectorStepSchedule:
    """Re-root a step in immutable registration bytes without rejecting raw drift early."""

    if not isinstance(spec, CollectorStepSpec):
        raise CollectorContinuityError("collector raw step specification is invalid")
    _require_event_sha256(spec.schedule_sha256, "collector raw schedule_sha256")
    frozen = _FROZEN_COLLECTOR_STEP_SCHEDULES.get(spec.schedule_sha256)
    if frozen is None or spec.step_ordinal < 0 or spec.step_ordinal >= len(frozen.specs):
        raise CollectorContinuityError("collector raw step specification is not frozen")
    if frozen.specs[spec.step_ordinal] != spec:
        raise CollectorContinuityError("collector raw step specification is not frozen")
    path = lexical_absolute_path(spec.registration_file)
    opened = open_nofollow_regular(path)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise CollectorContinuityError("collector raw registration size is invalid")
        raw = os.pread(opened.descriptor, size, 0)
        if len(raw) != size:
            raise CollectorContinuityError("collector raw registration was truncated")
        verify_file_identity(path, opened.identity)
    except OSError as exc:
        raise CollectorContinuityError("collector raw registration cannot be read") from exc
    finally:
        opened.close()
    registration = decode_canonical_json_object(raw)
    require_exact_keys(registration, _REGISTRATION_V4_FIELDS, "collector raw registration")
    if registration.get("schema_version") != _REGISTRATION_V4_SCHEMA:
        raise CollectorContinuityError("collector raw registration schema is unsupported")
    registration_sha256 = hashlib.sha256(raw).hexdigest()
    if registration_sha256 != spec.registration_sha256 or registration_sha256 != frozen.registration_sha256:
        raise CollectorContinuityError("collector raw registration identity drifted")
    prerequisites = registration.get("prerequisites")
    if not isinstance(prerequisites, Mapping) or registration.get("prerequisites_sha256") != canonical_json_sha256(dict(prerequisites)):
        raise CollectorContinuityError("collector raw registration prerequisites drifted")
    capability = prerequisites.get("collector")
    if not isinstance(capability, Mapping):
        raise CollectorContinuityError("collector raw registration capability is invalid")
    try:
        sessions = _validate_registration_sessions(registration["sessions"])
        symbols = tuple(registration["symbols"])
        ledger_path = lexical_absolute_path(capability["ledger_path"])
        ledger_identity = PhysicalFileIdentity.from_dict(capability["ledger_identity"])
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector raw registration is invalid") from exc
    if (
        sessions != frozen.sessions
        or symbols != frozen.specs[0].symbols
        or registration.get("database_path") != frozen.database_path
        or registration.get("source") != frozen.source
        or registration.get("adjustment_mode") != frozen.adjustment_mode
        or registration.get("adjustment_version") != frozen.adjustment_version
    ):
        raise CollectorContinuityError("collector raw registration schedule drifted")
    ledger = _parse_retained_bound_collector_ledger(ledger_path, ledger_identity)
    if len(ledger) < 2 or ledger[1].get("event_type") != "REGISTRATION_BOUND":
        raise CollectorContinuityError("collector raw registration is not ledger-bound")
    binding = ledger[1].get("event")
    expected = {
        "registration_sha256": registration_sha256,
        "panel_sha256": registration.get("panel_sha256"),
        "sessions": list(sessions),
        "sessions_sha256": canonical_json_sha256(list(sessions)),
        "prerequisites_sha256": registration.get("prerequisites_sha256"),
    }
    if not isinstance(binding, Mapping) or any(binding.get(key) != value for key, value in expected.items()):
        raise CollectorContinuityError("collector raw registration binding drifted")
    return frozen


def _raw_snapshot_from_connection(
    connection: sqlite3.Connection, spec: CollectorStepSpec
) -> tuple[
    dict[str, object],
    dict[str, tuple[dict[str, object], ...]],
    dict[str, tuple[tuple[dict[str, object], list[str]], ...]],
]:
    """Read state and selected rows from one main-only, query-only transaction."""

    if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
        raise CollectorContinuityError("collector raw verification requires no active transaction")
    frozen_schedule = _validate_raw_step_spec(spec)
    original_text_factory = connection.text_factory
    original_row_factory = connection.row_factory
    factories_restored = False
    began = False
    try:
        connection.row_factory = None
        connection.text_factory = bytes
        connection.execute("BEGIN")
        began = True
        if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise CollectorContinuityError("collector raw verification foreign key check failed")
        aggregate, counts = _compute_collector_logical_digest(connection)
        records = _read_collector_step_records(connection)
        table_sha256 = {
            table: _hash_step_records(
                _STEP_TABLE_DOMAIN + table.encode("ascii") + b"\x00",
                table,
                rows,
                kind="table",
            )
            for table, rows in records.items()
        }
        receipt_sources = {
            row["receipt_id"]: row["source"]
            for row, _ in records["collection_receipts"]
            if type(row.get("receipt_id")) is int and isinstance(row.get("source"), str)
        }
        references: dict[int, list[bool]] = {}
        for table, rows in records.items():
            if table == "collection_receipts":
                continue
            for row, _ in rows:
                receipt_id = row.get("receipt_id")
                if receipt_id is not None:
                    if type(receipt_id) is not int or receipt_id not in receipt_sources:
                        raise CollectorContinuityError("collector raw receipt reference is invalid")
                    references.setdefault(receipt_id, []).append(
                        _step_row_selected(table, row, spec, frozen_schedule)
                    )

        def in_scope(table: str, row: Mapping[str, object]) -> bool:
            if table == "collection_receipts":
                receipt_id = row.get("receipt_id")
                linked = references.get(receipt_id) if type(receipt_id) is int else None
                return row.get("source") == spec.selector_source and bool(linked) and all(linked)
            return _step_row_selected(table, row, spec, frozen_schedule)

        outside_scope = {
            table: _hash_step_records(
                _STEP_OUTSIDE_DOMAIN + table.encode("ascii") + b"\x00",
                table,
                tuple(record for record in records[table] if not in_scope(table, record[0])),
                kind="outside_scope",
            )
            for table in spec.allowed_tables
        }
        high_water_row = connection.execute(
            "SELECT COALESCE(MAX(receipt_id),0) FROM main.collection_receipts"
        ).fetchone()
        if high_water_row is None or type(high_water_row[0]) is not int or high_water_row[0] < 0:
            raise CollectorContinuityError("collector raw receipt high water is invalid")
        selected = {
            table: tuple(dict(row) for row, _ in rows if in_scope(table, row))
            for table, rows in records.items()
            if table in spec.allowed_tables
        }
        state = validate_collector_step_state(
            {
                "schema_version": COLLECTOR_STEP_STATE_SCHEMA,
                "collector_state_sha256": aggregate.hexdigest(),
                "table_counts": counts,
                "table_sha256": table_sha256,
                "outside_scope_sha256": outside_scope,
                "receipt_id_high_water": high_water_row[0],
            },
            allowed_tables=spec.allowed_tables,
        )
        connection.rollback()
        began = False
        connection.row_factory = original_row_factory
        connection.text_factory = original_text_factory
        factories_restored = True
        return state, selected, records
    except CollectorContinuityError:
        raise
    except (sqlite3.Error, TypeError, UnicodeError, ValueError) as exc:
        raise CollectorContinuityError("collector raw verification snapshot cannot be computed") from exc
    finally:
        cleanup_error: BaseException | None = None
        if began or connection.in_transaction:
            try:
                connection.rollback()
            except sqlite3.Error as exc:
                cleanup_error = exc
            else:
                if connection.in_transaction:
                    cleanup_error = CollectorContinuityError("collector raw verification transaction did not close")
        if not factories_restored:
            try:
                connection.row_factory = original_row_factory
                connection.text_factory = original_text_factory
            except (AttributeError, TypeError) as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise CollectorContinuityError("collector raw verification connection restoration failed") from cleanup_error


def _prior_receipt_rows(
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]], high_water: int
) -> dict[str, tuple[dict[str, object], ...]]:
    """Freeze every pre-attempt receipt and its references, not just selected rows."""

    result: dict[str, tuple[dict[str, object], ...]] = {}
    for table, rows in records.items():
        kept: list[dict[str, object]] = []
        for row, _ in rows:
            receipt_id = row.get("receipt_id")
            if table == "collection_receipts":
                receipt_id = row.get("receipt_id")
            if type(receipt_id) is int and receipt_id <= high_water:
                kept.append(dict(row))
        result[table] = tuple(kept)
    return result


def _verify_rehydrated_baseline_prefix(
    baseline: CollectorStepBaseline,
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    spec: CollectorStepSpec,
) -> None:
    """Prove the persisted in-scope before-image remains an exact old prefix."""

    high_water = baseline.step_state.get("receipt_id_high_water")
    if type(high_water) is not int or high_water < 0:
        raise CollectorContinuityError("collector recovery receipt high water is invalid")
    for table, expected_rows in baseline.selector_rows.items():
        columns, primary_key = _collector_table_columns(table)
        current = {
            tuple(row[column] for column in primary_key): dict(row)
            for row, _ in records[table]
        }
        for expected in expected_rows:
            if table == "sync_coverage":
                # Coverage may widen monotonically; prices validate that exact rule.
                continue
            receipt_id = expected.get("receipt_id")
            if type(receipt_id) is not int or receipt_id > high_water:
                raise CollectorContinuityError("collector recovery prior selector row is invalid")
            key = tuple(expected[column] for column in primary_key)
            actual = current.get(key)
            if actual is None or canonical_json_bytes(actual) != canonical_json_bytes(expected):
                raise CollectorContinuityError("collector recovery changed prior selector evidence")
    _validate_baseline_selector_evidence(
        baseline, records, spec, _validate_raw_step_spec(spec)
    )


def _validate_baseline_selector_evidence(
    baseline: CollectorStepBaseline,
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    spec: CollectorStepSpec,
    frozen_schedule: _FrozenCollectorStepSchedule,
) -> None:
    """Validate existing in-scope evidence before it becomes a retry baseline."""

    receipt_rows = {
        row["receipt_id"]: dict(row)
        for row, _ in records["collection_receipts"]
        if type(row.get("receipt_id")) is int
    }
    receipt_ids = {
        row["receipt_id"]
        for rows in baseline.selector_rows.values()
        for row in rows
        if row.get("receipt_id") is not None
    }
    if any(type(receipt_id) is not int or receipt_id not in receipt_rows for receipt_id in receipt_ids):
        raise CollectorContinuityError("collector raw baseline receipt reference is invalid")
    payloads = {
        receipt_id: _receipt_payload(receipt_rows[receipt_id], spec)
        for receipt_id in receipt_ids
    }
    if spec.step_id in {"pre_open_context", "post_close_context"} and receipt_ids:
        if len(receipt_ids) != 1:
            raise CollectorContinuityError("collector raw baseline context is not atomic")
        receipt_id = next(iter(receipt_ids))
        _verify_context_raw(records, receipt_id, payloads[receipt_id][0], payloads[receipt_id][1], spec)
    elif spec.step_id == "pre_open_corporate_actions" and receipt_ids:
        if len(receipt_ids) != 1:
            raise CollectorContinuityError("collector raw baseline actions are not atomic")
        receipt_id = next(iter(receipt_ids))
        _verify_actions_raw(records, receipt_id, payloads[receipt_id][0], payloads[receipt_id][1], spec)



def _capture_collector_step_baseline_from_connection(
    connection: sqlite3.Connection, spec: CollectorStepSpec
) -> CollectorStepBaseline:
    """Capture the before-image used by the pure raw postcondition verifier."""

    frozen_schedule = _validate_raw_step_spec(spec)
    state, selected, records = _raw_snapshot_from_connection(connection, spec)
    high_water = state["receipt_id_high_water"]
    if type(high_water) is not int:
        raise CollectorContinuityError("collector raw baseline high water is invalid")
    baseline = CollectorStepBaseline(
        step_state=state,
        selector_rows=selected,
        _prior_receipt_rows=_prior_receipt_rows(records, high_water),
    )
    _validate_baseline_selector_evidence(baseline, records, spec, frozen_schedule)
    return baseline


def capture_collector_step_baseline(
    token: CollectorReadToken, spec: CollectorStepSpec
) -> CollectorStepBaseline:
    """Capture a raw-verification baseline through an opaque token only."""

    require_collector_continuity_health()
    with _borrow_registered_collector_read_connection(token, spec) as connection:
        return _capture_collector_step_baseline_from_connection(connection, spec)


def _raw_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CollectorContinuityError(f"collector raw {label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CollectorContinuityError(f"collector raw {label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise CollectorContinuityError(f"collector raw {label} timestamp is invalid")
    return parsed


def _receipt_payload(
    row: Mapping[str, object],
    spec: CollectorStepSpec,
    start: datetime | None = None,
    finish: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object], str, str]:
    receipt_id = row.get("receipt_id")
    if type(receipt_id) is not int or receipt_id <= 0 or row.get("source") != spec.selector_source:
        raise CollectorContinuityError("collector raw receipt identity is invalid")
    observed = _raw_timestamp(row.get("observed_at"), "receipt observed_at")
    created = _raw_timestamp(row.get("created_at"), "receipt created_at")
    if (start is None) != (finish is None):
        raise CollectorContinuityError("collector raw receipt attempt window is invalid")
    if start is not None and finish is not None and not (
        start <= observed <= finish and start <= created <= finish
    ):
        raise CollectorContinuityError("collector raw receipt is outside attempt window")
    if created < observed:
        raise CollectorContinuityError("collector raw receipt creation precedes observation")
    observed_local = observed.astimezone(_SHANGHAI)
    created_local = created.astimezone(_SHANGHAI)
    if (
        observed_local.date().isoformat() != spec.session
        or created_local.date().isoformat() != spec.session
    ):
        raise CollectorContinuityError("collector raw receipt session is invalid")
    if spec.phase == "pre_open":
        in_phase = (
            _PREOPEN_START <= observed_local.time() < _PREOPEN_END
            and _PREOPEN_START <= created_local.time() < _PREOPEN_END
        )
    else:
        in_phase = (
            observed_local.time() >= _POST_CLOSE_START
            and created_local.time() >= _POST_CLOSE_START
        )
    if not in_phase:
        raise CollectorContinuityError("collector raw receipt phase is invalid")
    request_raw = row.get("request_json")
    response_raw = row.get("response_json")
    if not isinstance(request_raw, str) or not isinstance(response_raw, str):
        raise CollectorContinuityError("collector raw receipt JSON is invalid")
    request = decode_canonical_json_object(request_raw.encode("ascii"))
    response = decode_canonical_json_object(response_raw.encode("ascii"))
    response_sha256 = hashlib.sha256(response_raw.encode("ascii")).hexdigest()
    if row.get("response_sha256") != response_sha256:
        raise CollectorContinuityError("collector raw response hash is invalid")
    return request, response, canonical_json_sha256(request), response_sha256


def _receipt_references(
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]], receipt_id: int
) -> dict[str, tuple[dict[str, object], ...]]:
    return {
        table: tuple(dict(row) for row, _ in rows if row.get("receipt_id") == receipt_id)
        for table, rows in records.items()
        if table != "collection_receipts"
    }


def _verify_raw_delta(
    baseline: CollectorStepBaseline,
    after: Mapping[str, object],
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    spec: CollectorStepSpec,
) -> tuple[dict[int, dict[str, object]], bool]:
    """Reject cross-step mutation and return each attributable new receipt."""

    before = validate_collector_step_state(baseline.step_state, allowed_tables=spec.allowed_tables)
    current = validate_collector_step_state(after, allowed_tables=spec.allowed_tables)
    for table in COLLECTOR_STATE_TABLES:
        if table not in spec.allowed_tables and before["table_sha256"][table] != current["table_sha256"][table]:
            raise CollectorContinuityError("collector raw changed a disallowed table")
    for table in spec.allowed_tables:
        if before["outside_scope_sha256"][table] != current["outside_scope_sha256"][table]:
            raise CollectorContinuityError("collector raw changed an allowed-table complement")
    high_water = before["receipt_id_high_water"]
    current_high_water = current["receipt_id_high_water"]
    if type(high_water) is not int or type(current_high_water) is not int or current_high_water < high_water:
        raise CollectorContinuityError("collector raw receipt high water drifted")
    if baseline._rehydrated:
        _verify_rehydrated_baseline_prefix(baseline, records, spec)
    else:
        prior = _prior_receipt_rows(records, high_water)
        if baseline._prior_receipt_rows and prior != baseline._prior_receipt_rows:
            raise CollectorContinuityError("collector raw changed a prior receipt or reference")
    receipts: dict[int, dict[str, object]] = {}
    for row, _ in records["collection_receipts"]:
        receipt_id = row.get("receipt_id")
        if type(receipt_id) is int and receipt_id > high_water:
            receipts[receipt_id] = dict(row)
    for receipt_id in receipts:
        references = _receipt_references(records, receipt_id)
        linked = [row for rows in references.values() for row in rows]
        if not linked or any(row.get("source") != spec.selector_source for row in linked):
            raise CollectorContinuityError("collector raw receipt is orphaned or foreign")
        for table, rows in references.items():
            for row in rows:
                if not _step_row_selected(table, row, spec, _validate_raw_step_spec(spec)):
                    raise CollectorContinuityError("collector raw receipt has a foreign reference")
    changed = before["collector_state_sha256"] != current["collector_state_sha256"]
    return receipts, changed


def _exact_rows(rows: Sequence[Mapping[str, object]], expected: Sequence[Mapping[str, object]]) -> bool:
    return sorted(canonical_json_bytes(dict(row)) for row in rows) == sorted(
        canonical_json_bytes(dict(row)) for row in expected
    )


def _selected_row_map(
    rows: Sequence[Mapping[str, object]], key: str, label: str
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or value in result:
            raise CollectorContinuityError(f"collector raw {label} selector is invalid")
        result[value] = dict(row)
    return result


def _verify_price_baseline_immutability(
    baseline: CollectorStepBaseline,
    selected: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """Require old finalized rows and coverage to remain an append-only prefix."""

    before_daily = _selected_row_map(baseline.selector_rows.get("daily", ()), "code", "daily")
    after_daily = _selected_row_map(selected.get("daily", ()), "code", "daily")
    for symbol, before in before_daily.items():
        after = after_daily.get(symbol)
        if after is None or canonical_json_bytes(after) != canonical_json_bytes(before):
            raise CollectorContinuityError("collector raw changed an existing daily row")

    before_coverage = _selected_row_map(
        baseline.selector_rows.get("sync_coverage", ()), "code", "sync coverage"
    )
    after_coverage = _selected_row_map(
        selected.get("sync_coverage", ()), "code", "sync coverage"
    )
    for symbol, before in before_coverage.items():
        after = after_coverage.get(symbol)
        if after is None:
            raise CollectorContinuityError("collector raw removed existing sync coverage")
        if after.get("start_date") != before.get("start_date"):
            raise CollectorContinuityError("collector raw changed sync coverage start")
        before_end = before.get("end_date")
        after_end = after.get("end_date")
        if not isinstance(before_end, str) or not isinstance(after_end, str) or after_end < before_end:
            raise CollectorContinuityError("collector raw shrank sync coverage")
        if after_end == before_end and after.get("retrieved_at") != before.get("retrieved_at"):
            raise CollectorContinuityError("collector raw retimestamped exact sync coverage")


def _verify_context_raw(
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    receipt_id: int,
    request: Mapping[str, object],
    response: Mapping[str, object],
    spec: CollectorStepSpec,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from .forward_context import _COUNT_URL, _PAGE_SIZE, _PAGE_URL, _market_symbol, _parse_raw_pages, _status_values

    require_exact_keys(request, {"count_url", "page_url", "node", "page_size"}, "collector raw context request")
    if request != {"count_url": _COUNT_URL, "page_url": _PAGE_URL, "node": "hs_a", "page_size": _PAGE_SIZE}:
        raise CollectorContinuityError("collector raw context request is invalid")
    require_exact_keys(response, {"advertised_count", "count_raw", "raw_pages"}, "collector raw context response")
    raw_rows = _parse_raw_pages(response["raw_pages"])
    if type(response["advertised_count"]) is not int or response["advertised_count"] != len(raw_rows):
        raise CollectorContinuityError("collector raw context advertised count is invalid")
    if not isinstance(response["count_raw"], str) or int(response["count_raw"].strip().strip('"')) != len(raw_rows):
        raise CollectorContinuityError("collector raw context count response is invalid")
    by_symbol: dict[str, dict[str, object]] = {}
    for raw in raw_rows:
        symbol = _market_symbol(raw.get("symbol"))
        if symbol in by_symbol:
            raise CollectorContinuityError("collector raw context response has duplicate symbols")
        by_symbol[symbol] = dict(raw)
    if not set(spec.symbols) <= set(by_symbol):
        raise CollectorContinuityError("collector raw context response misses cohort")
    references = _receipt_references(records, receipt_id)
    contexts = references["forward_context_observations"]
    universe = references["forward_universe_observations"]
    statuses = references["forward_status_observations"]
    if len(contexts) != 1 or len(universe) != len(by_symbol) or len(statuses) != len(spec.symbols):
        raise CollectorContinuityError("collector raw context rows are incomplete")
    context = contexts[0]
    receipt_row = next(row for row, _ in records["collection_receipts"] if row.get("receipt_id") == receipt_id)
    observed_at = receipt_row["observed_at"]
    if spec.phase == "pre_open":
        timing = {
            "decision_available_at": observed_at,
            "outcome_observed_at": None,
            "finalized_at": None,
        }
    else:
        timing = {
            "decision_available_at": None,
            "outcome_observed_at": observed_at,
            "finalized_at": observed_at,
        }
    expected_context = {
        "effective_date": spec.session, "observation_phase": spec.phase, "source": spec.selector_source,
        "receipt_id": receipt_id, **timing,
    }
    if context != expected_context:
        raise CollectorContinuityError("collector raw context observation is invalid")
    expected_universe = [
        {"effective_date": spec.session, "observation_phase": spec.phase, "symbol": symbol, "is_member": 1,
         "source": spec.selector_source, "receipt_id": receipt_id}
        for symbol in sorted(by_symbol)
    ]
    if not _exact_rows(universe, expected_universe):
        raise CollectorContinuityError("collector raw context universe is invalid")
    expected_status = []
    for symbol in spec.symbols:
        name, listing_status, board, is_st, is_suspended = _status_values(symbol, by_symbol[symbol])
        expected_status.append({
            "effective_date": spec.session, "observation_phase": spec.phase, "symbol": symbol,
            "name": name, "listing_status": listing_status, "board": board, "is_st": is_st,
            "is_suspended": is_suspended, "source": spec.selector_source, "receipt_id": receipt_id,
        })
    if not _exact_rows(statuses, expected_status):
        raise CollectorContinuityError("collector raw context status is invalid")
    return tuple(spec.symbols), ()


def _verify_actions_raw(
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    receipt_id: int,
    request: Mapping[str, object],
    response: Mapping[str, object],
    spec: CollectorStepSpec,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from .forward_corporate_actions import _event_fields, _source_rows

    require_exact_keys(request, {"symbols", "observation_date", "years"}, "collector raw actions request")
    years = [date.fromisoformat(spec.session).year - 1, date.fromisoformat(spec.session).year]
    if request != {"symbols": list(spec.symbols), "observation_date": spec.session, "years": years}:
        raise CollectorContinuityError("collector raw actions request is invalid")
    require_exact_keys(response, {"symbols"}, "collector raw actions response")
    response_symbols = response["symbols"]
    if (
        not isinstance(response_symbols, Mapping)
        or set(response_symbols) != set(spec.symbols)
        or any(type(symbol) is not str for symbol in response_symbols)
    ):
        raise CollectorContinuityError("collector raw actions response is invalid")
    for symbol in spec.symbols:
        batches = response_symbols.get(symbol)
        if not isinstance(batches, list) or len(batches) != 2:
            raise CollectorContinuityError("collector raw actions batches are invalid")
        if any(not isinstance(batch, Mapping) or set(batch) != {"year", "fields", "rows"} for batch in batches):
            raise CollectorContinuityError("collector raw actions batches are invalid")
        if [batch["year"] for batch in batches] != years:
            raise CollectorContinuityError("collector raw actions batch years are invalid")
        for batch in batches:
            fields = batch["fields"]
            rows = batch["rows"]
            if (
                not isinstance(fields, list)
                or not fields
                or any(type(field) is not str or not field for field in fields)
                or len(set(fields)) != len(fields)
                or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != len(fields) for row in rows)
            ):
                raise CollectorContinuityError("collector raw actions batch payload is invalid")
    try:
        raw = _source_rows(response)
    except (TypeError, ValueError) as exc:
        raise CollectorContinuityError("collector raw actions response is invalid") from exc
    if tuple(sorted(raw)) != spec.symbols:
        raise CollectorContinuityError("collector raw actions response cohort is invalid")
    references = _receipt_references(records, receipt_id)
    coverage = references["forward_corporate_action_coverage"]
    actions = references["forward_corporate_actions"]
    if len(coverage) != len(spec.symbols):
        raise CollectorContinuityError("collector raw action coverage is incomplete")
    expected_coverage = []
    expected_actions = []
    receipt_row = next(row for row, _ in records["collection_receipts"] if row.get("receipt_id") == receipt_id)
    observed_at = receipt_row["observed_at"]
    for symbol in spec.symbols:
        rows = raw[symbol]
        expected_coverage.append({"observation_date": spec.session, "symbol": symbol, "available_at": observed_at,
                                  "source": spec.selector_source, "receipt_id": receipt_id, "event_count": len(rows)})
        for source_row in rows:
            payload = canonical_json_bytes(source_row).decode("ascii")
            effective, announcement = _event_fields(source_row)
            expected_actions.append({
                "observation_date": spec.session, "symbol": symbol,
                "event_id": hashlib.sha256(payload.encode("ascii")).hexdigest(), "effective_date": effective,
                "announcement_date": announcement, "payload_json": payload, "available_at": observed_at,
                "source": spec.selector_source, "receipt_id": receipt_id,
            })
    if not _exact_rows(coverage, expected_coverage) or not _exact_rows(actions, expected_actions):
        raise CollectorContinuityError("collector raw actions reconstruction is invalid")
    return tuple(spec.symbols), ()


def _expected_price_receipt_gap(
    prior_coverage: Mapping[str, object] | None,
    frozen_schedule: _FrozenCollectorStepSchedule,
    spec: CollectorStepSpec,
) -> tuple[str, str] | None:
    """Derive the one exact request range justified by a baseline coverage row."""

    if prior_coverage is None:
        return frozen_schedule.cohort_start, spec.session
    start = prior_coverage.get("start_date")
    end = prior_coverage.get("end_date")
    if start != frozen_schedule.cohort_start or not isinstance(end, str):
        raise CollectorContinuityError("collector raw baseline price coverage is invalid")
    try:
        end_date = date.fromisoformat(end)
        session_date = date.fromisoformat(spec.session)
    except ValueError as exc:
        raise CollectorContinuityError("collector raw baseline price coverage is invalid") from exc
    if end_date > session_date:
        raise CollectorContinuityError("collector raw baseline price coverage exceeds session")
    if end_date == session_date:
        return None
    return (end_date + timedelta(days=1)).isoformat(), spec.session


def _verify_prices_raw(
    records: Mapping[str, Sequence[tuple[dict[str, object], list[str]]]],
    receipts: Mapping[int, Mapping[str, object]],
    baseline: CollectorStepBaseline,
    spec: CollectorStepSpec,
    frozen_schedule: _FrozenCollectorStepSchedule,
    *,
    attempt_start: datetime | None = None,
    attempt_finish: datetime | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], Literal["complete", "partial_prices"]]:
    from .fetch_tencent import _TENCENT, parse_tencent_bar
    from .ticker import to_tencent

    all_daily = [dict(row) for row, _ in records["daily"] if _step_row_selected("daily", row, spec, frozen_schedule)]
    all_coverage = [dict(row) for row, _ in records["sync_coverage"] if _step_row_selected("sync_coverage", row, spec, frozen_schedule)]
    selected = {"daily": tuple(all_daily), "sync_coverage": tuple(all_coverage)}
    _verify_price_baseline_immutability(baseline, selected)
    daily_by_symbol = _selected_row_map(all_daily, "code", "daily")
    coverage_by_symbol = _selected_row_map(all_coverage, "code", "sync coverage")
    baseline_daily = _selected_row_map(
        baseline.selector_rows.get("daily", ()), "code", "daily"
    )
    baseline_coverage = _selected_row_map(
        baseline.selector_rows.get("sync_coverage", ()), "code", "sync coverage"
    )
    receipt_rows = {
        row["receipt_id"]: dict(row)
        for row, _ in records["collection_receipts"]
        if type(row.get("receipt_id")) is int
    }
    verified: list[str] = []
    for symbol, row in daily_by_symbol.items():
        if row.get("date") != spec.session or row.get("source") != "tencent" or row.get("adjustment_mode") != "raw" or row.get("adjustment_version") != frozen_schedule.adjustment_version or row.get("is_final") != 1:
            raise CollectorContinuityError("collector raw price identity is invalid")
        verified.append(symbol)
    for symbol, row in coverage_by_symbol.items():
        if (
            row.get("start_date") != frozen_schedule.cohort_start
            or not isinstance(row.get("end_date"), str)
            or not frozen_schedule.cohort_start <= row["end_date"] <= spec.session
            or row.get("source") != "tencent"
            or row.get("adjustment_mode") != "raw"
            or row.get("adjustment_version") != frozen_schedule.adjustment_version
        ):
            raise CollectorContinuityError("collector raw price coverage is invalid")
        prior = baseline_coverage.get(symbol)
        if (
            (prior is None or row["end_date"] > prior.get("end_date", ""))
            and symbol not in daily_by_symbol
        ):
            raise CollectorContinuityError(
                "collector raw price coverage has no verified finalized daily row"
            )
    for symbol, row in daily_by_symbol.items():
        receipt_id = row.get("receipt_id")
        if type(receipt_id) is not int or receipt_id not in receipt_rows:
            raise CollectorContinuityError("collector raw price receipt is missing")
        receipt = receipt_rows[receipt_id]
        is_new = receipt_id in receipts
        request, response, _, _ = _receipt_payload(
            receipt,
            spec,
            attempt_start if is_new else None,
            attempt_finish if is_new else None,
        )
        references = _receipt_references(records, receipt_id)
        rows = references["daily"]
        if len(rows) != 1 or any(other for table, values in references.items() if table != "daily" for other in values):
            raise CollectorContinuityError("collector raw price receipt must bind one daily row")
        if rows[0] != row or symbol not in spec.symbols:
            raise CollectorContinuityError("collector raw price symbol is invalid")
        if is_new and symbol in baseline_daily:
            raise CollectorContinuityError("collector raw price refetched a verified symbol")
        high_water = baseline.step_state.get("receipt_id_high_water")
        if (
            not is_new
            and (type(high_water) is not int or receipt_id > high_water)
        ):
            raise CollectorContinuityError("collector raw old price receipt is not baseline-bound")
        prior_coverage = next(
            (
                item for item in baseline.selector_rows.get("sync_coverage", ())
                if item.get("code") == symbol
                and item.get("source") == "tencent"
                and item.get("adjustment_mode") == "raw"
                and item.get("adjustment_version") == frozen_schedule.adjustment_version
            ),
            None,
        )
        require_exact_keys(request, {"method", "url", "start_date", "end_date"}, "collector raw price request")
        expected_url = _TENCENT.format(sym=to_tencent(symbol))
        expected_gap = _expected_price_receipt_gap(
            prior_coverage, frozen_schedule, spec
        )
        if is_new:
            if expected_gap is None or request != {
                "method": "qt", "url": expected_url,
                "start_date": expected_gap[0], "end_date": expected_gap[1],
            }:
                raise CollectorContinuityError("collector raw price gap is invalid")
        else:
            expected_start = (
                frozen_schedule.cohort_start
                if expected_gap is None
                else expected_gap[0]
            )
            if request != {
                "method": "qt", "url": expected_url,
                "start_date": expected_start, "end_date": spec.session,
            }:
                raise CollectorContinuityError("collector raw price request is invalid")
        try:
            date.fromisoformat(str(request["start_date"]))
        except ValueError as exc:
            raise CollectorContinuityError("collector raw price request is invalid") from exc
        require_exact_keys(response, {"raw", "fields", "rows"}, "collector raw price response")
        if response.get("fields") != "date,open,high,low,close,volume" or not isinstance(response.get("raw"), str) or not isinstance(response.get("rows"), list):
            raise CollectorContinuityError("collector raw price response is invalid")
        expected_prefix = f'v_{to_tencent(symbol)}="'
        raw = response["raw"]
        if not raw.startswith(expected_prefix) or not raw.endswith('";') or raw.count('"') != 2:
            raise CollectorContinuityError("collector raw Tencent response envelope is invalid")
        parsed = parse_tencent_bar(response["raw"], symbol)
        if parsed is None or parsed.get("date") != spec.session:
            raise CollectorContinuityError("collector raw price response is not finalized current data")
        expected_response_rows = [[parsed["date"], str(parsed["open"]), str(parsed["high"]), str(parsed["low"]), str(parsed["close"]), str(float(parsed["volume"]) * 100.0)]]
        if response["rows"] != expected_response_rows:
            raise CollectorContinuityError("collector raw price response reconstruction is invalid")
        expected = dict(parsed)
        expected["volume"] = float(expected["volume"]) * 100.0
        for field_name in ("date", "open", "high", "low", "close", "volume"):
            if row.get(field_name) != expected[field_name]:
                raise CollectorContinuityError("collector raw daily reconstruction is invalid")
        if row.get("retrieved_at") != receipt.get("observed_at"):
            raise CollectorContinuityError("collector raw daily receipt binding is invalid")
    verified_symbols = tuple(sorted(set(verified)))
    missing = tuple(
        symbol for symbol in spec.symbols
        if symbol not in daily_by_symbol
        or symbol not in coverage_by_symbol
        or coverage_by_symbol[symbol].get("end_date") != spec.session
    )
    raw_class: Literal["complete", "partial_prices"] = (
        "complete" if not missing else "partial_prices"
    )
    return verified_symbols, missing, raw_class


def _raw_result(
    raw_class: Literal["complete", "unchanged", "partial_prices", "forbidden"],
    code: str,
    state: dict[str, object],
    *,
    receipt_ids: Sequence[int] = (),
    requests: Sequence[str] = (),
    responses: Sequence[str] = (),
    verified: Sequence[str] = (),
    missing: Sequence[str] = (),
) -> CollectorRawPostconditionResult:
    return CollectorRawPostconditionResult(
        verifier_id=_RAW_POSTCONDITION_SCHEMA,
        raw_class=raw_class,
        code=code,
        retryable=raw_class in {"unchanged", "partial_prices"},
        step_state_after=state,
        new_receipt_ids=tuple(receipt_ids),
        request_sha256=tuple(requests),
        response_sha256=tuple(responses),
        verified_symbols=tuple(verified),
        missing_symbols=tuple(missing),
    )


def _verify_collector_raw_postcondition_from_connection(
    connection: sqlite3.Connection,
    spec: CollectorStepSpec,
    baseline: CollectorStepBaseline,
    *,
    attempt_started_at: str,
    attempt_finished_at: str,
) -> CollectorRawPostconditionResult:
    """Verify raw evidence without executing a provider, child, or writer."""

    _validate_raw_step_spec(spec)
    if not isinstance(baseline, CollectorStepBaseline):
        raise CollectorContinuityError("collector raw baseline is invalid")
    start = _raw_timestamp(attempt_started_at, "attempt start")
    finish = _raw_timestamp(attempt_finished_at, "attempt finish")
    if finish < start:
        raise CollectorContinuityError("collector raw attempt timestamps are invalid")
    after, _, records = _raw_snapshot_from_connection(connection, spec)
    try:
        receipts, changed = _verify_raw_delta(baseline, after, records, spec)
        if not changed:
            return _raw_result("unchanged", "no_persisted_change", after)
        frozen_schedule = _validate_raw_step_spec(spec)
        request_hashes: list[str] = []
        response_hashes: list[str] = []
        payloads: dict[int, tuple[dict[str, object], dict[str, object]]] = {}
        for receipt_id in sorted(receipts):
            request, response, request_hash, response_hash = _receipt_payload(receipts[receipt_id], spec, start, finish)
            payloads[receipt_id] = (request, response)
            request_hashes.append(request_hash)
            response_hashes.append(response_hash)
        if spec.step_id in {"pre_open_context", "post_close_context"}:
            if len(receipts) != 1:
                raise CollectorContinuityError("collector raw context step is not atomic")
            receipt_id = next(iter(receipts))
            verified, missing = _verify_context_raw(records, receipt_id, payloads[receipt_id][0], payloads[receipt_id][1], spec)
            return _raw_result("complete", "context_complete", after, receipt_ids=sorted(receipts), requests=request_hashes, responses=response_hashes, verified=verified, missing=missing)
        if spec.step_id == "pre_open_corporate_actions":
            if len(receipts) != 1:
                raise CollectorContinuityError("collector raw actions step is not atomic")
            receipt_id = next(iter(receipts))
            verified, missing = _verify_actions_raw(records, receipt_id, payloads[receipt_id][0], payloads[receipt_id][1], spec)
            return _raw_result("complete", "corporate_actions_complete", after, receipt_ids=sorted(receipts), requests=request_hashes, responses=response_hashes, verified=verified, missing=missing)
        if spec.step_id == "post_close_prices":
            verified, missing, raw_class = _verify_prices_raw(
                records,
                receipts,
                baseline,
                spec,
                frozen_schedule,
                attempt_start=start,
                attempt_finish=finish,
            )
            return _raw_result(raw_class, "prices_complete" if raw_class == "complete" else "prices_partial", after, receipt_ids=sorted(receipts), requests=request_hashes, responses=response_hashes, verified=verified, missing=missing)
        raise CollectorContinuityError("collector raw step identity is invalid")
    except CollectorContinuityError:
        return _raw_result("forbidden", "forbidden_evidence", after)


def verify_collector_raw_postcondition(
    token: CollectorReadToken,
    spec: CollectorStepSpec,
    baseline: CollectorStepBaseline,
    *,
    attempt_started_at: str,
    attempt_finished_at: str,
) -> CollectorRawPostconditionResult:
    """Verify persisted raw evidence through an opaque token only."""

    require_collector_continuity_health()
    with _borrow_registered_collector_read_connection(token, spec) as connection:
        return _verify_collector_raw_postcondition_from_connection(
            connection,
            spec,
            baseline,
            attempt_started_at=attempt_started_at,
            attempt_finished_at=attempt_finished_at,
        )


def evaluate_collector_step_attempt(
    result: CollectorRawPostconditionResult, *, returncode: int
) -> Literal["complete", "retryable_failure", "nonretryable_failure"]:
    """Classify the raw proof and process result without authorizing a writer."""

    require_collector_continuity_health()
    if not isinstance(result, CollectorRawPostconditionResult) or type(returncode) is not int:
        raise CollectorContinuityError("collector raw attempt classification is invalid")
    if result.raw_class == "complete":
        return "complete" if returncode == 0 else "nonretryable_failure"
    if result.raw_class in {"unchanged", "partial_prices"}:
        return "retryable_failure"
    if result.raw_class == "forbidden":
        return "nonretryable_failure"
    raise CollectorContinuityError("collector raw attempt classification is invalid")


# Attempt protocol ----------------------------------------------------------

_COLLECTOR_ATTEMPT_NONCE_BYTES: Final = 32
_COLLECTOR_ATTEMPT_OUTPUT_LIMIT: Final = 8 * 1024 * 1024
_COLLECTOR_CHILD_ENVIRONMENT: Final = (
    "STOCKDATA_COLLECTOR_REGISTRATION_FILE",
    "STOCKDATA_COLLECTOR_LEDGER_FILE",
    "STOCKDATA_COLLECTOR_LEDGER_IDENTITY",
    "STOCKDATA_COLLECTOR_ATTEMPT_ID",
    "STOCKDATA_COLLECTOR_SESSION",
    "STOCKDATA_COLLECTOR_PHASE",
    "STOCKDATA_COLLECTOR_STEP_ID",
    "STOCKDATA_COLLECTOR_STEP_ORDINAL",
    "STOCKDATA_COLLECTOR_COMMAND_SHA256",
    "STOCKDATA_COLLECTOR_REGISTRATION_SHA256",
    "STOCKDATA_COLLECTOR_DATABASE_UUID",
    "STOCKDATA_COLLECTOR_LEASE_FD",
    "STOCKDATA_COLLECTOR_PIPE_FD",
)
_COLLECTOR_CHILD_LEGACY_ENVIRONMENT: Final = frozenset(
    {
        "STOCKDATA_COLLECTOR_REGISTRATION_FILE",
        "STOCKDATA_COLLECTOR_ATTEMPT_ID",
        "STOCKDATA_COLLECTOR_LEASE_FD",
        "STOCKDATA_COLLECTOR_PIPE_FD",
    }
)


@dataclass
class _CollectorAttemptLaunch:
    """Private parent state held only until one terminal append or failure."""

    spec: CollectorStepSpec
    baseline: CollectorStepBaseline
    step_raw_before: dict[str, object]
    attempt_id: str
    started_at: str
    started_event_sha256: str
    nonce_sha256: str
    ledger_identity: PhysicalFileIdentity
    database_uuid: str
    nonce: bytes = field(repr=False)
    _nonce_buffer: bytearray = field(repr=False)


@dataclass(frozen=True)
class CollectorAttemptOutcome:
    """Audit-safe parent result; child output bodies are never retained."""

    step_id: str
    step_ordinal: int
    attempt_id: str
    terminal_event_sha256: str
    terminal_event_type: Literal["ATTEMPT_COMPLETED", "ATTEMPT_FAILED"]
    classification: str
    retryable: bool
    process_result_known: bool
    returncode: int | None
    raw_class: str


class CollectorWriteToken:
    """Opaque child-only authority for one active collector attempt."""

    __slots__ = ()

    def __copy__(self) -> "CollectorWriteToken":
        raise TypeError("collector writer tokens cannot be copied")

    def __deepcopy__(self, memo: object) -> "CollectorWriteToken":
        del memo
        raise TypeError("collector writer tokens cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("collector writer tokens cannot be serialized")

    def __reduce__(self) -> object:
        raise TypeError("collector writer tokens cannot be serialized")

    def __repr__(self) -> str:
        return "CollectorWriteToken()"


@dataclass
class _CollectorWriteBinding:
    token: CollectorWriteToken | None
    spec: CollectorStepSpec
    attempt_id: str
    started_event_sha256: str
    nonce_sha256: str
    nonce: bytearray = field(repr=False)
    lease_fd: int = -1
    owner_pid: int = field(default_factory=os.getpid)
    owner_thread_id: int = field(default_factory=threading.get_ident)
    state: Literal["OPEN", "CLOSED"] = "OPEN"


@dataclass(frozen=True)
class _CollectorProcessResult:
    process_result_known: bool
    returncode: int | None
    stdout_sha256: str | None
    stdout_bytes: int | None
    stderr_sha256: str | None
    stderr_bytes: int | None
    plumbing_failed: bool


@dataclass(frozen=True)
class _CollectorChildRun:
    """One parent-side launcher outcome without inventing a child result."""

    launch_state: Literal["not_invoked", "handle_obtained", "indeterminate"]
    process: _CollectorProcessResult | None


_COLLECTOR_WRITE_LOCK = threading.RLock()
_COLLECTOR_WRITE_BINDINGS: dict[CollectorWriteToken, _CollectorWriteBinding] = {}


def _collector_attempt_now() -> str:
    return datetime.now(_SHANGHAI).isoformat(timespec="microseconds")


def _clear_nonce(nonce: bytearray) -> None:
    for index in range(len(nonce)):
        nonce[index] = 0


def _close_descriptor_once(descriptor: int, *, label: str) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError as exc:
        raise CollectorContinuityError(f"collector {label} cannot be closed") from exc


def _attempt_history_for_spec(
    lease: CollectorPhaseLease,
    spec: CollectorStepSpec,
    *,
    enforce_phase_order: bool,
) -> tuple[dict[str, object], ...]:
    """Validate phase order and the exact ledger/database binding before start."""

    _validate_collector_step_spec(spec)
    registration = _read_bound_registration(spec.registration_file)
    prepared = registration["prepared"]
    if not isinstance(prepared, Mapping):
        raise CollectorContinuityError("collector attempt prepared authority is invalid")
    try:
        expected_ledger = PhysicalFileIdentity.from_dict(prepared["ledger_identity"])
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector attempt ledger authority is invalid") from exc
    if lease.verify() != expected_ledger:
        raise CollectorContinuityError("collector attempt lease identity drifted")
    history = _phase_ledger_history(lease)
    binding = history[1]["event"]
    if not isinstance(binding, Mapping) or binding.get("registration_sha256") != spec.registration_sha256:
        raise CollectorContinuityError("collector attempt registration binding drifted")
    completed: set[int] = set()
    active_attempt_id: str | None = None
    for entry in history[2:]:
        event_type = entry["event_type"]
        details = entry["event"]
        if not isinstance(details, Mapping):
            raise CollectorContinuityError("collector attempt ledger detail is invalid")
        if event_type == "ATTEMPT_STARTED":
            attempt_id = details.get("attempt_id")
            if active_attempt_id is not None or not isinstance(attempt_id, str):
                raise CollectorContinuityError("collector dangling attempt requires recovery")
            active_attempt_id = attempt_id
            continue
        if event_type.startswith("SQLITE_RECOVERY"):
            if active_attempt_id is None:
                raise CollectorContinuityError("collector recovery has no active attempt")
            continue
        if event_type not in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
            raise CollectorContinuityError("collector phase ledger event is invalid")
        if active_attempt_id is None or details.get("attempt_id") != active_attempt_id:
            raise CollectorContinuityError("collector dangling attempt requires recovery")
        ordinal = details.get("step_ordinal")
        if type(ordinal) is not int or ordinal > spec.step_ordinal:
            raise CollectorContinuityError("collector attempt phase order is invalid")
        if event_type == "ATTEMPT_COMPLETED":
            completed.add(ordinal)
        elif details.get("retryable") is not True:
            raise CollectorContinuityError("collector registration is quarantined")
        active_attempt_id = None
    if active_attempt_id is not None:
        raise CollectorContinuityError("collector dangling attempt requires recovery")
    if enforce_phase_order and any(
        ordinal not in completed for ordinal in range(spec.step_ordinal)
    ):
        raise CollectorContinuityError("collector attempt earlier step is incomplete")
    if spec.step_ordinal in completed:
        raise CollectorContinuityError("collector attempt step is already complete")
    return history


def _tail_state_after(history: Sequence[Mapping[str, object]]) -> str | None:
    tail = history[-1]
    if tail.get("event_type") in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
        details = tail.get("event")
        if isinstance(details, Mapping):
            value = details.get("state_after_sha256")
            if isinstance(value, str):
                return value
    return None


@dataclass(frozen=True)
class _CollectorDeleteJournal:
    identity: PhysicalFileIdentity
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class _DanglingCollectorAttempt:
    spec: CollectorStepSpec
    authority: _RegisteredCollectorReadAuthority
    start_event: dict[str, object]
    start: dict[str, object]
    recovery_start_event: dict[str, object] | None
    recovery_start: dict[str, object] | None
    recovery_terminal_event: dict[str, object] | None
    recovery_terminal: dict[str, object] | None


def _locate_dangling_collector_attempt(
    lease: CollectorPhaseLease, spec: CollectorStepSpec
) -> _DanglingCollectorAttempt | None:
    """Locate the only recoverable open attempt without opening SQLite."""

    require_collector_continuity_health()
    authority = _read_registered_collector_read_authority(spec)
    if lease.verify() != authority.ledger_identity:
        raise CollectorContinuityError("collector recovery lease identity drifted")
    verify_file_identity(authority.canonical_path, authority.expected_identity)
    history = _phase_ledger_history(lease)
    binding = history[1].get("event")
    if not isinstance(binding, Mapping) or binding.get("registration_sha256") != spec.registration_sha256:
        raise CollectorContinuityError("collector recovery registration binding drifted")

    start_event: dict[str, object] | None = None
    start: dict[str, object] | None = None
    recovery_start_event: dict[str, object] | None = None
    recovery_start: dict[str, object] | None = None
    recovery_terminal_event: dict[str, object] | None = None
    recovery_terminal: dict[str, object] | None = None
    for event in history[2:]:
        event_type = event.get("event_type")
        detail = event.get("event")
        if not isinstance(event_type, str) or not isinstance(detail, dict):
            raise CollectorContinuityError("collector recovery ledger detail is invalid")
        if event_type == "ATTEMPT_STARTED":
            start_event = event
            start = detail
            recovery_start_event = None
            recovery_start = None
            recovery_terminal_event = None
            recovery_terminal = None
        elif event_type == "SQLITE_RECOVERY_STARTED":
            if start is None:
                raise CollectorContinuityError("collector recovery has no open attempt")
            recovery_start_event = event
            recovery_start = detail
        elif event_type in {"SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"}:
            if start is None or recovery_start is None:
                raise CollectorContinuityError("collector recovery terminal has no open recovery")
            recovery_terminal_event = event
            recovery_terminal = detail
        elif event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
            start_event = None
            start = None
            recovery_start_event = None
            recovery_start = None
            recovery_terminal_event = None
            recovery_terminal = None
        else:
            raise CollectorContinuityError("collector recovery ledger event is invalid")
    if start is None or start_event is None:
        return None
    expected = {
        "registration_sha256": spec.registration_sha256,
        "database_uuid": authority.database_uuid,
        "session": spec.session,
        "phase": spec.phase,
        "step_id": spec.step_id,
        "step_ordinal": spec.step_ordinal,
        "command_sha256": spec.command_sha256,
    }
    if any(start.get(field) != value for field, value in expected.items()):
        raise CollectorContinuityError("collector dangling attempt does not match requested step")
    _validate_attempt_identity(start, require_nonce=True)
    if recovery_start is not None:
        if recovery_start_event is None:
            raise CollectorContinuityError("collector dangling recovery event is invalid")
        _validate_recovery_start_detail(recovery_start)
        if (
            recovery_start["attempt_id"] != start["attempt_id"]
            or recovery_start["attempt_started_event_sha256"]
            != start_event["event_sha256"]
        ):
            raise CollectorContinuityError("collector dangling recovery attempt drifted")
    if recovery_terminal is not None:
        if recovery_terminal_event is None or recovery_start_event is None:
            raise CollectorContinuityError("collector dangling recovery terminal is invalid")
        _validate_recovery_terminal_detail(
            str(recovery_terminal_event["event_type"]), recovery_terminal
        )
        if (
            recovery_terminal["recovery_started_event_sha256"]
            != recovery_start_event["event_sha256"]
        ):
            raise CollectorContinuityError("collector dangling recovery terminal drifted")
    return _DanglingCollectorAttempt(
        spec=spec,
        authority=authority,
        start_event=start_event,
        start=start,
        recovery_start_event=recovery_start_event,
        recovery_start=recovery_start,
        recovery_terminal_event=recovery_terminal_event,
        recovery_terminal=recovery_terminal,
    )


def _reject_recovery_wal_shm_sidecars(database_path: str) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            os.lstat(database_path + suffix)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CollectorContinuityError(
                "collector recovery sidecar cannot be inspected"
            ) from exc
        raise CollectorContinuityError("collector recovery WAL/SHM sidecars are forbidden")


def _read_collector_delete_journal(
    database_identity: PhysicalFileIdentity,
) -> _CollectorDeleteJournal | None:
    journal_path = database_identity.canonical_path + "-journal"
    try:
        status = os.lstat(journal_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CollectorContinuityError("collector recovery journal cannot be inspected") from exc
    if not stat.S_ISREG(status.st_mode):
        raise CollectorContinuityError("collector recovery journal must be a regular file")
    opened = open_nofollow_regular(journal_path)
    try:
        identity = opened.identity
        if (
            identity.parent_st_dev != database_identity.parent_st_dev
            or identity.parent_st_ino != database_identity.parent_st_ino
        ):
            raise CollectorContinuityError("collector recovery journal parent drifted")
        before = os.fstat(opened.descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
            raise CollectorContinuityError("collector recovery journal is invalid")
        digest = hashlib.sha256()
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(opened.descriptor, min(64 * 1024, before.st_size - offset), offset)
            if not chunk:
                raise CollectorContinuityError("collector recovery journal was truncated")
            digest.update(chunk)
            offset += len(chunk)
        after = os.fstat(opened.descriptor)
        if (
            after.st_size != before.st_size
            or int(after.st_dev) != identity.file_st_dev
            or int(after.st_ino) != identity.file_st_ino
        ):
            raise CollectorContinuityError("collector recovery journal changed while reading")
        verify_file_identity(journal_path, identity)
        return _CollectorDeleteJournal(identity, before.st_size, digest.hexdigest())
    except OSError as exc:
        raise CollectorContinuityError("collector recovery journal cannot be read") from exc
    finally:
        opened.close()


def _observe_collector_delete_journal(
    lease: CollectorPhaseLease, spec: CollectorStepSpec
) -> _CollectorDeleteJournal | None:
    """Observe the one admissible DELETE rollback journal before SQLite opens."""

    dangling = _locate_dangling_collector_attempt(lease, spec)
    if dangling is None:
        raise CollectorContinuityError("collector recovery has no dangling attempt")
    _reject_recovery_wal_shm_sidecars(dangling.authority.canonical_path)
    return _read_collector_delete_journal(dangling.authority.expected_identity)


def _recovery_start_event(
    dangling: _DanglingCollectorAttempt,
    journal: _CollectorDeleteJournal,
    *,
    started_at: str,
) -> dict[str, object]:
    _raw_timestamp(started_at, "recovery start")
    detail = {
        "registration_sha256": dangling.spec.registration_sha256,
        "database_uuid": dangling.authority.database_uuid,
        "state_before_sha256": dangling.start["state_before_sha256"],
        "attempt_id": dangling.start["attempt_id"],
        "attempt_started_event_sha256": dangling.start_event["event_sha256"],
        "recovery_id": secrets.token_hex(32),
        "recovery_kind": "hot_delete_journal",
        "journal_identity": journal.identity.to_dict(),
        "journal_bytes": journal.byte_count,
        "journal_sha256": journal.sha256,
        "started_at": started_at,
    }
    _validate_recovery_start_detail(detail)
    return detail


def _recovery_terminal_event(
    recovery_start: Mapping[str, object],
    recovery_started_event_sha256: str,
    *,
    event_type: Literal["SQLITE_RECOVERY_COMPLETED", "SQLITE_RECOVERY_FAILED"],
    step_state_after: Mapping[str, object],
    finished_at: str,
) -> dict[str, object]:
    detail = dict(recovery_start)
    detail["recovery_started_event_sha256"] = recovery_started_event_sha256
    detail["state_after_sha256"] = step_state_after["collector_state_sha256"]
    detail["step_state_after"] = dict(step_state_after)
    if event_type == "SQLITE_RECOVERY_COMPLETED":
        detail["completed_at"] = finished_at
        detail["recovery_classification"] = "hot_delete_journal_recovered"
    else:
        detail["failed_at"] = finished_at
        detail["failure_classification"] = "rollback_journal_recovery_failed"
        detail["retryable"] = False
    _validate_recovery_terminal_detail(event_type, detail)
    return detail


def _append_collector_phase_event_once(
    lease: CollectorPhaseLease,
    *,
    predecessor_event_sha256: str,
    event_type: str,
    event: Mapping[str, object],
) -> dict[str, object]:
    """Append a frozen phase event once, including an fsync-outcome reconciliation."""

    _require_event_sha256(predecessor_event_sha256, "phase predecessor hash")

    def matches(candidate: Mapping[str, object]) -> bool:
        return (
            candidate.get("event_type") == event_type
            and candidate.get("previous_event_sha256") == predecessor_event_sha256
            and candidate.get("event") == dict(event)
        )

    history = _phase_ledger_history(lease)
    tail = history[-1]
    if matches(tail):
        return tail
    if tail.get("event_sha256") != predecessor_event_sha256:
        raise CollectorContinuityError("collector recovery phase tail drifted")
    candidate = build_collector_ledger_event(
        previous_event=tail, event_type=event_type, event=event
    )
    _validate_ledger_chain((*history, candidate))
    try:
        appended = _append_collector_phase_event(
            lease, event_type=event_type, event=event
        )
    except (CollectorContinuityError, OSError) as exc:
        current = _phase_ledger_history(lease)[-1]
        if matches(current):
            raise CollectorContinuityError(
                "collector recovery phase append requires replay"
            ) from exc
        raise CollectorContinuityError(
            "collector recovery phase append cannot be confirmed"
        ) from exc
    if not matches(appended):
        raise CollectorContinuityError("collector recovery phase append drifted")
    return appended


_RECOVERY_SQLITE_DENIED_ACTIONS: Final = _REGISTERED_COLLECTOR_READ_DENIED_ACTIONS
_RECOVERY_SQLITE_PRAGMAS: Final = frozenset(
    {"database_list", "foreign_key_check", "foreign_keys", "journal_mode", "synchronous"}
)


def _recovery_sqlite_authorizer():
    def authorize(
        action: int,
        argument_1: str | None,
        argument_2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if action in _RECOVERY_SQLITE_DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == _REGISTERED_COLLECTOR_READ_PRAGMA_ACTION and (
            argument_1 not in _RECOVERY_SQLITE_PRAGMAS or argument_2 is not None
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _trigger_exact_delete_journal_rollback(
    lease: CollectorPhaseLease,
    dangling: _DanglingCollectorAttempt,
    recovery_start: Mapping[str, object],
) -> None:
    """Trigger only SQLite's automatic DELETE-journal recovery behind private fds."""

    current = _locate_dangling_collector_attempt(lease, dangling.spec)
    if (
        current is None
        or current.start_event["event_sha256"] != dangling.start_event["event_sha256"]
        or current.recovery_start_event is None
        or current.recovery_start_event["event_sha256"]
        != recovery_start.get("event_sha256")
    ):
        raise CollectorContinuityError("collector recovery start tail drifted")
    detail = recovery_start.get("event")
    if not isinstance(detail, Mapping):
        raise CollectorContinuityError("collector recovery start event is invalid")
    _validate_recovery_start_detail(detail)
    _reject_recovery_wal_shm_sidecars(current.authority.canonical_path)
    journal = _read_collector_delete_journal(current.authority.expected_identity)
    if journal is None:
        raise CollectorContinuityError(
            "collector recovery journal disappeared before SQLite rollback"
        )
    if (
        journal.identity.to_dict() != detail["journal_identity"]
        or journal.byte_count != detail["journal_bytes"]
        or journal.sha256 != detail["journal_sha256"]
    ):
        raise CollectorContinuityError("collector recovery journal identity drifted")

    opened = open_nofollow_regular(current.authority.canonical_path, writable=True)
    guard_fd = -1
    connection: sqlite3.Connection | None = None
    try:
        if opened.identity != current.authority.expected_identity:
            raise CollectorContinuityError("collector recovery database identity drifted")
        guard_fd = os.dup(opened.descriptor)
        os.set_inheritable(opened.descriptor, False)
        os.set_inheritable(guard_fd, False)
        _verify_private_registered_read_anchors(
            opened.descriptor, guard_fd, current.authority.expected_identity
        )
        # SQLite must open the canonical locator so it observes this database's
        # DELETE rollback journal; private descriptors remain identity anchors.
        verify_file_identity(
            current.authority.canonical_path, current.authority.expected_identity
        )
        locator = _sqlite_uri(current.authority.canonical_path, "rw") + "&cache=private"
        connection = sqlite3.connect(
            locator,
            uri=True,
            check_same_thread=True,
            isolation_level=None,
        )
        sqlite3.Connection.set_authorizer(connection, _recovery_sqlite_authorizer())
        database_list = sqlite3.Connection.execute(
            connection, "PRAGMA database_list"
        ).fetchall()
        if database_list != [(0, "main", current.authority.canonical_path)]:
            raise CollectorContinuityError("collector recovery SQLite locator drifted")
        journal_mode = sqlite3.Connection.execute(
            connection, "PRAGMA journal_mode"
        ).fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise CollectorContinuityError("collector recovery SQLite mode is invalid")
        foreign_key_error = sqlite3.Connection.execute(
            connection, "PRAGMA main.foreign_key_check"
        ).fetchone()
        if foreign_key_error is not None:
            raise CollectorContinuityError("collector recovery foreign key check failed")
        row = sqlite3.Connection.execute(
            connection, "SELECT COUNT(*) FROM main.sqlite_master"
        ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not int:
            raise CollectorContinuityError("collector recovery schema read is invalid")
        receipt_row = sqlite3.Connection.execute(
            connection, "SELECT COUNT(*) FROM main.collection_receipts"
        ).fetchone()
        if receipt_row is None or len(receipt_row) != 1 or type(receipt_row[0]) is not int:
            raise CollectorContinuityError("collector recovery receipt read is invalid")
        if connection.in_transaction:
            raise CollectorContinuityError("collector recovery SQLite transaction is open")
        _verify_private_registered_read_anchors(
            opened.descriptor, guard_fd, current.authority.expected_identity
        )
        verify_file_identity(
            current.authority.canonical_path, current.authority.expected_identity
        )
    except CollectorContinuityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CollectorContinuityError("collector recovery rollback cannot be proven") from exc
    finally:
        cleanup_error: BaseException | None = None
        if connection is not None:
            try:
                sqlite3.Connection.close(connection)
            except BaseException as exc:
                cleanup_error = exc
        if guard_fd >= 0:
            try:
                os.close(guard_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            opened.close()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise CollectorContinuityError(
                "collector recovery SQLite cleanup failed"
            ) from cleanup_error
    verify_file_identity(current.authority.canonical_path, current.authority.expected_identity)
    _reject_recovery_wal_shm_sidecars(current.authority.canonical_path)
    if _read_collector_delete_journal(current.authority.expected_identity) is not None:
        raise CollectorContinuityError("collector recovery rollback journal remains")
    fsync_regular_file(current.authority.canonical_path)
    fsync_parent_directory(current.authority.canonical_path)


def _recover_raw_postcondition(
    dangling: _DanglingCollectorAttempt, *, finished_at: str
) -> CollectorRawPostconditionResult:
    baseline = _rehydrate_collector_step_baseline(dangling.start, dangling.spec)
    with open_registered_collector_read_connection(dangling.spec) as token:
        return verify_collector_raw_postcondition(
            token,
            dangling.spec,
            baseline,
            attempt_started_at=str(dangling.start["started_at"]),
            attempt_finished_at=finished_at,
        )


def _recovered_attempt_terminal(
    dangling: _DanglingCollectorAttempt,
    *,
    step_state_after: Mapping[str, object],
    finished_at: str,
    failure_classification: str | None,
) -> tuple[Literal["ATTEMPT_COMPLETED", "ATTEMPT_FAILED"], dict[str, object]]:
    state_after = validate_collector_step_state(
        step_state_after, allowed_tables=dangling.spec.allowed_tables
    )
    start = dangling.start
    detail = {
        "registration_sha256": start["registration_sha256"],
        "database_uuid": start["database_uuid"],
        "state_before_sha256": start["state_before_sha256"],
        "session": start["session"],
        "phase": start["phase"],
        "step_id": start["step_id"],
        "step_ordinal": start["step_ordinal"],
        "attempt_id": start["attempt_id"],
        "command_sha256": start["command_sha256"],
        "started_at": start["started_at"],
        "step_state_before": start["step_state_before"],
        "step_raw_before": start["step_raw_before"],
        "state_after_sha256": state_after["collector_state_sha256"],
        "step_state_after": state_after,
        "process_result_known": False,
        "process_launch_state": "indeterminate",
        "returncode": None,
        "stdout_sha256": None,
        "stdout_bytes": None,
        "stderr_sha256": None,
        "stderr_bytes": None,
        "recovered": True,
        "verifier_id": _RAW_POSTCONDITION_SCHEMA,
    }
    if failure_classification is None:
        detail["completed_at"] = finished_at
        event_type: Literal["ATTEMPT_COMPLETED", "ATTEMPT_FAILED"] = "ATTEMPT_COMPLETED"
    else:
        if failure_classification not in _ATTEMPT_FAILURE_RETRYABILITY:
            raise CollectorContinuityError("collector recovery failure classification is invalid")
        detail["failed_at"] = finished_at
        detail["failure_classification"] = failure_classification
        detail["retryable"] = _ATTEMPT_FAILURE_RETRYABILITY[failure_classification]
        event_type = "ATTEMPT_FAILED"
    _validate_ledger_detail(event_type, detail)
    return event_type, detail


def _recovery_attempt_classification(
    raw: CollectorRawPostconditionResult,
) -> str | None:
    if raw.verifier_id != _RAW_POSTCONDITION_SCHEMA:
        raise CollectorContinuityError("collector recovery verifier identity is invalid")
    if raw.raw_class == "complete":
        return None
    if raw.raw_class == "unchanged":
        return "interrupted_no_commit"
    if raw.raw_class == "partial_prices":
        return "interrupted_partial_prices"
    if raw.raw_class == "forbidden":
        return "forbidden_drift"
    raise CollectorContinuityError("collector recovery raw classification is invalid")


def _recovered_attempt_outcome(
    terminal: Mapping[str, object], *, raw_class: str
) -> CollectorAttemptOutcome:
    event_type = terminal.get("event_type")
    detail = terminal.get("event")
    if event_type not in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"} or not isinstance(
        detail, Mapping
    ):
        raise CollectorContinuityError("collector recovery attempt terminal is invalid")
    failure = detail.get("failure_classification")
    if event_type == "ATTEMPT_COMPLETED":
        classification = "complete"
        retryable = False
    elif not isinstance(failure, str) or type(detail.get("retryable")) is not bool:
        raise CollectorContinuityError("collector recovery attempt terminal is invalid")
    else:
        classification = failure
        retryable = bool(detail["retryable"])
    return CollectorAttemptOutcome(
        step_id=str(detail["step_id"]),
        step_ordinal=int(detail["step_ordinal"]),
        attempt_id=str(detail["attempt_id"]),
        terminal_event_sha256=str(terminal["event_sha256"]),
        terminal_event_type=event_type,
        classification=classification,
        retryable=retryable,
        process_result_known=False,
        returncode=None,
        raw_class=raw_class,
    )


def _recovery_terminal_state_matches(
    dangling: _DanglingCollectorAttempt, raw: CollectorRawPostconditionResult
) -> None:
    if dangling.recovery_terminal is None:
        raise CollectorContinuityError("collector recovery terminal is missing")
    expected = validate_collector_step_state(
        dangling.recovery_terminal["step_state_after"],
        allowed_tables=dangling.spec.allowed_tables,
    )
    actual = validate_collector_step_state(
        raw.step_state_after, allowed_tables=dangling.spec.allowed_tables
    )
    if (
        expected["collector_state_sha256"] != actual["collector_state_sha256"]
        or dangling.recovery_terminal["state_after_sha256"]
        != actual["collector_state_sha256"]
    ):
        raise CollectorContinuityError("collector recovery terminal state drifted")


def _recover_dangling_collector_attempt(
    lease: CollectorPhaseLease,
    spec: CollectorStepSpec,
    *,
    now: Callable[[], str] = _collector_attempt_now,
) -> CollectorAttemptOutcome:
    """Classify one durable dangling start without invoking a provider or writer."""

    dangling = _locate_dangling_collector_attempt(lease, spec)
    if dangling is None:
        raise CollectorContinuityError("collector recovery has no dangling attempt")
    _reject_recovery_wal_shm_sidecars(dangling.authority.canonical_path)

    if dangling.recovery_terminal is None:
        if dangling.recovery_start is None:
            journal = _read_collector_delete_journal(
                dangling.authority.expected_identity
            )
            if journal is not None:
                started_at = now()
                start_detail = _recovery_start_event(
                    dangling, journal, started_at=started_at
                )
                started = _append_collector_phase_event_once(
                    lease,
                    predecessor_event_sha256=str(
                        dangling.start_event["event_sha256"]
                    ),
                    event_type="SQLITE_RECOVERY_STARTED",
                    event=start_detail,
                )
                dangling = _locate_dangling_collector_attempt(lease, spec)
                if dangling is None or dangling.recovery_start is None:
                    raise CollectorContinuityError("collector recovery start was not retained")
                _trigger_exact_delete_journal_rollback(lease, dangling, started)
        else:
            if (
                dangling.recovery_start is None
                or dangling.recovery_start_event is None
            ):
                raise CollectorContinuityError("collector recovery start is invalid")
            _validate_recovery_start_detail(dangling.recovery_start)
            journal = _read_collector_delete_journal(
                dangling.authority.expected_identity
            )
            if journal is not None:
                if (
                    journal.identity.to_dict()
                    != dangling.recovery_start["journal_identity"]
                    or journal.byte_count
                    != dangling.recovery_start["journal_bytes"]
                    or journal.sha256 != dangling.recovery_start["journal_sha256"]
                ):
                    raise CollectorContinuityError(
                        "collector recovery journal identity drifted"
                    )
                _trigger_exact_delete_journal_rollback(
                    lease, dangling, dangling.recovery_start_event
                )
            else:
                verify_file_identity(
                    dangling.authority.canonical_path,
                    dangling.authority.expected_identity,
                )
                _reject_recovery_wal_shm_sidecars(
                    dangling.authority.canonical_path
                )
                fsync_regular_file(dangling.authority.canonical_path)
                fsync_parent_directory(dangling.authority.canonical_path)
                verify_file_identity(
                    dangling.authority.canonical_path,
                    dangling.authority.expected_identity,
                )
                _reject_recovery_wal_shm_sidecars(
                    dangling.authority.canonical_path
                )
                if (
                    _read_collector_delete_journal(
                        dangling.authority.expected_identity
                    )
                    is not None
                ):
                    raise CollectorContinuityError(
                        "collector recovery journal reappeared before replay"
                    )

        dangling = _locate_dangling_collector_attempt(lease, spec)
        if dangling is None:
            raise CollectorContinuityError("collector recovery attempt disappeared")
        finished_at = now()
        _raw_timestamp(finished_at, "recovery finish")
        raw = _recover_raw_postcondition(dangling, finished_at=finished_at)
        if dangling.recovery_start is not None:
            if dangling.recovery_start_event is None:
                raise CollectorContinuityError("collector recovery start is invalid")
            recovery_detail = _recovery_terminal_event(
                dangling.recovery_start,
                str(dangling.recovery_start_event["event_sha256"]),
                event_type="SQLITE_RECOVERY_COMPLETED",
                step_state_after=raw.step_state_after,
                finished_at=finished_at,
            )
            _append_collector_phase_event_once(
                lease,
                predecessor_event_sha256=str(
                    dangling.recovery_start_event["event_sha256"]
                ),
                event_type="SQLITE_RECOVERY_COMPLETED",
                event=recovery_detail,
            )
            dangling = _locate_dangling_collector_attempt(lease, spec)
            if dangling is None:
                raise CollectorContinuityError("collector recovery attempt disappeared")
    else:
        finished_at = now()
        _raw_timestamp(finished_at, "recovery finish")
        raw = _recover_raw_postcondition(dangling, finished_at=finished_at)
        _recovery_terminal_state_matches(dangling, raw)

    if dangling.recovery_terminal is not None:
        _recovery_terminal_state_matches(dangling, raw)
    if (
        dangling.recovery_terminal_event is not None
        and dangling.recovery_terminal_event["event_type"] == "SQLITE_RECOVERY_FAILED"
    ):
        failure_classification: str | None = "rollback_journal_recovery_failed"
    else:
        failure_classification = _recovery_attempt_classification(raw)
    event_type, detail = _recovered_attempt_terminal(
        dangling,
        step_state_after=raw.step_state_after,
        finished_at=finished_at,
        failure_classification=failure_classification,
    )
    predecessor = (
        dangling.recovery_terminal_event
        if dangling.recovery_terminal_event is not None
        else dangling.start_event
    )
    terminal = _append_collector_phase_event_once(
        lease,
        predecessor_event_sha256=str(predecessor["event_sha256"]),
        event_type=event_type,
        event=detail,
    )
    return _recovered_attempt_outcome(terminal, raw_class=raw.raw_class)



def _begin_collector_step_attempt(
    lease: CollectorPhaseLease,
    spec: CollectorStepSpec,
    *,
    now: Callable[[], str] = _collector_attempt_now,
) -> _CollectorAttemptLaunch:
    """Persist a fresh active start before any child descriptor or process exists."""

    history = _attempt_history_for_spec(
        lease, spec, enforce_phase_order=False
    )
    registration = _read_bound_registration(spec.registration_file)
    prepared = registration["prepared"]
    if not isinstance(prepared, Mapping):
        raise CollectorContinuityError("collector attempt prepared authority is invalid")
    try:
        ledger_identity = PhysicalFileIdentity.from_dict(prepared["ledger_identity"])
        database_uuid = _require_event_sha256(prepared["database_uuid"], "database_uuid")
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector attempt prepared authority is invalid") from exc
    if lease.verify() != ledger_identity:
        raise CollectorContinuityError("collector attempt lease identity drifted")
    with open_registered_collector_read_connection(spec) as token:
        baseline = capture_collector_step_baseline(token, spec)
    state_before = validate_collector_step_state(
        baseline.step_state, allowed_tables=spec.allowed_tables
    )
    tail_state = _tail_state_after(history)
    if tail_state is not None and tail_state != state_before["collector_state_sha256"]:
        raise CollectorContinuityError("collector attempt ledger state drifted")
    step_raw_before = _step_raw_before_from_baseline(baseline, spec)
    nonce = secrets.token_bytes(_COLLECTOR_ATTEMPT_NONCE_BYTES)
    nonce_buffer = bytearray(nonce)
    if len(nonce_buffer) != _COLLECTOR_ATTEMPT_NONCE_BYTES:
        _clear_nonce(nonce_buffer)
        raise CollectorContinuityError("collector attempt nonce is invalid")
    attempt_id = secrets.token_hex(32)
    started_at = now()
    _raw_timestamp(started_at, "attempt start")
    details = {
        "registration_sha256": spec.registration_sha256,
        "database_uuid": database_uuid,
        "state_before_sha256": state_before["collector_state_sha256"],
        "session": spec.session,
        "phase": spec.phase,
        "step_id": spec.step_id,
        "step_ordinal": spec.step_ordinal,
        "attempt_id": attempt_id,
        "command_sha256": spec.command_sha256,
        "lease_nonce_sha256": hashlib.sha256(nonce).hexdigest(),
        "started_at": started_at,
        "step_state_before": state_before,
        "step_raw_before": step_raw_before,
    }
    try:
        started = _append_collector_phase_event(
            lease, event_type="ATTEMPT_STARTED", event=details
        )
    except (CollectorContinuityError, OSError) as exc:
        _clear_nonce(nonce_buffer)
        raise CollectorContinuityError("collector attempt start cannot be persisted") from exc
    except BaseException:
        _clear_nonce(nonce_buffer)
        raise
    return _CollectorAttemptLaunch(
        spec=spec,
        baseline=baseline,
        step_raw_before=step_raw_before,
        attempt_id=attempt_id,
        started_at=started_at,
        started_event_sha256=str(started["event_sha256"]),
        nonce_sha256=details["lease_nonce_sha256"],
        ledger_identity=ledger_identity,
        database_uuid=database_uuid,
        nonce=nonce,
        _nonce_buffer=nonce_buffer,
    )


def _active_attempt_tail(
    lease: CollectorPhaseLease, launch: _CollectorAttemptLaunch
) -> dict[str, object]:
    history = _phase_ledger_history(lease)
    tail = history[-1]
    if tail.get("event_type") != "ATTEMPT_STARTED" or tail.get("event_sha256") != launch.started_event_sha256:
        raise CollectorContinuityError("collector active attempt tail drifted")
    details = tail.get("event")
    if not isinstance(details, dict) or any(
        details.get(field) != expected
        for field, expected in (
            ("registration_sha256", launch.spec.registration_sha256),
            ("attempt_id", launch.attempt_id),
            ("command_sha256", launch.spec.command_sha256),
            ("lease_nonce_sha256", launch.nonce_sha256),
            ("step_raw_before", launch.step_raw_before),
        )
    ):
        raise CollectorContinuityError("collector active attempt detail drifted")
    return details


def _read_exact_child_nonce(descriptor: int) -> bytearray:
    """Consume one pipe payload, requiring exactly the nonce and EOF."""

    if type(descriptor) is not int or descriptor < 3:
        raise CollectorContinuityError("collector nonce descriptor is invalid")
    nonce = bytearray()
    close_error: BaseException | None = None
    try:
        if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise CollectorContinuityError("collector nonce descriptor is not a pipe")
        while len(nonce) <= _COLLECTOR_ATTEMPT_NONCE_BYTES:
            chunk = os.read(
                descriptor,
                _COLLECTOR_ATTEMPT_NONCE_BYTES + 1 - len(nonce),
            )
            if not chunk:
                break
            nonce.extend(chunk)
        if len(nonce) != _COLLECTOR_ATTEMPT_NONCE_BYTES:
            raise CollectorContinuityError("collector nonce pipe length is invalid")
        if os.read(descriptor, 1):
            raise CollectorContinuityError("collector nonce pipe has trailing bytes")
        return nonce
    except OSError as exc:
        raise CollectorContinuityError("collector nonce pipe cannot be read") from exc
    finally:
        try:
            _close_descriptor_once(descriptor, label="nonce descriptor")
        except BaseException as exc:
            close_error = exc
        if close_error is not None:
            _clear_nonce(nonce)
            raise close_error


def _consume_child_attempt_environment(
    environ: Mapping[str, str] | None,
    *,
    allow_legacy_test_handoff: bool,
    consume: bool,
) -> tuple[dict[str, str], bool]:
    """Validate one exact child environment without touching SQLite authority."""

    source: Mapping[str, str] = os.environ if environ is None else environ
    present = {
        name for name in source
        if isinstance(name, str) and name.startswith("STOCKDATA_COLLECTOR_")
    }
    expected = set(_COLLECTOR_CHILD_ENVIRONMENT)
    legacy = False
    if present != expected:
        if allow_legacy_test_handoff and present == _COLLECTOR_CHILD_LEGACY_ENVIRONMENT:
            legacy = True
        else:
            raise CollectorContinuityError("collector child attempt environment is invalid")
    required = _COLLECTOR_CHILD_LEGACY_ENVIRONMENT if legacy else expected
    values: dict[str, str] = {}
    for name in required:
        value = source.get(name)
        if not isinstance(value, str) or not value:
            raise CollectorContinuityError("collector child attempt environment is invalid")
        values[name] = value
    if consume and environ is None:
        for name in expected:
            os.environ.pop(name, None)
    return values, legacy


def classify_collector_child_environment(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Classify the process as a complete child before any SQLite marker read."""

    source: Mapping[str, str] = os.environ if environ is None else environ
    if not any(
        isinstance(name, str) and name.startswith("STOCKDATA_COLLECTOR_")
        for name in source
    ):
        return False
    _consume_child_attempt_environment(
        environ,
        allow_legacy_test_handoff=False,
        consume=False,
    )
    return True


@dataclass(frozen=True)
class _CollectorChildAuthority:
    registration_file: str
    registration_sha256: str
    database_uuid: str
    ledger_path: str
    ledger_identity: PhysicalFileIdentity
    legacy_test_handoff: bool


def _legacy_child_authority_from_registration(
    registration_file: str,
) -> _CollectorChildAuthority:
    """Read only the capability envelope needed to prove an injected test handoff."""

    path = lexical_absolute_path(registration_file)
    opened = open_nofollow_regular(path)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise CollectorContinuityError("collector registration size is invalid")
        raw = os.pread(opened.descriptor, size, 0)
        if len(raw) != size:
            raise CollectorContinuityError("collector registration was truncated while reading")
        verify_file_identity(path, opened.identity)
    except OSError as exc:
        raise CollectorContinuityError("collector registration cannot be read") from exc
    finally:
        opened.close()
    registration = decode_canonical_json_object(raw)
    require_exact_keys(registration, _REGISTRATION_V4_FIELDS, "collector registration")
    if registration.get("schema_version") != _REGISTRATION_V4_SCHEMA:
        raise CollectorContinuityError("collector registration schema is unsupported")
    prerequisites = registration.get("prerequisites")
    if not isinstance(prerequisites, Mapping) or not isinstance(prerequisites.get("collector"), Mapping):
        raise CollectorContinuityError("collector registration capability is invalid")
    capability = decode_capability(canonical_json_bytes(dict(prerequisites["collector"])))
    try:
        ledger_identity = PhysicalFileIdentity.from_dict(capability["ledger_identity"])
        database_uuid = _require_event_sha256(capability["database_uuid"], "database_uuid")
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector child capability is invalid") from exc
    ledger_path = lexical_absolute_path(capability["ledger_path"])
    if ledger_path != ledger_identity.canonical_path:
        raise CollectorContinuityError("collector child ledger path is invalid")
    return _CollectorChildAuthority(
        registration_file=path,
        registration_sha256=hashlib.sha256(raw).hexdigest(),
        database_uuid=database_uuid,
        ledger_path=ledger_path,
        ledger_identity=ledger_identity,
        legacy_test_handoff=True,
    )


def _child_authority_from_environment(
    values: Mapping[str, str], *, legacy_test_handoff: bool
) -> _CollectorChildAuthority:
    registration_file = lexical_absolute_path(values["STOCKDATA_COLLECTOR_REGISTRATION_FILE"])
    if legacy_test_handoff:
        return _legacy_child_authority_from_registration(registration_file)
    raw_identity = values["STOCKDATA_COLLECTOR_LEDGER_IDENTITY"]
    try:
        ledger_identity = PhysicalFileIdentity.from_dict(
            decode_canonical_json_object(raw_identity.encode("ascii"))
        )
        registration_sha256 = _require_event_sha256(
            values["STOCKDATA_COLLECTOR_REGISTRATION_SHA256"], "registration_sha256"
        )
        database_uuid = _require_event_sha256(
            values["STOCKDATA_COLLECTOR_DATABASE_UUID"], "database_uuid"
        )
    except (UnicodeEncodeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector child environment authority is invalid") from exc
    ledger_path = lexical_absolute_path(values["STOCKDATA_COLLECTOR_LEDGER_FILE"])
    if ledger_path != ledger_identity.canonical_path:
        raise CollectorContinuityError("collector child ledger path is invalid")
    return _CollectorChildAuthority(
        registration_file=registration_file,
        registration_sha256=registration_sha256,
        database_uuid=database_uuid,
        ledger_path=ledger_path,
        ledger_identity=ledger_identity,
        legacy_test_handoff=False,
    )


def _parse_child_descriptor(value: str, *, label: str) -> int:
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise CollectorContinuityError(f"collector child {label} is invalid")
    descriptor = int(value)
    if descriptor < 3:
        raise CollectorContinuityError(f"collector child {label} is invalid")
    return descriptor


def _child_environment_matches_active_attempt(
    values: Mapping[str, str],
    authority: _CollectorChildAuthority,
    active_attempt: Mapping[str, object],
    argv: Sequence[str],
) -> None:
    """Match plain child inputs to the active tail before opening SQLite."""

    command = tuple(argv)
    if not command or any(not isinstance(item, str) for item in command):
        raise CollectorContinuityError("collector child argv is invalid")
    command_sha256 = canonical_json_sha256(
        {"schema_version": "stockdata-forward-collector-command/1", "argv": list(command)}
    )
    if authority.legacy_test_handoff:
        expected = {
            "registration_sha256": authority.registration_sha256,
            "database_uuid": authority.database_uuid,
            "attempt_id": values["STOCKDATA_COLLECTOR_ATTEMPT_ID"],
            "command_sha256": command_sha256,
        }
    else:
        ordinal_text = values["STOCKDATA_COLLECTOR_STEP_ORDINAL"]
        if (
            not ordinal_text.isascii()
            or not ordinal_text.isdecimal()
            or str(int(ordinal_text)) != ordinal_text
        ):
            raise CollectorContinuityError("collector child step ordinal is invalid")
        ordinal = int(ordinal_text)
        if ordinal > 11:
            raise CollectorContinuityError("collector child step ordinal is invalid")
        step_id = values["STOCKDATA_COLLECTOR_STEP_ID"]
        expected_step = _STEP_IDENTITY.get(step_id)
        if (
            expected_step is None
            or values["STOCKDATA_COLLECTOR_PHASE"] != expected_step[0]
            or ordinal % 4 != expected_step[1]
            or values["STOCKDATA_COLLECTOR_SESSION"] != _validate_collector_session(
                values["STOCKDATA_COLLECTOR_SESSION"]
            )
        ):
            raise CollectorContinuityError("collector child step identity is invalid")
        expected = {
            "registration_sha256": authority.registration_sha256,
            "database_uuid": authority.database_uuid,
            "attempt_id": values["STOCKDATA_COLLECTOR_ATTEMPT_ID"],
            "session": values["STOCKDATA_COLLECTOR_SESSION"],
            "phase": values["STOCKDATA_COLLECTOR_PHASE"],
            "step_id": step_id,
            "step_ordinal": ordinal,
            "command_sha256": values["STOCKDATA_COLLECTOR_COMMAND_SHA256"],
        }
        _require_event_sha256(expected["command_sha256"], "command_sha256")
        if expected["command_sha256"] != command_sha256:
            raise CollectorContinuityError("collector child command hash is invalid")
    if any(active_attempt.get(field) != value for field, value in expected.items()):
        raise CollectorContinuityError("collector child active attempt drifted")
    if command_sha256 != active_attempt.get("command_sha256"):
        raise CollectorContinuityError("collector child argv does not match active attempt")


def _matching_child_spec(
    registration_file: str,
    argv: Sequence[str],
    *,
    active_attempt: Mapping[str, object],
) -> CollectorStepSpec:
    if not isinstance(registration_file, str) or not registration_file:
        raise CollectorContinuityError("collector child registration locator is invalid")
    command = tuple(argv)
    if not command or any(not isinstance(item, str) for item in command):
        raise CollectorContinuityError("collector child argv is invalid")
    canonical_registration = lexical_absolute_path(registration_file)
    matches = [
        spec
        for spec in freeze_collector_step_schedule(registration_file=canonical_registration)
        if (
            spec.command == command
            and active_attempt.get("registration_sha256") == spec.registration_sha256
            and active_attempt.get("session") == spec.session
            and active_attempt.get("phase") == spec.phase
            and active_attempt.get("step_id") == spec.step_id
            and active_attempt.get("step_ordinal") == spec.step_ordinal
            and active_attempt.get("command_sha256") == spec.command_sha256
        )
    ]
    if len(matches) != 1:
        raise CollectorContinuityError("collector child argv does not match active frozen step")
    return matches[0]


def _writer_binding_locked(token: object) -> _CollectorWriteBinding:
    if type(token) is not CollectorWriteToken:
        raise CollectorContinuityError("collector writer token is invalid")
    binding = _COLLECTOR_WRITE_BINDINGS.get(token)
    if (
        binding is None
        or binding.token is not token
        or binding.state != "OPEN"
        or binding.owner_pid != os.getpid()
        or binding.owner_thread_id != threading.get_ident()
        or len(binding.nonce) != _COLLECTOR_ATTEMPT_NONCE_BYTES
        or hashlib.sha256(binding.nonce).hexdigest() != binding.nonce_sha256
    ):
        raise CollectorContinuityError("collector writer token binding is invalid")
    return binding


def _verify_writer_binding_locked(
    binding: _CollectorWriteBinding,
    *,
    database_path: str | os.PathLike[str],
    step_id: str | None = None,
    session: str | None = None,
) -> None:
    expected_database = lexical_absolute_path(database_path)
    if expected_database != binding.spec.database_path:
        raise CollectorContinuityError("collector writer database path drifted")
    if step_id is not None and step_id != binding.spec.step_id:
        raise CollectorContinuityError("collector writer step authority is invalid")
    if session is not None and session != binding.spec.session:
        raise CollectorContinuityError("collector writer session authority is invalid")
    registration = _read_bound_registration(binding.spec.registration_file)
    prepared = registration["prepared"]
    if not isinstance(prepared, Mapping):
        raise CollectorContinuityError("collector writer prepared authority is invalid")
    try:
        ledger_identity = PhysicalFileIdentity.from_dict(prepared["ledger_identity"])
        database_identity = PhysicalFileIdentity.from_dict(prepared["database_identity"])
    except (KeyError, TypeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector writer authority is invalid") from exc
    if database_identity.canonical_path != expected_database:
        raise CollectorContinuityError("collector writer database identity drifted")
    verify_file_identity(expected_database, database_identity)
    verify_locked_collector_lease(
        binding.lease_fd, expected_ledger_identity=ledger_identity
    )
    if os.get_inheritable(binding.lease_fd):
        raise CollectorContinuityError("collector writer lease descriptor is inheritable")
    ledger = _parse_retained_bound_collector_ledger(
        ledger_identity.canonical_path, ledger_identity
    )
    tail = ledger[-1]
    details = tail.get("event")
    if (
        tail.get("event_type") != "ATTEMPT_STARTED"
        or tail.get("event_sha256") != binding.started_event_sha256
        or not isinstance(details, Mapping)
        or details.get("registration_sha256") != binding.spec.registration_sha256
        or details.get("attempt_id") != binding.attempt_id
        or details.get("session") != binding.spec.session
        or details.get("phase") != binding.spec.phase
        or details.get("step_id") != binding.spec.step_id
        or details.get("step_ordinal") != binding.spec.step_ordinal
        or details.get("command_sha256") != binding.spec.command_sha256
        or details.get("lease_nonce_sha256") != binding.nonce_sha256
    ):
        raise CollectorContinuityError("collector writer active attempt drifted")


def require_collector_writer(
    token: CollectorWriteToken,
    *,
    database_path: str | os.PathLike[str],
    step_id: str | None = None,
    session: str | None = None,
) -> None:
    """Reprove an opaque child writer token before collector code can write."""

    require_collector_continuity_health()
    with _COLLECTOR_WRITE_LOCK:
        binding = _writer_binding_locked(token)
        _verify_writer_binding_locked(
            binding,
            database_path=database_path,
            step_id=step_id,
            session=session,
        )


def open_collector_writer_database(
    *,
    database_path: str | os.PathLike[str],
    writer_token: CollectorWriteToken,
):
    """Open a collector only after writer authority has been proven.

    The cache factory intentionally has no schema creation or migration branch
    for a marked collector database.
    """

    require_collector_writer(writer_token, database_path=database_path)
    from .cache import open_authorized_collector_cache

    return open_authorized_collector_cache(
        database_path, writer_token=writer_token
    )


def open_collector_child_writer_authority(
    *,
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> CollectorWriteToken:
    """Consume inherited attempt plumbing and return one child-only writer token."""

    require_collector_continuity_health()
    values, legacy_test_handoff = _consume_child_attempt_environment(
        environ,
        allow_legacy_test_handoff=environ is not None,
        consume=True,
    )
    lease_fd = _parse_child_descriptor(
        values["STOCKDATA_COLLECTOR_LEASE_FD"], label="lease descriptor"
    )
    nonce_fd = _parse_child_descriptor(
        values["STOCKDATA_COLLECTOR_PIPE_FD"], label="nonce descriptor"
    )
    if lease_fd == nonce_fd:
        raise CollectorContinuityError("collector child descriptors are invalid")
    token: CollectorWriteToken | None = None
    nonce: bytearray | None = None
    keep_lease = False
    try:
        authority = _child_authority_from_environment(
            values, legacy_test_handoff=legacy_test_handoff
        )
        verify_locked_collector_lease(
            lease_fd, expected_ledger_identity=authority.ledger_identity
        )
        os.set_inheritable(lease_fd, False)
        ledger = _parse_retained_bound_collector_ledger(
            authority.ledger_path, authority.ledger_identity
        )
        tail = ledger[-1]
        details = tail.get("event")
        if tail.get("event_type") != "ATTEMPT_STARTED" or not isinstance(details, Mapping):
            raise CollectorContinuityError("collector child active attempt drifted")
        _child_environment_matches_active_attempt(values, authority, details, argv)
        nonce_descriptor = nonce_fd
        nonce_fd = -1
        nonce = _read_exact_child_nonce(nonce_descriptor)
        nonce_sha256 = hashlib.sha256(nonce).hexdigest()
        if nonce_sha256 != details.get("lease_nonce_sha256"):
            raise CollectorContinuityError("collector child nonce does not match active attempt")
        # The lock, retained ledger tail, argv, and nonce are now proven.  Only
        # now may the child read the registration or open SQLite authority.
        registration = _read_bound_registration(authority.registration_file)
        prepared = registration["prepared"]
        if not isinstance(prepared, Mapping):
            raise CollectorContinuityError("collector child prepared authority is invalid")
        try:
            prepared_ledger_identity = PhysicalFileIdentity.from_dict(prepared["ledger_identity"])
            prepared_database_uuid = _require_event_sha256(
                prepared["database_uuid"], "database_uuid"
            )
        except (KeyError, TypeError, CollectorContinuityError) as exc:
            raise CollectorContinuityError("collector child prepared authority is invalid") from exc
        if (
            prepared_ledger_identity != authority.ledger_identity
            or prepared_database_uuid != authority.database_uuid
            or registration.get("registration_sha256") != authority.registration_sha256
        ):
            raise CollectorContinuityError("collector child registration authority drifted")
        spec = _matching_child_spec(
            authority.registration_file, argv, active_attempt=details
        )
        if (
            details.get("registration_sha256") != spec.registration_sha256
            or details.get("database_uuid") != authority.database_uuid
            or details.get("attempt_id") != values["STOCKDATA_COLLECTOR_ATTEMPT_ID"]
            or details.get("session") != spec.session
            or details.get("phase") != spec.phase
            or details.get("step_id") != spec.step_id
            or details.get("step_ordinal") != spec.step_ordinal
            or details.get("command_sha256") != spec.command_sha256
        ):
            raise CollectorContinuityError("collector child active attempt drifted")
        token = CollectorWriteToken()
        binding = _CollectorWriteBinding(
            token=token,
            spec=spec,
            attempt_id=values["STOCKDATA_COLLECTOR_ATTEMPT_ID"],
            started_event_sha256=str(tail["event_sha256"]),
            nonce_sha256=nonce_sha256,
            nonce=nonce,
            lease_fd=lease_fd,
        )
        with _COLLECTOR_WRITE_LOCK:
            if token in _COLLECTOR_WRITE_BINDINGS:
                raise CollectorContinuityError("collector writer token is already bound")
            _COLLECTOR_WRITE_BINDINGS[token] = binding
        keep_lease = True
        return token
    except BaseException:
        if nonce is not None:
            _clear_nonce(nonce)
        raise
    finally:
        if nonce_fd >= 0:
            try:
                _close_descriptor_once(nonce_fd, label="nonce descriptor")
            except CollectorContinuityError:
                _mark_collector_continuity_fatal("collector-child-nonce-close")
        if not keep_lease and lease_fd >= 0:
            try:
                _close_descriptor_once(lease_fd, label="lease descriptor")
            except CollectorContinuityError:
                _mark_collector_continuity_fatal("collector-child-lease-close")


def close_collector_writer_authority(token: CollectorWriteToken) -> None:
    """Retire the child token and close only its inherited lock duplicate."""

    with _COLLECTOR_WRITE_LOCK:
        binding = _writer_binding_locked(token)
        _COLLECTOR_WRITE_BINDINGS.pop(token, None)
        binding.token = None
        binding.state = "CLOSED"
        descriptor = binding.lease_fd
        binding.lease_fd = -1
        _clear_nonce(binding.nonce)
    _close_descriptor_once(descriptor, label="writer lease descriptor")


def _drain_collector_child_output(process: object) -> _CollectorProcessResult:
    """Hash bounded stdout/stderr concurrently without retaining either body."""

    streams = {
        "stdout": getattr(process, "stdout", None),
        "stderr": getattr(process, "stderr", None),
    }
    if any(stream is None or not callable(getattr(stream, "read", None)) for stream in streams.values()):
        return _CollectorProcessResult(False, None, None, None, None, None, True)
    results: dict[str, tuple[str, int]] = {}
    failures: list[BaseException] = []
    result_lock = threading.Lock()
    terminate_lock = threading.Lock()
    terminated = False

    def terminate_once() -> None:
        nonlocal terminated
        with terminate_lock:
            if terminated:
                return
            terminated = True
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except BaseException as exc:
                    with result_lock:
                        failures.append(exc)

    def drain(name: str, stream: object) -> None:
        digest = hashlib.sha256()
        count = 0
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[union-attr]
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise CollectorContinuityError("collector child output is not bytes")
                count += len(chunk)
                if count > _COLLECTOR_ATTEMPT_OUTPUT_LIMIT:
                    terminate_once()
                    raise CollectorContinuityError("collector child output exceeded limit")
                digest.update(chunk)
            with result_lock:
                results[name] = (digest.hexdigest(), count)
        except BaseException as exc:
            terminate_once()
            with result_lock:
                failures.append(exc)

    workers = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in streams.items()
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    try:
        waited = getattr(process, "wait")()
        returncode = waited if type(waited) is int else getattr(process, "returncode", None)
    except BaseException as exc:
        failures.append(exc)
        returncode = None
    if failures or type(returncode) is not int or set(results) != {"stdout", "stderr"}:
        return _CollectorProcessResult(False, None, None, None, None, None, True)
    stdout_sha256, stdout_bytes = results["stdout"]
    stderr_sha256, stderr_bytes = results["stderr"]
    return _CollectorProcessResult(
        True,
        returncode,
        stdout_sha256,
        stdout_bytes,
        stderr_sha256,
        stderr_bytes,
        False,
    )


def _collector_attempt_child_environment(
    launch: _CollectorAttemptLaunch, *, lease_fd: int, nonce_fd: int
) -> dict[str, str]:
    return {
        "STOCKDATA_COLLECTOR_REGISTRATION_FILE": launch.spec.registration_file,
        "STOCKDATA_COLLECTOR_LEDGER_FILE": launch.ledger_identity.canonical_path,
        "STOCKDATA_COLLECTOR_LEDGER_IDENTITY": canonical_json_bytes(
            launch.ledger_identity.to_dict()
        ).decode("ascii"),
        "STOCKDATA_COLLECTOR_ATTEMPT_ID": launch.attempt_id,
        "STOCKDATA_COLLECTOR_SESSION": launch.spec.session,
        "STOCKDATA_COLLECTOR_PHASE": launch.spec.phase,
        "STOCKDATA_COLLECTOR_STEP_ID": launch.spec.step_id,
        "STOCKDATA_COLLECTOR_STEP_ORDINAL": str(launch.spec.step_ordinal),
        "STOCKDATA_COLLECTOR_COMMAND_SHA256": launch.spec.command_sha256,
        "STOCKDATA_COLLECTOR_REGISTRATION_SHA256": launch.spec.registration_sha256,
        "STOCKDATA_COLLECTOR_DATABASE_UUID": launch.database_uuid,
        "STOCKDATA_COLLECTOR_LEASE_FD": str(lease_fd),
        "STOCKDATA_COLLECTOR_PIPE_FD": str(nonce_fd),
    }


def _terminal_attempt_event(
    launch: _CollectorAttemptLaunch,
    raw: CollectorRawPostconditionResult,
    process: _CollectorProcessResult | None,
    *,
    process_launch_state: Literal["not_invoked", "handle_obtained", "indeterminate"],
    finished_at: str,
    failure_classification: str | None,
) -> tuple[Literal["ATTEMPT_COMPLETED", "ATTEMPT_FAILED"], dict[str, object]]:
    if raw.verifier_id != _RAW_POSTCONDITION_SCHEMA:
        raise CollectorContinuityError("collector raw verifier identity is invalid")
    state_after = validate_collector_step_state(
        raw.step_state_after, allowed_tables=launch.spec.allowed_tables
    )
    if process_launch_state == "handle_obtained":
        if process is None or not process.process_result_known:
            raise CollectorContinuityError("collector handled process result is invalid")
        process_result_known = True
        returncode = process.returncode
        stdout_sha256 = process.stdout_sha256
        stdout_bytes = process.stdout_bytes
        stderr_sha256 = process.stderr_sha256
        stderr_bytes = process.stderr_bytes
        recovered = False
    elif process_launch_state == "not_invoked":
        if process is not None:
            raise CollectorContinuityError("collector unlaunched process result is invalid")
        process_result_known = False
        returncode = None
        stdout_sha256 = None
        stdout_bytes = None
        stderr_sha256 = None
        stderr_bytes = None
        recovered = False
    elif process_launch_state == "indeterminate":
        if process is not None:
            raise CollectorContinuityError("collector indeterminate process result is invalid")
        process_result_known = False
        returncode = None
        stdout_sha256 = None
        stdout_bytes = None
        stderr_sha256 = None
        stderr_bytes = None
        recovered = True
    else:
        raise CollectorContinuityError("collector attempt launch state is invalid")
    details: dict[str, object] = {
        "registration_sha256": launch.spec.registration_sha256,
        "database_uuid": launch.database_uuid,
        "state_before_sha256": launch.baseline.step_state["collector_state_sha256"],
        "session": launch.spec.session,
        "phase": launch.spec.phase,
        "step_id": launch.spec.step_id,
        "step_ordinal": launch.spec.step_ordinal,
        "attempt_id": launch.attempt_id,
        "command_sha256": launch.spec.command_sha256,
        "started_at": launch.started_at,
        "step_state_before": launch.baseline.step_state,
        "step_raw_before": launch.step_raw_before,
        "state_after_sha256": state_after["collector_state_sha256"],
        "step_state_after": state_after,
        "process_result_known": process_result_known,
        "process_launch_state": process_launch_state,
        "returncode": returncode,
        "stdout_sha256": stdout_sha256,
        "stdout_bytes": stdout_bytes,
        "stderr_sha256": stderr_sha256,
        "stderr_bytes": stderr_bytes,
        "recovered": recovered,
        "verifier_id": raw.verifier_id,
    }
    if failure_classification is None:
        if (
            raw.raw_class != "complete"
            or process_launch_state != "handle_obtained"
            or process is None
            or process.returncode != 0
            or process.plumbing_failed
        ):
            raise CollectorContinuityError("collector completed attempt outcome is invalid")
        details["completed_at"] = finished_at
        return "ATTEMPT_COMPLETED", details
    if process_launch_state == "not_invoked":
        if failure_classification != "child_launch_failed" or raw.raw_class != "unchanged":
            raise CollectorContinuityError("collector launch failure postcondition is invalid")
    retryable = _ATTEMPT_FAILURE_RETRYABILITY.get(failure_classification)
    if retryable is None:
        raise CollectorContinuityError("collector attempt failure classification is invalid")
    details.update(
        {
            "failed_at": finished_at,
            "failure_classification": failure_classification,
            "retryable": retryable,
        }
    )
    return "ATTEMPT_FAILED", details


def _classify_attempt_terminal(
    raw: CollectorRawPostconditionResult,
    process: _CollectorProcessResult | None,
    *,
    process_launch_state: Literal["not_invoked", "handle_obtained", "indeterminate"] = "handle_obtained",
) -> str | None:
    if process_launch_state == "not_invoked":
        if process is not None or raw.raw_class != "unchanged":
            raise CollectorContinuityError("collector launch failure postcondition is invalid")
        return "child_launch_failed"
    if process_launch_state != "handle_obtained" or process is None:
        raise CollectorContinuityError("collector attempt launch outcome is indeterminate")
    if process.plumbing_failed:
        return "postflight_authority_failure"
    if raw.raw_class == "complete":
        if process.process_result_known and process.returncode == 0:
            return None
        return "child_process_failed_after_complete"
    if raw.raw_class == "unchanged":
        return "child_no_commit"
    if raw.raw_class == "partial_prices":
        return "child_partial_prices"
    if raw.raw_class == "forbidden":
        return "forbidden_drift"
    raise CollectorContinuityError("collector raw attempt classification is invalid")


def _run_collector_child(
    lease: CollectorPhaseLease,
    launch: _CollectorAttemptLaunch,
    *,
    popen_factory: Callable[..., object],
) -> _CollectorChildRun:
    """Spawn one exact child after handing it the lock duplicate and nonce pipe."""

    nonce_read = -1
    nonce_write = -1
    handoff: CollectorChildLeaseHandoff | None = None
    process: object | None = None
    close_failed = False
    popen_called = False
    launcher_error: BaseException | None = None
    try:
        nonce_read, nonce_write = os.pipe()
        os.set_inheritable(nonce_read, True)
        os.set_inheritable(nonce_write, False)
        _write_all(
            nonce_write,
            bytes(launch._nonce_buffer),
            label="collector nonce pipe",
        )
        _close_descriptor_once(nonce_write, label="nonce writer")
        nonce_write = -1
        handoff = lease.child_handoff()
        popen_called = True
        process = popen_factory(
            list(launch.spec.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            pass_fds=(handoff.fd, nonce_read),
            env=_collector_attempt_child_environment(
                launch, lease_fd=handoff.fd, nonce_fd=nonce_read
            ),
        )
    except BaseException as exc:
        launcher_error = exc
    finally:
        if nonce_write >= 0:
            descriptor = nonce_write
            nonce_write = -1
            try:
                _close_descriptor_once(descriptor, label="nonce writer")
            except CollectorContinuityError:
                close_failed = True
        if nonce_read >= 0:
            descriptor = nonce_read
            nonce_read = -1
            try:
                _close_descriptor_once(descriptor, label="nonce reader")
            except CollectorContinuityError:
                close_failed = True
        if handoff is not None:
            try:
                handoff.close()
            except CollectorContinuityError:
                close_failed = True
    if launcher_error is not None:
        if not popen_called or popen_factory is subprocess.Popen:
            return _CollectorChildRun("not_invoked", None)
        raise CollectorContinuityError("collector child launcher outcome is indeterminate") from launcher_error
    if process is None:
        raise CollectorContinuityError("collector child launcher outcome is indeterminate")
    result = _drain_collector_child_output(process)
    if not result.process_result_known:
        raise CollectorContinuityError("collector child process result is indeterminate")
    if close_failed:
        result = _CollectorProcessResult(
            result.process_result_known,
            result.returncode,
            result.stdout_sha256,
            result.stdout_bytes,
            result.stderr_sha256,
            result.stderr_bytes,
            True,
        )
    return _CollectorChildRun("handle_obtained", result)


def _append_terminal_once(
    lease: CollectorPhaseLease,
    launch: _CollectorAttemptLaunch,
    *,
    event_type: Literal["ATTEMPT_COMPLETED", "ATTEMPT_FAILED"],
    event: Mapping[str, object],
) -> dict[str, object]:
    """Append one frozen terminal, reconciling a failed append without mutation."""

    _active_attempt_tail(lease, launch)
    history = _phase_ledger_history(lease)
    candidate = build_collector_ledger_event(
        previous_event=history[-1], event_type=event_type, event=event
    )
    payload = canonical_json_bytes(candidate) + b"\n"

    def current_tail() -> dict[str, object]:
        current = _phase_ledger_history(lease)
        tail = current[-1]
        if canonical_json_bytes(tail) + b"\n" == payload:
            return tail
        _active_attempt_tail(lease, launch)
        return tail

    for append_attempt in range(2):
        current = _phase_ledger_history(lease)
        if canonical_json_bytes(current[-1]) + b"\n" == payload:
            return current[-1]
        _active_attempt_tail(lease, launch)
        complete = (*current, candidate)
        _validate_ledger_chain(complete)
        try:
            _append_verified_ledger_payload(lease.ledger, payload, complete)
            return candidate
        except (CollectorContinuityError, OSError) as exc:
            try:
                tail = current_tail()
            except CollectorContinuityError:
                raise CollectorContinuityError("collector terminal append outcome is indeterminate") from exc
            if canonical_json_bytes(tail) + b"\n" == payload:
                return tail
            if append_attempt:
                raise CollectorContinuityError("collector terminal append cannot be confirmed") from exc
    raise AssertionError("collector terminal append retry loop escaped")


def _execute_collector_step_attempt(
    lease: CollectorPhaseLease,
    spec: CollectorStepSpec,
    *,
    popen_factory: Callable[..., object] = subprocess.Popen,
    now: Callable[[], str] = _collector_attempt_now,
) -> CollectorAttemptOutcome:
    """Execute one phase-held attempt. It never acquires or releases `lease`."""

    if _locate_dangling_collector_attempt(lease, spec) is not None:
        return _recover_dangling_collector_attempt(lease, spec, now=now)
    _attempt_history_for_spec(lease, spec, enforce_phase_order=True)
    launch = _begin_collector_step_attempt(lease, spec, now=now)
    terminal_appended = False
    try:
        child_run = _run_collector_child(lease, launch, popen_factory=popen_factory)
        process = child_run.process
        finished_at = now()
        _raw_timestamp(finished_at, "attempt finish")
        _active_attempt_tail(lease, launch)
        with open_registered_collector_read_connection(spec) as token:
            raw = verify_collector_raw_postcondition(
                token,
                spec,
                launch.baseline,
                attempt_started_at=launch.started_at,
                attempt_finished_at=finished_at,
            )
        _active_attempt_tail(lease, launch)
        classification = _classify_attempt_terminal(
            raw,
            process,
            process_launch_state=child_run.launch_state,
        )
        event_type, detail = _terminal_attempt_event(
            launch,
            raw,
            process,
            process_launch_state=child_run.launch_state,
            finished_at=finished_at,
            failure_classification=classification,
        )
        terminal = _append_terminal_once(
            lease, launch, event_type=event_type, event=detail
        )
        terminal_appended = True
        return CollectorAttemptOutcome(
            step_id=launch.spec.step_id,
            step_ordinal=launch.spec.step_ordinal,
            attempt_id=launch.attempt_id,
            terminal_event_sha256=str(terminal["event_sha256"]),
            terminal_event_type=event_type,
            classification="complete" if classification is None else classification,
            retryable=False if classification is None else _ATTEMPT_FAILURE_RETRYABILITY[classification],
            process_result_known=process is not None and process.process_result_known,
            returncode=None if process is None else process.returncode,
            raw_class=raw.raw_class,
        )
    finally:
        _clear_nonce(launch._nonce_buffer)
        launch.nonce = b""
        if not terminal_appended:
            # A missing terminal is intentionally left as a dangling start for
            # task 2.6; this slice never invents a recovery classification.
            pass


def _open_attempt_spec(
    history: Sequence[Mapping[str, object]],
    schedule: _FrozenCollectorStepSchedule,
) -> CollectorStepSpec | None:
    detail: Mapping[str, object] | None = None
    for event in history[2:]:
        event_type = event.get("event_type")
        candidate = event.get("event")
        if not isinstance(candidate, Mapping):
            raise CollectorContinuityError("collector phase ledger detail is invalid")
        if event_type == "ATTEMPT_STARTED":
            detail = candidate
        elif event_type in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
            detail = None
    if detail is None:
        return None
    ordinal = detail.get("step_ordinal")
    if type(ordinal) is not int or ordinal < 0 or ordinal >= len(schedule.specs):
        raise CollectorContinuityError("collector dangling step ordinal is invalid")
    spec = schedule.specs[ordinal]
    expected = {
        "registration_sha256": spec.registration_sha256,
        "session": spec.session,
        "phase": spec.phase,
        "step_id": spec.step_id,
        "step_ordinal": spec.step_ordinal,
        "command_sha256": spec.command_sha256,
    }
    if any(detail.get(field) != value for field, value in expected.items()):
        raise CollectorContinuityError("collector dangling step identity drifted")
    return spec


def _completed_collector_ordinals(
    history: Sequence[Mapping[str, object]],
) -> frozenset[int]:
    completed: set[int] = set()
    for event in history[2:]:
        if event.get("event_type") != "ATTEMPT_COMPLETED":
            continue
        detail = event.get("event")
        ordinal = detail.get("step_ordinal") if isinstance(detail, Mapping) else None
        if type(ordinal) is not int:
            raise CollectorContinuityError("collector completed step ordinal is invalid")
        completed.add(ordinal)
    return frozenset(completed)


def _verify_registered_collector_tail_state(
    lease: CollectorPhaseLease,
    schedule: _FrozenCollectorStepSchedule,
) -> None:
    history = _phase_ledger_history(lease)
    if len(history) == 2:
        spec = schedule.specs[0]
        with open_registered_collector_read_connection(spec) as token:
            state = snapshot_collector_step_state(token, spec)
        expected_counts = {table: 0 for table in COLLECTOR_STATE_TABLES}
        expected_counts["forward_capture_cohort"] = 1
        expected_counts["forward_collector_genesis"] = 1
        if (
            state["table_counts"] != expected_counts
            or state["receipt_id_high_water"] != 0
        ):
            raise CollectorContinuityError("collector initial logical state is not clean")
        return
    tail = history[-1]
    if tail.get("event_type") not in {"ATTEMPT_COMPLETED", "ATTEMPT_FAILED"}:
        raise CollectorContinuityError("collector phase tail requires recovery")
    detail = tail.get("event")
    ordinal = detail.get("step_ordinal") if isinstance(detail, Mapping) else None
    if type(ordinal) is not int or ordinal < 0 or ordinal >= len(schedule.specs):
        raise CollectorContinuityError("collector phase tail step is invalid")
    spec = schedule.specs[ordinal]
    expected = validate_collector_step_state(
        detail["step_state_after"], allowed_tables=spec.allowed_tables
    )
    with open_registered_collector_read_connection(spec) as token:
        actual = snapshot_collector_step_state(token, spec)
    if actual != expected or detail.get("state_after_sha256") != actual["collector_state_sha256"]:
        raise CollectorContinuityError("collector phase tail state drifted")


def _reverify_registered_collector_static_prerequisites(
    authority: Mapping[str, object], *, observed_at: datetime
) -> None:
    registration = authority.get("registration")
    if not isinstance(registration, Mapping):
        raise CollectorContinuityError("collector registration authority is invalid")
    prerequisites = registration.get("prerequisites")
    prerequisite_files = registration.get("prerequisite_files")
    sessions = authority.get("sessions")
    symbols = authority.get("symbols")
    registered_at = authority.get("registered_at")
    if (
        not isinstance(prerequisites, Mapping)
        or not isinstance(prerequisite_files, Mapping)
        or not isinstance(sessions, tuple)
        or not isinstance(symbols, tuple)
        or not isinstance(registered_at, datetime)
    ):
        raise CollectorContinuityError("collector static prerequisites are invalid")
    try:
        from stockdata.future_panel_registration import (
            FuturePanelRegistrationError,
            reverify_registration_prerequisites,
        )

        reverify_registration_prerequisites(
            prerequisites=prerequisites,
            prerequisite_files=prerequisite_files,
            database_file=str(authority["database_path"]),
            panel=tuple(
                sorted(f"{symbol}@{session}" for symbol in symbols for session in sessions)
            ),
            symbols=symbols,
            first_session=sessions[0],
            registered_at=registered_at,
            observed_at=observed_at,
        )
    except (FuturePanelRegistrationError, KeyError, TypeError, ValueError) as exc:
        raise CollectorContinuityError("collector static prerequisites drifted") from exc


def _complete_collector_materialization_history(
    lease: CollectorPhaseLease,
    schedule: _FrozenCollectorStepSchedule,
    *,
    registration_sha256: str,
    database_uuid: str,
) -> tuple[tuple[dict[str, object], ...], dict[str, object], dict[str, object]]:
    history = _phase_ledger_history(lease)
    return _validate_complete_collector_materialization_history(
        history,
        schedule,
        registration_sha256=registration_sha256,
        database_uuid=database_uuid,
    )


def _validate_complete_collector_materialization_history(
    history: tuple[dict[str, object], ...],
    schedule: _FrozenCollectorStepSchedule,
    *,
    registration_sha256: str,
    database_uuid: str,
) -> tuple[tuple[dict[str, object], ...], dict[str, object], dict[str, object]]:
    """Validate one parsed terminal history without consulting live paths."""

    if len(schedule.specs) != 12 or tuple(
        spec.step_ordinal for spec in schedule.specs
    ) != tuple(range(12)):
        raise CollectorContinuityError("collector materialization schedule is incomplete")
    completed_ordinals: set[int] = set()
    next_ordinal = 0
    previous_terminal_state: dict[str, object] | None = None
    previous_terminal_ordinal: int | None = None
    for event in history[2:]:
        event_type = event.get("event_type")
        if event_type in {
            "SQLITE_RECOVERY_STARTED",
            "SQLITE_RECOVERY_COMPLETED",
            "SQLITE_RECOVERY_FAILED",
        }:
            continue
        if event_type not in {
            "ATTEMPT_STARTED",
            "ATTEMPT_COMPLETED",
            "ATTEMPT_FAILED",
        }:
            raise CollectorContinuityError(
                "collector materialization ledger history is invalid"
            )
        detail = event.get("event")
        ordinal = detail.get("step_ordinal") if isinstance(detail, Mapping) else None
        if type(ordinal) is not int or ordinal != next_ordinal or ordinal >= 12:
            raise CollectorContinuityError(
                "collector materialization ledger order drifted"
            )
        spec = schedule.specs[ordinal]
        expected = {
            "registration_sha256": registration_sha256,
            "database_uuid": database_uuid,
            "session": spec.session,
            "phase": spec.phase,
            "step_id": spec.step_id,
            "step_ordinal": ordinal,
            "command_sha256": spec.command_sha256,
        }
        if not isinstance(detail, Mapping) or any(
            detail.get(field) != value for field, value in expected.items()
        ):
            raise CollectorContinuityError(
                "collector materialization ledger schedule drifted"
            )
        if event_type == "ATTEMPT_STARTED":
            state_before = validate_collector_step_state(
                detail.get("step_state_before"), allowed_tables=spec.allowed_tables
            )
            if previous_terminal_state is None:
                expected_counts = {table: 0 for table in COLLECTOR_STATE_TABLES}
                expected_counts["forward_capture_cohort"] = 1
                expected_counts["forward_collector_genesis"] = 1
                if (
                    state_before["table_counts"] != expected_counts
                    or state_before["receipt_id_high_water"] != 0
                ):
                    raise CollectorContinuityError(
                        "collector materialization initial state is invalid"
                    )
            elif previous_terminal_ordinal == ordinal:
                if state_before != previous_terminal_state:
                    raise CollectorContinuityError(
                        "collector materialization retry state chain drifted"
                    )
            elif previous_terminal_ordinal == ordinal - 1:
                global_fields = {
                    "schema_version",
                    "collector_state_sha256",
                    "table_counts",
                    "table_sha256",
                    "receipt_id_high_water",
                }
                if any(
                    state_before[field] != previous_terminal_state[field]
                    for field in global_fields
                ):
                    raise CollectorContinuityError(
                        "collector materialization state chain drifted"
                    )
            else:
                raise CollectorContinuityError(
                    "collector materialization state chain order drifted"
                )
        terminal_state: dict[str, object] | None = None
        if event_type in {"ATTEMPT_FAILED", "ATTEMPT_COMPLETED"}:
            terminal_state = validate_collector_step_state(
                detail.get("step_state_after"), allowed_tables=spec.allowed_tables
            )
            attempt_state_before = validate_collector_step_state(
                detail.get("step_state_before"), allowed_tables=spec.allowed_tables
            )
            if (
                terminal_state["outside_scope_sha256"]
                != attempt_state_before["outside_scope_sha256"]
            ):
                raise CollectorContinuityError(
                    "collector materialization outside-scope state drifted"
                )
        if event_type == "ATTEMPT_FAILED":
            if detail.get("retryable") is not True:
                raise CollectorContinuityError(
                    "collector materialization ledger is quarantined"
                )
            if terminal_state is None:
                raise CollectorContinuityError(
                    "collector materialization terminal state is missing"
                )
            previous_terminal_state = terminal_state
            previous_terminal_ordinal = ordinal
            continue
        if event_type == "ATTEMPT_COMPLETED":
            if ordinal in completed_ordinals:
                raise CollectorContinuityError(
                    "collector materialization step completed more than once"
                )
            completed_ordinals.add(ordinal)
            next_ordinal += 1
            if terminal_state is None:
                raise CollectorContinuityError(
                    "collector materialization terminal state is missing"
                )
            previous_terminal_state = terminal_state
            previous_terminal_ordinal = ordinal
    if completed_ordinals != set(range(12)) or next_ordinal != 12:
        raise CollectorContinuityError("collector materialization ledger is incomplete")
    tail = history[-1]
    tail_detail = tail.get("event")
    if (
        tail.get("event_type") != "ATTEMPT_COMPLETED"
        or not isinstance(tail_detail, Mapping)
        or tail_detail.get("step_ordinal") != 11
    ):
        raise CollectorContinuityError("collector materialization ledger tail is invalid")
    tail_state = validate_collector_step_state(
        tail_detail.get("step_state_after"),
        allowed_tables=schedule.specs[11].allowed_tables,
    )
    if tail_detail.get("state_after_sha256") != tail_state["collector_state_sha256"]:
        raise CollectorContinuityError("collector materialization ledger tail state is invalid")
    logical_state = validate_collector_logical_state(
        {
            "schema_version": COLLECTOR_LOGICAL_STATE_SCHEMA,
            "collector_state_sha256": tail_state["collector_state_sha256"],
            "table_counts": tail_state["table_counts"],
        }
    )
    return history, tail_state, logical_state


def _create_collector_snapshot_staging(
    path: str | os.PathLike[str],
) -> tuple[str, int, int, str]:
    parent_path = lexical_absolute_path(
        os.path.abspath(os.path.expanduser(os.fspath(path)))
    )
    parent_fd, _, _ = _open_parent(
        os.path.join(parent_path, ".collector-snapshot-parent")
    )
    leaf = f".collector-snapshot-{secrets.token_hex(32)}"
    canonical = os.path.join(parent_path, leaf)
    directory_fd = -1
    created = False
    try:
        os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        created = True
        directory_fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
        os.fchmod(directory_fd, 0o700)
        status = os.fstat(directory_fd)
        if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o700:
            raise CollectorContinuityError(
                "collector materialization staging is not private"
            )
        os.fsync(directory_fd)
        os.fsync(parent_fd)
        return canonical, parent_fd, directory_fd, leaf
    except FileExistsError as exc:
        os.close(parent_fd)
        raise CollectorContinuityError(
            "collector materialization staging collides with an existing entry"
        ) from exc
    except BaseException:
        if directory_fd >= 0:
            os.close(directory_fd)
        if created:
            try:
                os.rmdir(leaf, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        raise


def _cleanup_collector_snapshot_staging(
    parent_fd: int, directory_fd: int, leaf: str
) -> None:
    errors: list[BaseException] = []
    try:
        names = os.listdir(directory_fd)
    except BaseException as exc:
        names = []
        errors.append(exc)
    for name in names:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                os.rmdir(name, dir_fd=directory_fd)
            else:
                if stat.S_ISREG(status.st_mode):
                    try:
                        os.chmod(
                            name,
                            0o600,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except (NotImplementedError, OSError):
                        pass
                os.unlink(name, dir_fd=directory_fd)
        except BaseException as exc:
            errors.append(exc)
    try:
        os.fsync(directory_fd)
    except BaseException as exc:
        errors.append(exc)
    try:
        os.rmdir(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise _collector_cleanup_error(
            "collector materialization staging cleanup failed",
            tuple(f"cleanup_{index}" for index in range(len(errors))),
            tuple(errors),
        )


def _write_all_collector_artifact(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise CollectorContinuityError("collector materialization artifact write failed")
        offset += written


def _write_collector_snapshot_artifact(
    directory_fd: int, staging_path: str, raw: bytes
) -> str:
    identifier = hashlib.sha256(raw).hexdigest()
    descriptor = create_exclusive_regular_file(directory_fd, identifier)
    try:
        _write_all_collector_artifact(descriptor, raw)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)
    return os.path.join(staging_path, identifier)


def _sha256_open_descriptor(descriptor: int) -> str:
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_size < 0:
        raise CollectorContinuityError("collector snapshot database is not regular")
    digest = hashlib.sha256()
    offset = 0
    while offset < status.st_size:
        chunk = os.pread(descriptor, min(1024 * 1024, status.st_size - offset), offset)
        if not chunk:
            raise CollectorContinuityError("collector snapshot database was truncated")
        digest.update(chunk)
        offset += len(chunk)
    current = os.fstat(descriptor)
    if (
        current.st_size != status.st_size
        or current.st_dev != status.st_dev
        or current.st_ino != status.st_ino
    ):
        raise CollectorContinuityError("collector snapshot database changed while hashing")
    return digest.hexdigest()


def _verify_collector_snapshot_temp_identity(
    descriptor: int, directory_fd: int, path: str
) -> PhysicalFileIdentity:
    expected = _legacy_identity(
        _identity_from_open_file(descriptor, directory_fd, path)
    )
    opened = open_existing_regular_file(path)
    try:
        if opened.identity != expected:
            raise CollectorContinuityError(
                "collector snapshot database identity drifted"
            )
    finally:
        opened.close()
    return expected


def _backup_registered_collector_database(
    token: CollectorReadToken,
    spec: CollectorStepSpec,
    *,
    snapshot_path: str,
) -> None:
    destination: sqlite3.Connection | None = None
    body_error: BaseException | None = None
    try:
        destination = sqlite3.connect(
            _sqlite_uri(snapshot_path, "rw"), uri=True, isolation_level=None
        )
        destination.execute("PRAGMA foreign_keys=ON")
        destination.execute(f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}")
        destination.execute("PRAGMA synchronous=FULL")
        destination.execute("PRAGMA journal_mode=DELETE")
        with _borrow_registered_collector_read_connection(token, spec) as source:
            source.backup(destination)
    except BaseException as exc:
        body_error = exc
    close_error: BaseException | None = None
    if destination is not None:
        try:
            destination.close()
        except BaseException as exc:
            close_error = exc
    if body_error is not None:
        if close_error is not None:
            raise _combine_collector_context_errors(body_error, close_error)
        if isinstance(body_error, CollectorContinuityError):
            raise body_error
        raise CollectorContinuityError("collector SQLite backup failed") from body_error
    if close_error is not None:
        raise CollectorContinuityError("collector SQLite backup cannot be closed") from close_error
    _reject_registered_collector_read_sidecars(snapshot_path)


def _verify_collector_snapshot_database(
    path: str,
    spec: CollectorStepSpec,
    *,
    capability: Mapping[str, object],
    expected_step_state: Mapping[str, object],
    expected_logical_state: Mapping[str, object],
) -> None:
    _reject_registered_collector_read_sidecars(path)
    frozen_schedule = _validate_raw_step_spec(spec)
    opened = open_nofollow_regular(path)
    connection: sqlite3.Connection | None = None
    body_error: BaseException | None = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(path, safe='/')}?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = None
        connection.text_factory = str
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=1")
        _verify_collector_snapshot_connection(
            connection,
            spec,
            frozen_schedule,
            capability=capability,
            expected_step_state=expected_step_state,
            expected_logical_state=expected_logical_state,
        )
    except BaseException as exc:
        body_error = exc
    close_error: BaseException | None = None
    if connection is not None:
        try:
            connection.close()
        except BaseException as exc:
            close_error = exc
    try:
        opened.close()
    except BaseException as exc:
        close_error = (
            exc
            if close_error is None
            else _combine_collector_context_errors(close_error, exc)
        )
    if body_error is not None:
        if close_error is not None:
            raise _combine_collector_context_errors(body_error, close_error)
        if isinstance(body_error, CollectorContinuityError):
            raise body_error
        raise CollectorContinuityError("collector snapshot verification failed") from body_error
    if close_error is not None:
        raise CollectorContinuityError("collector snapshot verification cleanup failed") from close_error
    _reject_registered_collector_read_sidecars(path)


def _verify_collector_snapshot_connection(
    connection: sqlite3.Connection,
    spec: CollectorStepSpec,
    schedule: _FrozenCollectorStepSchedule,
    *,
    capability: Mapping[str, object],
    expected_step_state: Mapping[str, object],
    expected_logical_state: Mapping[str, object],
) -> None:
    """Verify snapshot SQLite semantics on an already controlled connection."""

    if (
        str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        != "delete"
        or connection.execute("PRAGMA main.foreign_key_check").fetchone()
        is not None
        or connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)
    ):
        raise CollectorContinuityError("collector snapshot SQLite contract is invalid")
    verify_collector_authority_schema(connection)
    cohort, cohort_sha256 = _read_prepared_cohort(connection)
    expected_cohort = {
        "symbols": list(schedule.specs[0].symbols),
        "start": schedule.cohort_start,
        "source": schedule.source,
        "adjustment_mode": schedule.adjustment_mode,
        "adjustment_version": schedule.adjustment_version,
    }
    rows = connection.execute(
        "SELECT database_uuid,cohort_sha256,genesis_json,genesis_sha256,"
        "ledger_genesis_event_sha256,created_at "
        "FROM main.forward_collector_genesis"
    ).fetchall()
    if len(rows) != 1 or len(rows[0]) != 6:
        raise CollectorContinuityError("collector snapshot genesis is invalid")
    row = rows[0]
    try:
        genesis = _validate_prepared_genesis(
            decode_canonical_json_object(str(row[2]).encode("ascii"))
        )
    except (UnicodeEncodeError, CollectorContinuityError) as exc:
        raise CollectorContinuityError("collector snapshot genesis is invalid") from exc
    if (
        cohort != expected_cohort
        or cohort_sha256 != capability.get("cohort_sha256")
        or row[0] != capability.get("database_uuid")
        or row[1] != cohort_sha256
        or row[3] != capability.get("genesis_sha256")
        or row[4] != capability.get("ledger_genesis_event_sha256")
        or _prepared_schema_sha256(connection)
        != capability.get("collector_schema_sha256")
        or genesis.get("database_uuid") != capability.get("database_uuid")
        or genesis.get("cohort_sha256") != capability.get("cohort_sha256")
        or genesis.get("database_identity") != capability.get("database_identity")
        or genesis.get("ledger_identity") != capability.get("ledger_identity")
        or genesis.get("created_at") != row[5]
        or genesis.get("collector_schema_sha256")
        != capability.get("collector_schema_sha256")
        or canonical_json_sha256(genesis) != capability.get("genesis_sha256")
    ):
        raise CollectorContinuityError("collector snapshot authority drifted")
    step_state = _snapshot_collector_step_state_for_schedule(
        connection, spec, schedule
    )
    logical_state = compute_collector_logical_state(connection)
    if step_state != expected_step_state or logical_state != expected_logical_state:
        raise CollectorContinuityError("collector snapshot logical state drifted")


_SNAPSHOT_SEMANTIC_READ_PRAGMAS: Final = (
    _REGISTERED_COLLECTOR_READ_PRAGMAS | frozenset({"integrity_check"})
)


def _snapshot_semantic_read_authorizer(
    action: int,
    argument_1: str | None,
    argument_2: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    del database_name, trigger_name
    if action in _REGISTERED_COLLECTOR_READ_DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == _REGISTERED_COLLECTOR_READ_PRAGMA_ACTION:
        if argument_1 not in _SNAPSHOT_SEMANTIC_READ_PRAGMAS:
            return sqlite3.SQLITE_DENY
        if argument_2 is not None and not (
            (argument_1 == "query_only" and argument_2 in {"1", "ON", "on"})
            or (argument_1 == "foreign_keys" and argument_2 in {"1", "ON", "on"})
            or (
                argument_1 == "busy_timeout"
                and argument_2 == str(COLLECTOR_BUSY_TIMEOUT_MS)
            )
        ):
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _validate_registered_collector_exact_panel(
    raw: bytes, authority: Mapping[str, object]
) -> None:
    """Bind one canonical exact-panel artifact to registration authority."""

    if not isinstance(raw, bytes):
        raise CollectorContinuityError("collector snapshot exact panel bytes are invalid")
    try:
        panel = json.loads(
            raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorContinuityError(
            "collector snapshot exact panel is invalid"
        ) from exc
    sessions = authority.get("sessions")
    symbols = authority.get("symbols")
    registration = authority.get("registration")
    if (
        not isinstance(sessions, tuple)
        or not isinstance(symbols, tuple)
        or not isinstance(registration, Mapping)
    ):
        raise CollectorContinuityError("collector snapshot registration is invalid")
    expected = sorted(
        f"{symbol}@{session}" for symbol in symbols for session in sessions
    )
    if (
        type(panel) is not list
        or panel != expected
        or raw != canonical_json_bytes(panel)
        or hashlib.sha256(raw).hexdigest() != registration.get("panel_sha256")
    ):
        raise CollectorContinuityError(
            "collector snapshot exact panel drifted from registration"
        )


def verify_registered_collector_materialization_snapshot(
    registration_raw: bytes,
    ledger_raw: bytes,
    continuity_closure_raw: bytes,
    snapshot_database: OpenedRegularFile,
    snapshot_database_reference: Mapping[str, object],
    *,
    exact_panel_raw: bytes,
) -> None:
    """Reverify one bundled collector snapshot without granting readiness."""

    require_collector_continuity_health()
    if not isinstance(registration_raw, bytes):
        raise CollectorContinuityError("collector snapshot registration bytes are invalid")
    if not isinstance(ledger_raw, bytes):
        raise CollectorContinuityError("collector snapshot ledger bytes are invalid")
    if not isinstance(continuity_closure_raw, bytes):
        raise CollectorContinuityError("collector snapshot closure bytes are invalid")
    if not isinstance(snapshot_database, OpenedRegularFile):
        raise CollectorContinuityError("collector snapshot database authority is invalid")
    if not isinstance(snapshot_database_reference, Mapping):
        raise CollectorContinuityError("collector snapshot database reference is invalid")

    authority = _decode_registered_schedule_authority(registration_raw)
    _validate_registered_collector_exact_panel(exact_panel_raw, authority)
    history = parse_collector_ledger(ledger_raw)
    _validate_registered_schedule_ledger(authority, history)
    schedule = _build_collector_step_schedule(
        authority,
        registration_file=str(authority["database_path"]),
    )
    capability = authority.get("capability")
    if not isinstance(capability, Mapping):
        raise CollectorContinuityError("collector snapshot capability is invalid")
    history, tail_state, logical_state = (
        _validate_complete_collector_materialization_history(
            history,
            schedule,
            registration_sha256=str(authority["registration_sha256"]),
            database_uuid=str(capability["database_uuid"]),
        )
    )
    closure = decode_collector_continuity_closure(continuity_closure_raw)
    reference = decode_canonical_json_object(
        canonical_json_bytes(dict(snapshot_database_reference))
    )
    require_exact_keys(
        reference, _CLOSURE_REFERENCE_FIELDS, "collector snapshot database reference"
    )
    if (
        reference.get("kind") != SNAPSHOT_DATABASE_REFERENCE_KIND
        or reference.get("schema_version") != SNAPSHOT_DATABASE_REFERENCE_SCHEMA
    ):
        raise CollectorContinuityError("collector snapshot database reference is invalid")
    _require_event_sha256(
        reference.get("identifier"), "collector snapshot database identifier"
    )
    tail = history[-1]
    if closure != {
        "schema_version": CLOSURE_SCHEMA,
        "live_database_identity": capability["database_identity"],
        "live_ledger_identity": capability["ledger_identity"],
        "database_uuid": capability["database_uuid"],
        "registration_sha256": authority["registration_sha256"],
        "ledger_head": {
            "seq": tail["seq"],
            "event_type": tail["event_type"],
            "event_sha256": tail["event_sha256"],
        },
        "logical_state": tail_state,
        "snapshot_database_reference": reference,
    }:
        raise CollectorContinuityError("collector snapshot closure binding drifted")

    path = snapshot_database.identity.canonical_path
    _reject_registered_collector_read_sidecars(path)
    status = os.fstat(snapshot_database.descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or int(status.st_dev) != snapshot_database.identity.file_st_dev
        or int(status.st_ino) != snapshot_database.identity.file_st_ino
    ):
        raise CollectorContinuityError("collector snapshot database identity drifted")
    verify_file_identity(path, snapshot_database.identity)
    if _sha256_open_descriptor(snapshot_database.descriptor) != reference["identifier"]:
        raise CollectorContinuityError("collector snapshot database content drifted")

    duplicate = -1
    connection: sqlite3.Connection | None = None
    body_error: BaseException | None = None
    try:
        duplicate = os.dup(snapshot_database.descriptor)
        os.set_inheritable(duplicate, False)
        locator = f"/dev/fd/{duplicate}"
        connection = sqlite3.connect(
            f"file:{locator}?mode=ro&immutable=1&cache=private",
            uri=True,
            check_same_thread=True,
            isolation_level=None,
        )
        connection.row_factory = None
        connection.text_factory = str
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={COLLECTOR_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=1")
        connection.set_authorizer(_snapshot_semantic_read_authorizer)
        database_list = connection.execute("PRAGMA database_list").fetchall()
        if database_list != [(0, "main", locator)]:
            raise CollectorContinuityError(
                "collector snapshot database locator drifted"
            )
        if (
            connection.execute("PRAGMA query_only").fetchone() != (1,)
            or connection.execute("PRAGMA foreign_keys").fetchone() != (1,)
            or connection.execute("PRAGMA busy_timeout").fetchone()
            != (COLLECTOR_BUSY_TIMEOUT_MS,)
        ):
            raise CollectorContinuityError(
                "collector snapshot read connection contract drifted"
            )
        _verify_collector_snapshot_connection(
            connection,
            schedule.specs[11],
            schedule,
            capability=capability,
            expected_step_state=tail_state,
            expected_logical_state=logical_state,
        )
    except BaseException as exc:
        body_error = exc
    cleanup_errors: list[BaseException] = []
    if connection is not None:
        try:
            connection.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
    if duplicate >= 0:
        try:
            os.close(duplicate)
        except BaseException as exc:
            cleanup_errors.append(exc)
    if body_error is not None:
        if cleanup_errors:
            raise _combine_collector_context_errors(
                body_error,
                _collector_cleanup_error(
                    "collector snapshot verification cleanup failed",
                    tuple(f"cleanup_{index}" for index in range(len(cleanup_errors))),
                    tuple(cleanup_errors),
                ),
            )
        if isinstance(body_error, CollectorContinuityError):
            raise body_error
        raise CollectorContinuityError(
            "collector snapshot semantic verification failed"
        ) from body_error
    if cleanup_errors:
        raise _collector_cleanup_error(
            "collector snapshot verification cleanup failed",
            tuple(f"cleanup_{index}" for index in range(len(cleanup_errors))),
            tuple(cleanup_errors),
        )
    _reject_registered_collector_read_sidecars(path)
    verify_file_identity(path, snapshot_database.identity)
    if _sha256_open_descriptor(snapshot_database.descriptor) != reference["identifier"]:
        raise CollectorContinuityError("collector snapshot database content drifted")


def _read_registered_materialization_bytes(
    authority: Mapping[str, object],
    lease: CollectorPhaseLease,
    history: Sequence[Mapping[str, object]],
) -> tuple[bytes, bytes]:
    registration_file = str(authority["registration_file"])
    opened = open_nofollow_regular(registration_file)
    try:
        size = os.fstat(opened.descriptor).st_size
        if size <= 0 or size > 1_048_576:
            raise CollectorContinuityError("collector registration snapshot size is invalid")
        registration_raw = os.pread(opened.descriptor, size, 0)
        if len(registration_raw) != size:
            raise CollectorContinuityError("collector registration snapshot was truncated")
        verify_file_identity(registration_file, opened.identity)
    finally:
        opened.close()
    if (
        hashlib.sha256(registration_raw).hexdigest()
        != authority.get("registration_sha256")
        or decode_canonical_json_object(registration_raw) != authority.get("registration")
    ):
        raise CollectorContinuityError("collector registration snapshot drifted")
    lease.verify()
    ledger_raw = _ledger_source_bytes(lease.ledger)
    if parse_collector_ledger(ledger_raw) != tuple(history):
        raise CollectorContinuityError("collector ledger snapshot drifted")
    lease.verify()
    return registration_raw, ledger_raw


def _reverify_collector_materialization_live_state(
    registration_file: str,
    lease: CollectorPhaseLease,
    schedule: _FrozenCollectorStepSchedule,
    *,
    authority: Mapping[str, object],
    history: Sequence[Mapping[str, object]],
    expected_step_state: Mapping[str, object],
) -> None:
    _reject_registered_collector_read_sidecars(schedule.database_path)
    refreshed = _read_bound_registration(registration_file)
    if any(
        refreshed.get(field) != authority.get(field)
        for field in (
            "registration_sha256",
            "database_path",
            "ledger_path",
            "ledger_identity",
            "capability",
            "registration",
        )
    ):
        raise CollectorContinuityError("collector materialization authority drifted")
    if _phase_ledger_history(lease) != tuple(history):
        raise CollectorContinuityError("collector materialization ledger head drifted")
    with open_registered_collector_read_connection(schedule.specs[11]) as token:
        current = snapshot_collector_step_state(token, schedule.specs[11])
    if current != expected_step_state:
        raise CollectorContinuityError("collector materialization live state drifted")
    _reject_registered_collector_read_sidecars(schedule.database_path)


def create_registered_collector_materialization_snapshot(
    registration_file: str | os.PathLike[str],
    *,
    database: str | os.PathLike[str],
    staging_directory: str | os.PathLike[str],
) -> dict[str, object]:
    """Create one durable snapshot in a new private child of the staging parent."""

    require_collector_continuity_health()
    canonical_registration = lexical_absolute_path(
        os.path.abspath(os.path.expanduser(os.fspath(registration_file)))
    )
    canonical_database = lexical_absolute_path(
        os.path.abspath(os.path.expanduser(os.fspath(database)))
    )
    bootstrap = _read_registered_schedule_authority(canonical_registration)
    if bootstrap.get("database_path") != canonical_database:
        raise CollectorContinuityError("collector materialization database differs from registration")
    ledger_path = bootstrap.get("ledger_path")
    if not isinstance(ledger_path, str):
        raise CollectorContinuityError("collector materialization ledger is invalid")
    lease = acquire_collector_phase_lease(ledger_path)
    with lease:
        _reject_registered_collector_read_sidecars(canonical_database)
        specs = freeze_collector_step_schedule(
            registration_file=canonical_registration
        )
        schedule = _FROZEN_COLLECTOR_STEP_SCHEDULES.get(specs[0].schedule_sha256)
        if (
            schedule is None
            or schedule.database_path != canonical_database
            or schedule.ledger_path != ledger_path
            or schedule.ledger_identity != lease.verify()
        ):
            raise CollectorContinuityError(
                "collector materialization schedule authority drifted"
            )
        authority = _read_bound_registration(canonical_registration)
        if any(
            authority.get(field) != bootstrap.get(field)
            for field in (
                "registration_sha256",
                "database_path",
                "ledger_path",
                "ledger_identity",
                "capability",
                "registration",
            )
        ):
            raise CollectorContinuityError("collector materialization authority drifted")
        capability = authority.get("capability")
        if not isinstance(capability, Mapping):
            raise CollectorContinuityError("collector materialization capability is invalid")
        history, tail_state, logical_state = _complete_collector_materialization_history(
            lease,
            schedule,
            registration_sha256=str(authority["registration_sha256"]),
            database_uuid=str(capability["database_uuid"]),
        )
        with open_registered_collector_read_connection(specs[11]) as token:
            live_state = snapshot_collector_step_state(token, specs[11])
        if live_state != tail_state:
            raise CollectorContinuityError("collector materialization live tail drifted")

        staging_path, parent_fd, directory_fd, staging_leaf = (
            _create_collector_snapshot_staging(staging_directory)
        )
        result: dict[str, object] | None = None
        body_error: BaseException | None = None
        try:
            temporary_leaf = f".database-{secrets.token_hex(16)}.sqlite"
            temporary_fd = create_exclusive_regular_file(directory_fd, temporary_leaf)
            temporary_path = os.path.join(staging_path, temporary_leaf)
            try:
                os.fchmod(temporary_fd, 0o600)
                if stat.S_IMODE(os.fstat(temporary_fd).st_mode) != 0o600:
                    raise CollectorContinuityError(
                        "collector snapshot temporary database is not private"
                    )
                temporary_identity = _verify_collector_snapshot_temp_identity(
                    temporary_fd, directory_fd, temporary_path
                )
                with open_registered_collector_read_connection(specs[11]) as token:
                    before_backup = snapshot_collector_step_state(token, specs[11])
                    if before_backup != tail_state:
                        raise CollectorContinuityError(
                            "collector materialization live state drifted before backup"
                        )
                    _backup_registered_collector_database(
                        token, specs[11], snapshot_path=temporary_path
                    )
                    after_backup = snapshot_collector_step_state(token, specs[11])
                if after_backup != tail_state:
                    raise CollectorContinuityError(
                        "collector materialization live state drifted during backup"
                    )
                _reject_registered_collector_read_sidecars(canonical_database)
                if _verify_collector_snapshot_temp_identity(
                    temporary_fd, directory_fd, temporary_path
                ) != temporary_identity:
                    raise CollectorContinuityError(
                        "collector snapshot database identity drifted during backup"
                    )
                os.fsync(temporary_fd)
                database_sha256 = _sha256_open_descriptor(temporary_fd)
                _verify_collector_snapshot_database(
                    temporary_path,
                    specs[11],
                    capability=capability,
                    expected_step_state=tail_state,
                    expected_logical_state=logical_state,
                )
                try:
                    os.stat(
                        database_sha256,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise CollectorContinuityError(
                        "collector snapshot database content address collides"
                    )
                os.fchmod(temporary_fd, 0o400)
                os.fsync(temporary_fd)
                os.replace(
                    temporary_leaf,
                    database_sha256,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                os.close(temporary_fd)
            snapshot_database_path = os.path.join(staging_path, database_sha256)
            _reject_registered_collector_read_sidecars(snapshot_database_path)
            snapshot_opened = open_nofollow_regular(snapshot_database_path)
            try:
                if (
                    stat.S_IMODE(os.fstat(snapshot_opened.descriptor).st_mode) != 0o400
                    or _sha256_open_descriptor(snapshot_opened.descriptor)
                    != database_sha256
                ):
                    raise CollectorContinuityError(
                        "collector snapshot database finalization drifted"
                    )
            finally:
                snapshot_opened.close()

            _reverify_collector_materialization_live_state(
                canonical_registration,
                lease,
                schedule,
                authority=authority,
                history=history,
                expected_step_state=tail_state,
            )
            registration_raw, ledger_raw = _read_registered_materialization_bytes(
                authority, lease, history
            )
            registration_path = _write_collector_snapshot_artifact(
                directory_fd, staging_path, registration_raw
            )
            ledger_snapshot_path = _write_collector_snapshot_artifact(
                directory_fd, staging_path, ledger_raw
            )
            _reverify_collector_materialization_live_state(
                canonical_registration,
                lease,
                schedule,
                authority=authority,
                history=history,
                expected_step_state=tail_state,
            )

            database_reference = {
                "kind": SNAPSHOT_DATABASE_REFERENCE_KIND,
                "identifier": database_sha256,
                "schema_version": SNAPSHOT_DATABASE_REFERENCE_SCHEMA,
            }
            tail = history[-1]
            closure = validate_collector_continuity_closure(
                {
                    "schema_version": CLOSURE_SCHEMA,
                    "live_database_identity": capability["database_identity"],
                    "live_ledger_identity": capability["ledger_identity"],
                    "database_uuid": capability["database_uuid"],
                    "registration_sha256": authority["registration_sha256"],
                    "ledger_head": {
                        "seq": tail["seq"],
                        "event_type": tail["event_type"],
                        "event_sha256": tail["event_sha256"],
                    },
                    "logical_state": tail_state,
                    "snapshot_database_reference": database_reference,
                }
            )
            closure_raw = canonical_json_bytes(closure)
            closure_path = _write_collector_snapshot_artifact(
                directory_fd, staging_path, closure_raw
            )
            closure_sha256 = hashlib.sha256(closure_raw).hexdigest()
            os.fsync(directory_fd)
            os.fsync(parent_fd)
            result = {
                "staging_directory": staging_path,
                "database": {
                    "path": snapshot_database_path,
                    "reference": database_reference,
                },
                "registration": {
                    "path": registration_path,
                    "sha256": hashlib.sha256(registration_raw).hexdigest(),
                },
                "ledger": {
                    "path": ledger_snapshot_path,
                    "sha256": hashlib.sha256(ledger_raw).hexdigest(),
                },
                "continuity_closure": {
                    "path": closure_path,
                    "reference": {
                        "kind": CONTINUITY_CLOSURE_REFERENCE_KIND,
                        "identifier": closure_sha256,
                        "schema_version": CLOSURE_SCHEMA,
                    },
                },
            }
        except BaseException as exc:
            body_error = exc
        cleanup_error: BaseException | None = None
        if body_error is not None:
            try:
                _cleanup_collector_snapshot_staging(
                    parent_fd, directory_fd, staging_leaf
                )
            except BaseException as exc:
                cleanup_error = exc
        close_errors: list[BaseException] = []
        for descriptor in (directory_fd, parent_fd):
            try:
                os.close(descriptor)
            except BaseException as exc:
                close_errors.append(exc)
        for exc in close_errors:
            cleanup_error = (
                exc
                if cleanup_error is None
                else _combine_collector_context_errors(cleanup_error, exc)
            )
        if body_error is not None:
            if cleanup_error is not None:
                raise _combine_collector_context_errors(body_error, cleanup_error)
            raise body_error
        if cleanup_error is not None:
            raise cleanup_error
        if result is None:
            raise CollectorContinuityError("collector materialization snapshot is unavailable")
        return result


def _require_collector_attempt_window(spec: CollectorStepSpec) -> datetime:
    observed_at = _raw_timestamp(_collector_attempt_now(), "phase observation")
    local = observed_at.astimezone(_SHANGHAI)
    if local.date().isoformat() != spec.session:
        raise CollectorContinuityError("collector phase session is not current")
    if spec.phase == "pre_open":
        valid = _PREOPEN_START <= local.time() < _PREOPEN_END
    else:
        valid = local.time() >= _POST_CLOSE_START
    if not valid:
        raise CollectorContinuityError("collector phase window is closed")
    return observed_at


def execute_registered_collector_phase(
    registration_file: str | os.PathLike[str],
    *,
    database: str | os.PathLike[str],
    effective_date: str,
    phase: str,
) -> tuple[CollectorAttemptOutcome, ...]:
    """Execute one registered phase under the sole complete-phase lease owner."""

    require_collector_continuity_health()
    prelease = _read_registered_schedule_authority(registration_file)
    ledger_path = prelease.get("ledger_path")
    if not isinstance(ledger_path, str):
        raise CollectorContinuityError("collector registered ledger is invalid")
    lease = acquire_collector_phase_lease(ledger_path)
    with lease:
        specs = freeze_collector_step_schedule(registration_file=registration_file)
        schedule = _FROZEN_COLLECTOR_STEP_SCHEDULES.get(specs[0].schedule_sha256)
        if (
            schedule is None
            or schedule.ledger_path != ledger_path
            or schedule.ledger_identity != lease.verify()
            or schedule.registration_sha256 != prelease.get("registration_sha256")
        ):
            raise CollectorContinuityError("collector registered schedule is unavailable")
        history = _phase_ledger_history(lease)
        dangling_spec = _open_attempt_spec(history, schedule)
        recovered: CollectorAttemptOutcome | None = None
        if dangling_spec is not None:
            recovered = _recover_dangling_collector_attempt(lease, dangling_spec)
            if recovered.terminal_event_type != "ATTEMPT_COMPLETED":
                raise CollectorContinuityError(
                    "collector recovered attempt failed; retry requires a new invocation"
                )

        authority = _read_bound_registration(registration_file)
        canonical_database = lexical_absolute_path(database)
        if canonical_database != schedule.database_path or authority["database_path"] != canonical_database:
            raise CollectorContinuityError("collector phase database differs from registration")
        try:
            requested_date = _validate_collector_session(effective_date)
        except CollectorContinuityError:
            raise
        if requested_date not in schedule.sessions:
            raise CollectorContinuityError("collector phase session is not registered")
        if phase not in {"pre_open", "post_close"}:
            raise CollectorContinuityError("collector phase is invalid")
        target = tuple(
            spec
            for spec in schedule.specs
            if spec.session == requested_date and spec.phase == phase
        )
        if len(target) != 2:
            raise CollectorContinuityError("collector registered phase schedule is invalid")

        observed_at = _require_collector_attempt_window(target[0])
        _reverify_registered_collector_static_prerequisites(
            authority, observed_at=observed_at
        )
        _verify_registered_collector_tail_state(lease, schedule)

        history = _phase_ledger_history(lease)
        completed = _completed_collector_ordinals(history)
        target_ordinals = {spec.step_ordinal for spec in target}
        if target_ordinals.issubset(completed):
            raise CollectorContinuityError("collector phase is already complete")
        outcomes: list[CollectorAttemptOutcome] = []
        if recovered is not None and recovered.step_ordinal in target_ordinals:
            outcomes.append(recovered)
        for spec in target:
            if spec.step_ordinal in completed:
                continue
            observed_at = _require_collector_attempt_window(spec)
            _reverify_registered_collector_static_prerequisites(
                authority, observed_at=observed_at
            )
            outcome = _execute_collector_step_attempt(lease, spec)
            if outcome.terminal_event_type != "ATTEMPT_COMPLETED":
                raise CollectorContinuityError("collector phase attempt failed")
            outcomes.append(outcome)
            completed = _completed_collector_ordinals(_phase_ledger_history(lease))
        if not target_ordinals.issubset(completed):
            raise CollectorContinuityError("collector phase is incomplete")
        _verify_registered_collector_tail_state(lease, schedule)
        return tuple(outcomes)


execute_collector_step_attempt = _execute_collector_step_attempt
