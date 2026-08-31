"""Bounded, shadow-only capture of a QMT v2 loopback transport snapshot.

This module does not import QMT, write a cache, or select data for decisions.
It captures a shared-token-accessed, unverified transport response as explicit
evidence so an offline consumer can reject stale, partial, or unbound data.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "qmt-transport-snapshot/2"
REQUEST_SCHEMA_VERSION = "qmt-transport-request/2"
ACK_SCHEMA_VERSION = "qmt-transport-ack/2"
PERMITTED_USES = ("offline_replay", "shadow_compare")
FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAX_SYMBOLS = 20
MAX_COUNT = 5000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PRODUCT_BYTES = 32 * 1024 * 1024
MAX_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_WAIT_TIMEOUT_SECONDS = 300.0
MAX_POLL_INTERVAL_SECONDS = 30.0
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADJUSTMENTS = {"raw": "none", "qfq": "front"}
_VOLUME_UNITS = {"share"}
_AMOUNT_UNITS = {"cny"}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROTECTED_OUTPUT_ROOTS = (_REPO_ROOT,)


class QmtTransportCaptureError(RuntimeError):
    """A QMT v2 transport response cannot be used as shadow evidence."""


class QmtTransportTimeout(QmtTransportCaptureError):
    """The producer did not publish the exact requested v2 snapshot in time."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QmtTransportCaptureError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise QmtTransportCaptureError(f"non-finite JSON number: {value}")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise QmtTransportCaptureError("value is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _permitted_uses() -> list[str]:
    return list(PERMITTED_USES)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QmtTransportCaptureError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QmtTransportCaptureError(
            f"{field} must be a timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QmtTransportCaptureError(f"{field} must be a timezone-aware timestamp")
    return parsed.astimezone(timezone.utc)


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise QmtTransportCaptureError(f"{field} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise QmtTransportCaptureError(f"{field} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise QmtTransportCaptureError(f"{field} must be a canonical ISO date")
    return parsed


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QmtTransportCaptureError(f"{field} must be finite numeric data")
    number = float(value)
    if not math.isfinite(number):
        raise QmtTransportCaptureError(f"{field} must be finite numeric data")
    return number


def _symbols(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise QmtTransportCaptureError("symbols must be a sequence")
    symbols = [str(value).strip().upper() for value in values]
    if not 1 <= len(symbols) <= MAX_SYMBOLS or len(set(symbols)) != len(symbols):
        raise QmtTransportCaptureError(f"symbols must contain 1..{MAX_SYMBOLS} unique items")
    if any(not _SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise QmtTransportCaptureError("symbols must use 000000.SH/SZ/BJ format")
    return symbols


def build_qmt_transport_request(
    symbols: Sequence[str], *, count: int, adjustment: str,
    request_id: str | None = None,
) -> dict[str, object]:
    """Build the sole accepted canonical QMT v2 request contract."""
    if isinstance(count, bool) or not isinstance(count, int) \
            or not 1 <= count <= MAX_COUNT:
        raise QmtTransportCaptureError(f"count must be an integer in 1..{MAX_COUNT}")
    if adjustment not in _ADJUSTMENTS:
        raise QmtTransportCaptureError("adjustment must be raw or qfq")
    identifier = request_id or str(uuid.uuid4())
    try:
        uuid.UUID(identifier)
    except (AttributeError, ValueError, TypeError) as exc:
        raise QmtTransportCaptureError("request_id must be a UUID") from exc
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": identifier,
        "symbols": _symbols(symbols),
        "period": "1d",
        "count": count,
        "fields": list(FIELDS),
        "adjustment": adjustment,
        "qmt_parameter": _ADJUSTMENTS[adjustment],
        "fill_data": False,
    }


def request_sha256(request: object) -> str:
    """Return the SHA-256 of a complete canonical QMT v2 request."""
    _verify_request(request)
    return _sha256(request)


def _verify_request(request: object) -> dict[str, object]:
    expected_keys = {
        "schema_version", "request_id", "symbols", "period", "count", "fields",
        "adjustment", "qmt_parameter", "fill_data",
    }
    if not isinstance(request, dict) or set(request) != expected_keys:
        raise QmtTransportCaptureError("QMT v2 request schema is incomplete")
    if request.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise QmtTransportCaptureError("QMT v1 request is rejected")
    try:
        uuid.UUID(str(request.get("request_id")))
    except (AttributeError, ValueError, TypeError) as exc:
        raise QmtTransportCaptureError("request_id must be a UUID") from exc
    symbols = request.get("symbols")
    if not isinstance(symbols, list) or _symbols(symbols) != symbols:
        raise QmtTransportCaptureError("QMT request symbols are invalid")
    count = request.get("count")
    adjustment = request.get("adjustment")
    if isinstance(count, bool) or not isinstance(count, int) \
            or not 1 <= count <= MAX_COUNT \
            or request.get("period") != "1d" \
            or request.get("fields") != list(FIELDS) \
            or adjustment not in _ADJUSTMENTS \
            or request.get("qmt_parameter") != _ADJUSTMENTS[adjustment] \
            or request.get("fill_data") is not False:
        raise QmtTransportCaptureError("QMT request contract is invalid")
    return request


def _loopback_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.username or parsed.password \
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise QmtTransportCaptureError("QMT endpoint must be a bare loopback HTTP URL")
    try:
        host = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise QmtTransportCaptureError("QMT endpoint must use a loopback IP literal") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise QmtTransportCaptureError("QMT endpoint port is invalid") from exc
    if not host.is_loopback or port is None:
        raise QmtTransportCaptureError("QMT endpoint must use a loopback IP literal")
    return base_url.rstrip("/")


def _verify_ack(payload: object, request: dict[str, object], digest: str) -> None:
    expected_keys = {"schema_version", "ok", "request_id", "request_sha256"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise QmtTransportCaptureError("QMT acknowledgement schema is incomplete")
    if payload.get("schema_version") != ACK_SCHEMA_VERSION:
        raise QmtTransportCaptureError("QMT v1 acknowledgement is rejected")
    if payload.get("ok") is not True \
            or payload.get("request_id") != request["request_id"] \
            or payload.get("request_sha256") != digest:
        raise QmtTransportCaptureError("QMT acknowledgement binding is invalid")


def _is_foreign_snapshot(
    snapshot: object, request: dict[str, object], digest: str,
) -> bool:
    """Only a well-labelled different v2 request is a pollable wait state."""
    return isinstance(snapshot, dict) \
        and snapshot.get("schema_version") == SCHEMA_VERSION \
        and (snapshot.get("request_id") != request["request_id"]
             or snapshot.get("request_sha256") != digest)


def _validate_rows(
    symbol: str, rows: object, *, count: int, generated_at: datetime,
) -> list[dict[str, object]]:
    if not isinstance(rows, list) or not rows or len(rows) > count:
        raise QmtTransportCaptureError(f"{symbol} response exceeds available history")
    output: list[dict[str, object]] = []
    expected_keys = {"date", *FIELDS}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise QmtTransportCaptureError(f"{symbol} row schema is invalid")
        day = _date(row.get("date"), f"{symbol}.date")
        if day >= generated_at.astimezone(_SHANGHAI).date():
            raise QmtTransportCaptureError(
                f"{symbol} row is not a finalized prior-day bar"
            )
        values = {field: _number(row.get(field), f"{symbol}.{field}") for field in FIELDS}
        if any(values[field] <= 0 for field in ("open", "high", "low", "close")) \
                or values["volume"] < 0 or values["amount"] < 0 \
                or values["high"] < max(values["open"], values["close"]) \
                or values["low"] > min(values["open"], values["close"]) \
                or values["low"] > values["high"]:
            raise QmtTransportCaptureError(f"{symbol} OHLCV is invalid")
        output.append({"date": day.isoformat(), **values})
    dates = [str(row["date"]) for row in output]
    if dates != sorted(set(dates)):
        raise QmtTransportCaptureError(f"{symbol} dates must be unique and increasing")
    return output


def validate_qmt_transport_snapshot(
    snapshot: object, request: dict[str, object], *, observed_at: datetime | None = None,
    baseline_generated_at: str | None = None,
) -> dict[str, object]:
    """Fail closed unless a complete v2 snapshot exactly echoes ``request``."""
    request = _verify_request(request)
    digest = request_sha256(request)
    expected_keys = {
        "schema_version", "request_id", "request_sha256", "request",
        "producer_instance", "qmt_build", "xtquant_build", "generated_at",
        "available_at", "volume_unit", "amount_unit", "market",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected_keys:
        raise QmtTransportCaptureError("QMT snapshot schema is incomplete")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise QmtTransportCaptureError("QMT v1 snapshot is rejected")
    if snapshot.get("request_id") != request["request_id"] \
            or snapshot.get("request_sha256") != digest \
            or snapshot.get("request") != request:
        raise QmtTransportCaptureError("QMT snapshot request binding is invalid")
    if any(not isinstance(snapshot.get(field), str) or not snapshot[field]
           for field in ("producer_instance", "qmt_build", "xtquant_build")):
        raise QmtTransportCaptureError("QMT producer identity is incomplete")
    generated_at = _timestamp(snapshot.get("generated_at"), "generated_at")
    available_at = _timestamp(snapshot.get("available_at"), "available_at")
    current = observed_at or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise QmtTransportCaptureError("observed_at must include timezone")
    current = current.astimezone(timezone.utc)
    if generated_at > current or available_at > current:
        raise QmtTransportCaptureError("QMT snapshot timestamp is in the future")
    if available_at < generated_at:
        raise QmtTransportCaptureError("QMT availability precedes generation")
    if baseline_generated_at is not None \
            and generated_at <= _timestamp(baseline_generated_at, "baseline_generated_at"):
        raise QmtTransportTimeout("QMT snapshot is replayed or has not advanced")
    if snapshot.get("volume_unit") not in _VOLUME_UNITS \
            or snapshot.get("amount_unit") not in _AMOUNT_UNITS:
        raise QmtTransportCaptureError("QMT volume or amount unit is ambiguous")
    market = snapshot.get("market")
    if not isinstance(market, dict) or set(market) != set(request["symbols"]):
        raise QmtTransportCaptureError("QMT snapshot symbols are partial or foreign")

    symbols: list[dict[str, object]] = []
    for symbol in request["symbols"]:
        item = market[symbol]
        required = {"coverage", "finality", "errors", "rows", "rows_sha256"}
        if not isinstance(item, dict) or set(item) != required:
            raise QmtTransportCaptureError(f"{symbol} snapshot schema is incomplete")
        if item.get("errors") != []:
            raise QmtTransportCaptureError(f"{symbol} QMT export reports errors")
        rows = _validate_rows(
            symbol, item.get("rows"), count=request["count"], generated_at=generated_at
        )
        if item.get("rows_sha256") != _sha256(rows):
            raise QmtTransportCaptureError(f"{symbol} rows hash is invalid")
        dates = [str(row["date"]) for row in rows]
        coverage = {
            "status": "complete_available_history",
            "requested_count": request["count"],
            "returned_count": len(rows),
            "start": dates[0],
            "end": dates[-1],
        }
        finality = {
            "status": "source_marked_final",
            "verification": "unverified",
            "watermark": dates[-1],
        }
        if item.get("coverage") != coverage or item.get("finality") != finality:
            raise QmtTransportCaptureError(f"{symbol} coverage or finality is invalid")
        symbols.append({
            "symbol": symbol,
            "coverage": coverage,
            "finality": finality,
            "errors": [],
            "rows": rows,
            "rows_sha256": item["rows_sha256"],
        })
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "authority_grade": "shadow",
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "permitted_uses": _permitted_uses(),
        "source_id": "qmt_loopback_transport_v2",
        "source_authentication": "shared_token_unverified",
        "finality": "source_marked_final_unverified",
        "request": request,
        "request_id": request["request_id"],
        "request_sha256": digest,
        "producer_instance": snapshot["producer_instance"],
        "qmt_build": snapshot["qmt_build"],
        "xtquant_build": snapshot["xtquant_build"],
        "generated_at": snapshot["generated_at"],
        "available_at": snapshot["available_at"],
        "volume_unit": snapshot["volume_unit"],
        "amount_unit": snapshot["amount_unit"],
        "symbols": symbols,
    }
    return {**unsigned, "snapshot_sha256": _sha256(unsigned)}


class QmtTransportCaptureClient:
    """POST an exact request and poll loopback HTTP for its v2 snapshot."""

    def __init__(
        self, *, token: str | None = None, base_url: str = "http://127.0.0.1:8000",
        request_timeout: float = 3.0, max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        resolved = token if token is not None else os.environ.get("QMT_TRANSPORT_TOKEN")
        if not isinstance(resolved, str) or not resolved:
            raise QmtTransportCaptureError("QMT_TRANSPORT_TOKEN is required")
        if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) \
                or not math.isfinite(float(request_timeout)) \
                or not 0 < float(request_timeout) <= MAX_REQUEST_TIMEOUT_SECONDS \
                or isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) \
                or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise QmtTransportCaptureError("transport limits are invalid")
        self._base_url = _loopback_base_url(base_url)
        self._token = resolved
        self._request_timeout = float(request_timeout)
        self._max_response_bytes = int(max_response_bytes)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )

    def _json(self, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        if (path, method) not in {("/latest", "GET"), ("/request", "POST")}:
            raise QmtTransportCaptureError("unsupported QMT transport operation")
        raw_body = _canonical(body) if body is not None else None
        request = urllib.request.Request(
            self._base_url + path, data=raw_body, method=method,
            headers={"Accept": "application/json", "X-Token": self._token},
        )
        if raw_body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self._request_timeout) as response:
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise QmtTransportCaptureError(f"QMT HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise QmtTransportCaptureError("QMT loopback channel is unavailable") from exc
        if len(raw) > self._max_response_bytes:
            raise QmtTransportCaptureError("QMT response exceeds the memory limit")
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QmtTransportCaptureError("QMT response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise QmtTransportCaptureError("QMT response root must be an object")
        return payload

    def capture(
        self, symbols: Sequence[str], *, count: int = 250, adjustment: str = "raw",
        wait_timeout: float = 90.0, poll_interval: float = 1.0,
    ) -> dict[str, object]:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(float(value))
               for value in (wait_timeout, poll_interval)) \
                or not 0 < float(wait_timeout) <= MAX_WAIT_TIMEOUT_SECONDS \
                or not 0 < float(poll_interval) <= MAX_POLL_INTERVAL_SECONDS:
            raise QmtTransportCaptureError("poll limits are invalid")
        baseline = self._json("/latest")
        if baseline.get("schema_version") != SCHEMA_VERSION:
            raise QmtTransportCaptureError("QMT v1 snapshot is rejected")
        baseline_generated_at = baseline.get("generated_at")
        _timestamp(baseline_generated_at, "baseline_generated_at")
        request = build_qmt_transport_request(
            symbols, count=count, adjustment=adjustment,
        )
        digest = request_sha256(request)
        _verify_ack(self._json("/request", method="POST", body=request), request, digest)
        deadline = time.monotonic() + wait_timeout
        last_reason = "no snapshot received"
        while time.monotonic() < deadline:
            snapshot = self._json("/latest")
            try:
                return validate_qmt_transport_snapshot(
                    snapshot, request,
                    baseline_generated_at=baseline_generated_at,
                )
            except QmtTransportTimeout as exc:
                last_reason = str(exc)
                time.sleep(poll_interval)
            except QmtTransportCaptureError:
                if _is_foreign_snapshot(snapshot, request, digest):
                    last_reason = "QMT snapshot belongs to another request"
                    time.sleep(poll_interval)
                    continue
                raise
        raise QmtTransportTimeout(f"QMT did not publish the requested snapshot: {last_reason}")


def _verify_capture(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or "snapshot_sha256" not in payload:
        raise QmtTransportCaptureError("QMT capture schema is incomplete")
    supplied = payload.get("snapshot_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    if not isinstance(supplied, str) or not _SHA256.fullmatch(supplied) \
            or supplied != _sha256(unsigned):
        raise QmtTransportCaptureError("QMT capture hash is invalid")
    request = unsigned.get("request")
    if not isinstance(request, dict):
        raise QmtTransportCaptureError("QMT capture request is missing")
    if unsigned.get("request_id") != request.get("request_id"):
        raise QmtTransportCaptureError("QMT capture request binding is invalid")
    verified = validate_qmt_transport_snapshot(
        {
            "schema_version": unsigned.get("schema_version"),
            "request_id": request.get("request_id"),
            "request_sha256": unsigned.get("request_sha256"),
            "request": request,
            "producer_instance": unsigned.get("producer_instance"),
            "qmt_build": unsigned.get("qmt_build"),
            "xtquant_build": unsigned.get("xtquant_build"),
            "generated_at": unsigned.get("generated_at"),
            "available_at": unsigned.get("available_at"),
            "volume_unit": unsigned.get("volume_unit"),
            "amount_unit": unsigned.get("amount_unit"),
            "market": {
                item["symbol"]: {
                    key: item[key] for key in (
                        "coverage", "finality", "errors", "rows", "rows_sha256"
                    )
                }
                for item in unsigned.get("symbols", []) if isinstance(item, dict)
            },
        },
        request,
    )
    if {key: value for key, value in verified.items() if key != "snapshot_sha256"} != unsigned:
        raise QmtTransportCaptureError("QMT capture content is invalid")
    return payload


def _nofollow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not isinstance(flag, int) or not flag:
        raise QmtTransportCaptureError("O_NOFOLLOW is required for QMT artifacts")
    return flag


def _safe_output_directory(output_root: str | Path, error_type):
    candidate = Path(output_root).expanduser()
    try:
        candidate_status = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise error_type("QMT output root must already exist") from exc
    if not stat.S_ISDIR(candidate_status.st_mode) or not resolved.is_dir() or any(
        resolved == protected or protected in resolved.parents
        for protected in _PROTECTED_OUTPUT_ROOTS
    ):
        raise error_type("QMT output root is protected or not a directory")
    if resolved == Path("/") or not resolved.is_absolute():
        raise error_type("QMT output root is invalid")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag())
    try:
        for component in resolved.parts[1:]:
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise error_type("QMT output root is not a directory")
        return descriptor, resolved, (int(status.st_dev), int(status.st_ino))
    except OSError as exc:
        os.close(descriptor)
        raise error_type("QMT output root cannot be opened safely") from exc
    except Exception:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, raw: bytes, error_type) -> None:
    offset = 0
    while offset < len(raw):
        try:
            written = os.write(descriptor, raw[offset:])
        except OSError as exc:
            raise error_type("QMT artifact cannot be written") from exc
        if written <= 0:
            raise error_type("QMT artifact cannot be written")
        offset += written


def _read_exact(descriptor: int, limit: int, error_type) -> bytes:
    chunks = []
    remaining = limit + 1
    while remaining:
        try:
            chunk = os.read(descriptor, remaining)
        except OSError as exc:
            raise error_type("QMT artifact cannot be read") from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        remaining -= len(chunk)
    raise error_type("QMT artifact exceeds its expected size")


def _entry_matches(directory_fd: int, name: str, identity: tuple[int, int, int]) -> bool:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and (
        int(status.st_dev), int(status.st_ino), int(status.st_nlink)
    ) == identity


def _read_regular_file(path: str | Path, limit: int, error_type) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
    except OSError as exc:
        raise error_type("QMT artifact is unreadable") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 \
                or status.st_size > limit:
            raise error_type("QMT artifact is not a bounded single-link regular file")
        identity = (
            int(status.st_dev), int(status.st_ino), int(status.st_nlink),
            int(status.st_size), int(status.st_mtime_ns), int(status.st_ctime_ns),
        )
        raw = _read_exact(descriptor, int(status.st_size), error_type)
        after = os.fstat(descriptor)
        if (
            int(after.st_dev), int(after.st_ino), int(after.st_nlink),
            int(after.st_size), int(after.st_mtime_ns), int(after.st_ctime_ns),
        ) != identity:
            raise error_type("QMT artifact identity changed during read")
        return raw
    finally:
        os.close(descriptor)


def _write_content_addressed(
    output_root: str | Path, digest: str, raw: bytes, error_type,
) -> Path:
    """Durably create or byte-verify one artifact below an existing safe root."""
    if not _SHA256.fullmatch(digest):
        raise error_type("QMT artifact digest is invalid")
    directory_fd, root, identity = _safe_output_directory(output_root, error_type)
    name = f"{digest}.json"
    descriptor = -1
    created = False
    created_identity: tuple[int, int, int] | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created = True
        except FileExistsError:
            descriptor = os.open(name, os.O_RDONLY | _nofollow_flag(), dir_fd=directory_fd)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 \
                    or _read_exact(descriptor, len(raw), error_type) != raw:
                raise error_type("QMT artifact conflicts with existing bytes")
            entry_identity = (int(status.st_dev), int(status.st_ino), int(status.st_nlink))
            if not _entry_matches(directory_fd, name, entry_identity):
                raise error_type("QMT artifact entry identity changed")
            if (int(os.fstat(directory_fd).st_dev), int(os.fstat(directory_fd).st_ino)) != identity:
                raise error_type("QMT output parent identity changed")
            return root / name
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise error_type("QMT artifact target is not a single-link regular file")
        created_identity = (int(status.st_dev), int(status.st_ino), int(status.st_nlink))
        if not _entry_matches(directory_fd, name, created_identity):
            raise error_type("QMT artifact entry identity changed")
        _write_all(descriptor, raw, error_type)
        if not _entry_matches(directory_fd, name, created_identity):
            raise error_type("QMT artifact entry identity changed")
        os.fsync(descriptor)
        if not _entry_matches(directory_fd, name, created_identity):
            raise error_type("QMT artifact entry identity changed")
        if (int(os.fstat(directory_fd).st_dev), int(os.fstat(directory_fd).st_ino)) != identity:
            raise error_type("QMT output parent identity changed")
        os.fsync(directory_fd)
        if not _entry_matches(directory_fd, name, created_identity):
            raise error_type("QMT artifact entry identity changed")
        return root / name
    except OSError as exc:
        if created and created_identity is not None \
                and _entry_matches(directory_fd, name, created_identity):
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        raise error_type("QMT artifact cannot be written durably") from exc
    except Exception:
        if created and created_identity is not None \
                and _entry_matches(directory_fd, name, created_identity):
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def write_qmt_transport_snapshot(output_root: str | Path, capture: dict[str, object]) -> Path:
    """Write explicit evidence, never a cache, database, or application state."""
    verified = _verify_capture(capture)
    return _write_content_addressed(
        output_root, verified["snapshot_sha256"], _canonical(verified),
        QmtTransportCaptureError,
    )


def load_qmt_transport_snapshot(path: str | Path) -> dict[str, object]:
    try:
        raw = _read_regular_file(path, MAX_RESPONSE_BYTES, QmtTransportCaptureError)
        payload = json.loads(
            raw.decode("ascii"), object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtTransportCaptureError("QMT capture artifact is unreadable") from exc
    if raw != _canonical(payload):
        raise QmtTransportCaptureError("QMT capture artifact bytes are not canonical")
    return _verify_capture(payload)


__all__ = [
    "ACK_SCHEMA_VERSION", "FIELDS", "MAX_RESPONSE_BYTES", "QmtTransportCaptureClient",
    "QmtTransportCaptureError", "QmtTransportTimeout", "SCHEMA_VERSION",
    "build_qmt_transport_request", "load_qmt_transport_snapshot", "request_sha256",
    "validate_qmt_transport_snapshot", "write_qmt_transport_snapshot",
]
