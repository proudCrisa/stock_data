"""GET-only sealed replay of an existing QmtExport/2.0 pool snapshot."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "qmt-pool-sealed-replay/1"
SERVICE = "QmtExport/2.0"
PERMITTED_USES = ("offline_replay",)
FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAX_POOL_SYMBOLS = 512
MAX_REQUEST_SYMBOLS = 20
MAX_ROWS_PER_SYMBOL = 1500
MAX_TOTAL_ROWS = MAX_REQUEST_SYMBOLS * MAX_ROWS_PER_SYMBOL
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 15.0
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_STATUS_KEYS = {
    "account_sections", "auth_required", "errors", "export_dir", "generated",
    "latest_age_sec", "latest_exists", "latest_mtime", "python", "server",
    "symbols", "uptime_sec",
}
_LATEST_KEYS = {"account", "account_id", "errors", "generated", "market", "request"}


class QmtPoolReplayError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QmtPoolReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise QmtPoolReplayError(f"non-finite JSON number: {value}")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise QmtPoolReplayError("value is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _local_generated(value: object) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise QmtPoolReplayError("generated must be a Shanghai local timestamp")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=_SHANGHAI)
            return parsed.isoformat(), parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    raise QmtPoolReplayError("generated must be a Shanghai local timestamp")


def _symbols(value: object, *, maximum: int, field: str, ordered: bool) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise QmtPoolReplayError(f"{field} must contain 1..{maximum} symbols")
    if any(not isinstance(item, str) or not _SYMBOL.fullmatch(item) for item in value):
        raise QmtPoolReplayError(f"{field} contains an invalid symbol")
    if len(set(value)) != len(value) or (ordered and value != sorted(value)):
        raise QmtPoolReplayError(f"{field} symbols are not deterministic")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QmtPoolReplayError(f"{field} must be finite numeric data")
    result = float(value)
    if not math.isfinite(result):
        raise QmtPoolReplayError(f"{field} must be finite numeric data")
    return result


def _day(value: object, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise QmtPoolReplayError(f"{field} must use YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise QmtPoolReplayError(f"{field} must use YYYYMMDD") from exc


def normalize_qmt_pool_wire(status: object, latest: object, *, observed_at: datetime | None = None) -> dict:
    """Validate actual QmtExport/2.0 GET bodies and remove non-replay sections."""
    if not isinstance(status, dict) or set(status) != _STATUS_KEYS \
            or status.get("server") != SERVICE or status.get("errors") != [] \
            or status.get("latest_exists") is not True \
            or status.get("auth_required") is not True:
        raise QmtPoolReplayError("QMT pool status contract is invalid")
    if not isinstance(latest, dict) or set(latest) != _LATEST_KEYS \
            or latest.get("errors") != [] or not isinstance(latest.get("market"), dict):
        raise QmtPoolReplayError("QMT pool latest contract is invalid")
    if status.get("generated") != latest.get("generated"):
        raise QmtPoolReplayError("QMT pool generated watermarks differ")
    normalized_generated, generated = _local_generated(status.get("generated"))
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise QmtPoolReplayError("observed_at must include timezone")
    observed = observed.astimezone(timezone.utc)
    if generated > observed:
        raise QmtPoolReplayError("QMT pool generated timestamp is in the future")
    pool_symbols = _symbols(status.get("symbols"), maximum=MAX_POOL_SYMBOLS, field="status", ordered=False)
    request = latest.get("request")
    if not isinstance(request, dict) or set(request) != {"count", "fields", "period", "symbols"} \
            or isinstance(request.get("count"), bool) or not isinstance(request.get("count"), int) \
            or not 1 <= request["count"] <= MAX_ROWS_PER_SYMBOL \
            or request.get("period") != "1d" or request.get("fields") != list(FIELDS):
        raise QmtPoolReplayError("QMT pool source request contract is invalid")
    request_symbols = _symbols(request.get("symbols"), maximum=MAX_POOL_SYMBOLS, field="source request", ordered=False)
    market = latest["market"]
    if sorted(pool_symbols) != sorted(request_symbols) or set(market) != set(request_symbols):
        raise QmtPoolReplayError("QMT pool membership and market identities differ")
    canonical_symbols = sorted(pool_symbols)
    return {
        "service": SERVICE,
        "auth_required": status["auth_required"],
        "source_generated": status["generated"],
        "normalized_generated_at": normalized_generated,
        "available_at": observed.isoformat(timespec="seconds"),
        "symbols": canonical_symbols,
        "membership_sha256": _sha256(canonical_symbols),
        "source_request": {
            "count": request["count"], "fields": list(FIELDS), "period": "1d",
            "symbols": canonical_symbols,
        },
        "market": market,
    }


def _source_rows(symbol: str, record: object, *, count: int, capture_day: date) -> tuple[list[dict], int]:
    if not isinstance(record, dict) or set(record) != {"index", "columns"}:
        raise QmtPoolReplayError(f"{symbol} market schema is invalid")
    index = record.get("index")
    columns = record.get("columns")
    if not isinstance(index, list) or not index or len(index) > count \
            or not isinstance(columns, dict) or set(columns) != set(FIELDS) \
            or any(not isinstance(columns[field], list) or len(columns[field]) != len(index) for field in FIELDS):
        raise QmtPoolReplayError(f"{symbol} market columns are invalid")
    days = [_day(value, f"{symbol}.index") for value in index]
    if days != sorted(set(days)) or days[-1] > capture_day:
        raise QmtPoolReplayError(f"{symbol} source dates are invalid")
    normalized = []
    for offset, day in enumerate(days):
        values = {field: _number(columns[field][offset], f"{symbol}.{field}") for field in FIELDS}
        if any(values[field] <= 0 for field in ("open", "high", "low", "close")) \
                or values["volume"] < 0 or values["amount"] < 0 \
                or values["high"] < max(values["open"], values["close"]) \
                or values["low"] > min(values["open"], values["close"]) \
                or values["low"] > values["high"]:
            raise QmtPoolReplayError(f"{symbol} OHLCV is invalid")
        normalized.append({"date": day.isoformat(), **values})
    return [row for row in normalized if row["date"] < capture_day.isoformat()], len(normalized)


def seal_qmt_pool_replay(normalized: object, symbols: Sequence[str]) -> dict:
    """Seal explicitly selected symbols from a normalized QmtExport pool wire receipt."""
    if not isinstance(normalized, dict) or set(normalized) != {
        "service", "auth_required", "source_generated", "normalized_generated_at", "available_at",
        "symbols", "membership_sha256", "source_request", "market",
    } or normalized.get("service") != SERVICE:
        raise QmtPoolReplayError("normalized QMT pool receipt is invalid")
    pool_symbols = _symbols(normalized.get("symbols"), maximum=MAX_POOL_SYMBOLS, field="pool", ordered=True)
    if normalized.get("membership_sha256") != _sha256(pool_symbols):
        raise QmtPoolReplayError("QMT pool membership hash is invalid")
    source_request = normalized.get("source_request")
    if not isinstance(source_request, dict) or set(source_request) != {"count", "fields", "period", "symbols"} \
            or source_request.get("symbols") != pool_symbols \
            or source_request.get("fields") != list(FIELDS) or source_request.get("period") != "1d":
        raise QmtPoolReplayError("QMT pool source request is invalid")
    normalized_generated, generated = _local_generated(normalized.get("source_generated"))
    if normalized.get("normalized_generated_at") != normalized_generated:
        raise QmtPoolReplayError("QMT pool generated timestamp is invalid")
    try:
        available = datetime.fromisoformat(str(normalized.get("available_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise QmtPoolReplayError("QMT pool available_at is invalid") from exc
    if available.tzinfo is None or available.astimezone(timezone.utc) < generated:
        raise QmtPoolReplayError("QMT pool available_at precedes generation")
    selected = sorted(symbols)
    if not 1 <= len(selected) <= MAX_REQUEST_SYMBOLS or len(set(selected)) != len(selected) \
            or any(not isinstance(item, str) or item not in pool_symbols for item in selected):
        raise QmtPoolReplayError("selection must contain 1..20 existing pool symbols")
    market = normalized.get("market")
    if not isinstance(market, dict) or set(market) != set(pool_symbols):
        raise QmtPoolReplayError("normalized market identity is invalid")
    capture_day = generated.astimezone(_SHANGHAI).date()
    products = []
    total = 0
    for symbol in selected:
        rows, source_count = _source_rows(symbol, market[symbol], count=source_request["count"], capture_day=capture_day)
        if not rows:
            raise QmtPoolReplayError(f"{symbol} has no prior-day rows")
        dropped = source_count - len(rows)
        if dropped not in {0, 1}:
            raise QmtPoolReplayError(f"{symbol} capture-day count is invalid")
        total += len(rows)
        if total > MAX_TOTAL_ROWS:
            raise QmtPoolReplayError("QMT pool total row cap exceeded")
        content = {
            "symbol": symbol, "period": "1d",
            "price_identity": {"adjustment": "unbound", "volume_unit": "unbound", "amount_unit": "unbound", "finality": "unverified"},
            "source_row_count": source_count, "accepted_row_count": len(rows),
            "dropped_capture_day_count": dropped,
            "event_time_range": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "rows": rows, "rows_sha256": _sha256(rows),
        }
        products.append({**content, "content_sha256": _sha256(content)})
    receipt = {key: normalized[key] for key in (
        "service", "auth_required", "source_generated", "normalized_generated_at", "available_at",
        "symbols", "membership_sha256", "source_request",
    )}
    receipt["symbol_count"] = len(pool_symbols)
    unsigned = {
        "schema_version": SCHEMA_VERSION, "authority_grade": "shadow", "decision_eligible": False,
        "decision_authority": False, "actions": [], "permitted_uses": list(PERMITTED_USES),
        "source_authentication": "shared_token_unverified", "pool_receipt": receipt,
        "selection": {"symbols": selected, "period": "1d", "max_rows_per_symbol": MAX_ROWS_PER_SYMBOL},
        "generated_at": normalized["normalized_generated_at"], "available_at": normalized["available_at"],
        "products": products,
    }
    artifact = {**unsigned, "replay_sha256": _sha256(unsigned)}
    if len(_canonical(artifact)) > MAX_ARTIFACT_BYTES:
        raise QmtPoolReplayError("QMT pool replay artifact byte cap exceeded")
    return artifact


def verify_qmt_pool_replay(payload: object) -> dict:
    required = {
        "schema_version", "authority_grade", "decision_eligible", "decision_authority", "actions",
        "permitted_uses", "source_authentication", "pool_receipt", "selection", "generated_at",
        "available_at", "products", "replay_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise QmtPoolReplayError("QMT pool replay schema is incomplete")
    unsigned = {key: value for key, value in payload.items() if key != "replay_sha256"}
    if payload.get("replay_sha256") != _sha256(unsigned) or len(_canonical(payload)) > MAX_ARTIFACT_BYTES:
        raise QmtPoolReplayError("QMT pool replay seal is invalid")
    if unsigned.get("schema_version") != SCHEMA_VERSION or unsigned.get("authority_grade") != "shadow" \
            or unsigned.get("decision_eligible") is not False or unsigned.get("decision_authority") is not False \
            or unsigned.get("actions") != [] or unsigned.get("permitted_uses") != list(PERMITTED_USES) \
            or unsigned.get("source_authentication") != "shared_token_unverified":
        raise QmtPoolReplayError("QMT pool replay authority is invalid")
    receipt = unsigned.get("pool_receipt")
    if not isinstance(receipt, dict) or set(receipt) != {
        "service", "auth_required", "source_generated", "normalized_generated_at", "available_at",
        "symbols", "membership_sha256", "source_request", "symbol_count",
    } or receipt.get("symbol_count") != len(receipt.get("symbols", [])):
        raise QmtPoolReplayError("QMT pool receipt is invalid")
    pool_symbols = _symbols(receipt.get("symbols"), maximum=MAX_POOL_SYMBOLS, field="pool", ordered=True)
    if receipt.get("service") != SERVICE or receipt.get("auth_required") is not True \
            or receipt.get("membership_sha256") != _sha256(pool_symbols):
        raise QmtPoolReplayError("QMT pool receipt is invalid")
    normalized_generated, generated = _local_generated(receipt.get("source_generated"))
    if receipt.get("normalized_generated_at") != normalized_generated:
        raise QmtPoolReplayError("QMT pool generated timestamp is invalid")
    now = datetime.now(timezone.utc)
    if generated > now:
        raise QmtPoolReplayError("QMT pool generated timestamp is in the future")
    try:
        available = datetime.fromisoformat(str(receipt.get("available_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise QmtPoolReplayError("QMT pool available_at is invalid") from exc
    if available.tzinfo is None or available.astimezone(timezone.utc) < generated:
        raise QmtPoolReplayError("QMT pool available_at precedes generation")
    if available.astimezone(timezone.utc) > now:
        raise QmtPoolReplayError("QMT pool available_at is in the future")
    request = receipt.get("source_request")
    if not isinstance(request, dict) or set(request) != {"count", "fields", "period", "symbols"} \
            or isinstance(request.get("count"), bool) or not isinstance(request.get("count"), int) \
            or not 1 <= request["count"] <= MAX_ROWS_PER_SYMBOL or request.get("fields") != list(FIELDS) \
            or request.get("period") != "1d" or request.get("symbols") != pool_symbols:
        raise QmtPoolReplayError("QMT pool source request is invalid")
    selection = unsigned.get("selection")
    products = unsigned.get("products")
    if not isinstance(selection, dict) or set(selection) != {"symbols", "period", "max_rows_per_symbol"} \
            or selection.get("period") != "1d" or selection.get("max_rows_per_symbol") != MAX_ROWS_PER_SYMBOL \
            or not isinstance(products, list):
        raise QmtPoolReplayError("QMT pool replay content is invalid")
    selected = selection.get("symbols")
    if not isinstance(selected, list) or selected != sorted(selected) or len(selected) != len(products) \
            or not 1 <= len(selected) <= MAX_REQUEST_SYMBOLS or len(set(selected)) != len(selected) \
            or any(item not in pool_symbols for item in selected):
        raise QmtPoolReplayError("QMT pool replay content is invalid")
    if unsigned.get("generated_at") != normalized_generated or unsigned.get("available_at") != receipt.get("available_at"):
        raise QmtPoolReplayError("QMT pool timestamps are invalid")
    capture_day = generated.astimezone(_SHANGHAI).date()
    total = 0
    for expected_symbol, item in zip(selected, products):
        expected = {
            "symbol", "period", "price_identity", "source_row_count", "accepted_row_count",
            "dropped_capture_day_count", "event_time_range", "rows", "rows_sha256", "content_sha256",
        }
        if not isinstance(item, dict) or set(item) != expected or item.get("symbol") != expected_symbol \
                or item.get("period") != "1d" or item.get("price_identity") != {
                    "adjustment": "unbound", "volume_unit": "unbound", "amount_unit": "unbound", "finality": "unverified",
                } or not isinstance(item.get("rows"), list) or not item["rows"]:
            raise QmtPoolReplayError("QMT pool product is invalid")
        if any(isinstance(item.get(key), bool) or not isinstance(item.get(key), int) for key in (
            "source_row_count", "accepted_row_count", "dropped_capture_day_count"
        )) or item["accepted_row_count"] != len(item["rows"]) \
                or item["source_row_count"] != item["accepted_row_count"] + item["dropped_capture_day_count"] \
                or item["source_row_count"] > request["count"] \
                or item["dropped_capture_day_count"] not in {0, 1}:
            raise QmtPoolReplayError("QMT pool product counts are invalid")
        days: list[date] = []
        for row in item["rows"]:
            if not isinstance(row, dict) or set(row) != {"date", *FIELDS}:
                raise QmtPoolReplayError("QMT pool row is invalid")
            try:
                row_day = date.fromisoformat(row["date"])
            except (TypeError, ValueError) as exc:
                raise QmtPoolReplayError("QMT pool row date is invalid") from exc
            if row_day >= capture_day:
                raise QmtPoolReplayError("QMT pool replay contains capture-day data")
            values = {field: _number(row[field], f"{expected_symbol}.{field}") for field in FIELDS}
            if any(values[field] <= 0 for field in ("open", "high", "low", "close")) \
                    or values["volume"] < 0 or values["amount"] < 0 \
                    or values["high"] < max(values["open"], values["close"]) \
                    or values["low"] > min(values["open"], values["close"]) \
                    or values["low"] > values["high"]:
                raise QmtPoolReplayError("QMT pool row OHLCV is invalid")
            days.append(row_day)
        if days != sorted(set(days)) or item.get("event_time_range") != {
            "start": days[0].isoformat(), "end": days[-1].isoformat(),
        } or item.get("rows_sha256") != _sha256(item["rows"]):
            raise QmtPoolReplayError("QMT pool product rows are invalid")
        content = {key: value for key, value in item.items() if key != "content_sha256"}
        if item.get("content_sha256") != _sha256(content):
            raise QmtPoolReplayError("QMT pool product seal is invalid")
        total += len(item["rows"])
    if total > MAX_TOTAL_ROWS:
        raise QmtPoolReplayError("QMT pool total row cap exceeded")
    return payload


def _nofollow() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise QmtPoolReplayError("O_NOFOLLOW is required")
    return flag


def _safe_output_root(root: str | Path) -> tuple[int, Path, tuple[int, int]]:
    candidate = Path(root).expanduser()
    try:
        initial = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise QmtPoolReplayError("output root must already exist") from exc
    initial_identity = (int(initial.st_dev), int(initial.st_ino))
    if not stat.S_ISDIR(initial.st_mode) or not resolved.is_dir() or resolved == Path("/") \
            or resolved == _REPO_ROOT or _REPO_ROOT in resolved.parents:
        raise QmtPoolReplayError("output root is protected or not a directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _nofollow())
    try:
        for component in resolved.parts[1:]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | _nofollow(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        status = os.fstat(descriptor)
        if (int(status.st_dev), int(status.st_ino)) != initial_identity:
            raise QmtPoolReplayError("output root identity changed")
        return descriptor, resolved, (int(status.st_dev), int(status.st_ino))
    except Exception:
        os.close(descriptor)
        raise


def _entry_matches(directory_fd: int, name: str, identity: tuple[int, int, int]) -> bool:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and (int(status.st_dev), int(status.st_ino), int(status.st_nlink)) == identity


def _read_regular_file(directory_fd: int, name: str, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | _nofollow(), dir_fd=directory_fd)
        before = os.fstat(descriptor)
        identity = (int(before.st_dev), int(before.st_ino), int(before.st_nlink), int(before.st_size), int(before.st_mtime_ns), int(before.st_ctime_ns))
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise QmtPoolReplayError("existing replay artifact is unsafe")
        if not _entry_matches(directory_fd, name, identity[:3]):
            raise QmtPoolReplayError("existing replay artifact identity changed")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        after_identity = (int(after.st_dev), int(after.st_ino), int(after.st_nlink), int(after.st_size), int(after.st_mtime_ns), int(after.st_ctime_ns))
        if identity != after_identity or not _entry_matches(directory_fd, name, after_identity[:3]) \
                or len(b"".join(chunks)) > maximum:
            raise QmtPoolReplayError("existing replay artifact changed during read")
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_qmt_pool_replay(output_root: str | Path, artifact: dict) -> Path:
    artifact = verify_qmt_pool_replay(artifact)
    raw = _canonical(artifact)
    root_fd, root, parent_identity = _safe_output_root(output_root)
    name = f"{artifact['replay_sha256']}.json"
    descriptor = -1
    identity = None
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow(), 0o600, dir_fd=root_fd)
        status = os.fstat(descriptor)
        identity = (int(status.st_dev), int(status.st_ino), int(status.st_nlink))
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or not _entry_matches(root_fd, name, identity):
            raise QmtPoolReplayError("replay target identity is invalid")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise QmtPoolReplayError("replay artifact cannot be written")
            offset += written
        os.fsync(descriptor)
        if not _entry_matches(root_fd, name, identity) or (int(os.fstat(root_fd).st_dev), int(os.fstat(root_fd).st_ino)) != parent_identity:
            raise QmtPoolReplayError("replay artifact identity changed")
        os.fsync(root_fd)
        if not _entry_matches(root_fd, name, identity):
            raise QmtPoolReplayError("replay artifact identity changed")
        return root / name
    except FileExistsError:
        if _read_regular_file(root_fd, name, len(raw)) != raw:
            raise QmtPoolReplayError("existing replay artifact conflicts with replay seal")
        return root / name
    except Exception:
        if identity is not None and _entry_matches(root_fd, name, identity):
            try:
                os.unlink(name, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)


class QmtPoolReplayClient:
    def __init__(self, *, token: str | None = None, base_url: str = "http://127.0.0.1:8000"):
        token = token if token is not None else os.environ.get("QMT_POOL_REPLAY_TOKEN")
        if not isinstance(token, str) or not token:
            raise QmtPoolReplayError("QMT_POOL_REPLAY_TOKEN is required")
        parsed = urllib.parse.urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            port = parsed.port
        except ValueError:
            loopback = False
            port = None
        if parsed.scheme != "http" or not loopback or port is None or parsed.username or parsed.password \
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise QmtPoolReplayError("endpoint must be a bare loopback HTTP URL")
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _get(self, path: str) -> dict:
        if path not in {"/", "/latest"}:
            raise QmtPoolReplayError("unsupported pool operation")
        request = urllib.request.Request(self._base + path, headers={"Accept": "application/json", "X-Token": self._token})
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise QmtPoolReplayError(f"QMT HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise QmtPoolReplayError("QMT pool channel is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QmtPoolReplayError("QMT pool response exceeds memory cap")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_keys, parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QmtPoolReplayError("QMT pool response is not JSON") from exc
        if not isinstance(payload, dict):
            raise QmtPoolReplayError("QMT pool response root is invalid")
        return payload

    def capture(self, symbols: Sequence[str]) -> dict:
        status = self._get("/")
        latest = self._get("/latest")
        return seal_qmt_pool_replay(normalize_qmt_pool_wire(status, latest), symbols)


__all__ = [
    "FIELDS", "HTTP_TIMEOUT_SECONDS", "MAX_ARTIFACT_BYTES", "MAX_RESPONSE_BYTES", "QmtPoolReplayClient", "QmtPoolReplayError",
    "SCHEMA_VERSION", "normalize_qmt_pool_wire", "seal_qmt_pool_replay", "verify_qmt_pool_replay", "write_qmt_pool_replay",
]
