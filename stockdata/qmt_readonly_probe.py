"""Export a sealed, non-authoritative observation of local QMT daily bars."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
from datetime import datetime, timedelta, timezone
from numbers import Real
from pathlib import Path


SCHEMA_VERSION = "stockdata-qmt-readonly-probe/1"
PERMITTED_USES = ["environment_probe", "offline_validation"]
MAX_CODES = 3
MAX_LOOKBACK_DAYS = 180
MAX_ROWS_PER_OBSERVATION = 256
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
FIELDS = (
    "date", "open", "high", "low", "close", "volume", "amount",
    "suspend_flag",
)
_QMT_FIELDS = [
    "time", "open", "high", "low", "close", "volume", "amount",
    "suspendFlag",
]
_DIVIDEND_TYPES = {"raw": "none", "qfq": "front"}
_CODE_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_TOP_LEVEL_FIELDS = {
    "schema_version", "authority_grade", "decision_eligible",
    "decision_authority", "actions", "source_authentication",
    "quality_status", "permitted_uses", "created_at", "producer", "request",
    "observations", "artifact_sha256",
}
_SHANGHAI = timezone(timedelta(hours=8))


class QmtReadonlyProbeError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _date(value: object, field: str) -> str:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise QmtReadonlyProbeError(f"{field} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise QmtReadonlyProbeError(f"{field} must be a canonical ISO date")
    return str(value)


def _timestamp_value(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise QmtReadonlyProbeError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QmtReadonlyProbeError(f"{field} must include timezone")
    return parsed


def _timestamp(value: object, field: str) -> str:
    _timestamp_value(value, field)
    return str(value)


def _code(value: object) -> str:
    canonical = str(value or "").strip().upper()
    if not _CODE_PATTERN.fullmatch(canonical):
        raise QmtReadonlyProbeError(
            f"QMT code must use 000001.SZ/600000.SH/430001.BJ form: {value}"
        )
    return canonical


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise QmtReadonlyProbeError(f"{field} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise QmtReadonlyProbeError(f"{field} must be finite numeric data") from exc
    if not math.isfinite(result):
        raise QmtReadonlyProbeError(f"{field} must be finite numeric data")
    return result


def _row_date(time_value: object, index_value: object) -> str:
    for value in (time_value, index_value):
        if value is None:
            continue
        if isinstance(value, Real) and not isinstance(value, bool):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                parsed = datetime.fromtimestamp(
                    timestamp / 1000, tz=timezone.utc
                ).astimezone(_SHANGHAI).date()
                return parsed.isoformat()
        text = str(value).strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:10], fmt).date().isoformat()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            continue
    raise QmtReadonlyProbeError("QMT row has no recognizable trading date")


def _normalise_frame(frame, *, code: str, mode: str,
                     start: str, end: str) -> list[dict]:
    if frame is None or not hasattr(frame, "iterrows"):
        raise QmtReadonlyProbeError(f"QMT returned no frame for {code}/{mode}")
    rows = []
    for index, source in frame.iterrows():
        day = _row_date(source.get("time"), index)
        if not start <= day <= end:
            raise QmtReadonlyProbeError(
                f"QMT returned an out-of-scope row for {code}/{mode}: {day}"
            )
        values = {
            field: _number(source.get(field), f"{code}/{mode}/{day}.{field}")
            for field in ("open", "high", "low", "close", "volume", "amount")
        }
        suspend_flag = _number(
            source.get("suspendFlag", 0),
            f"{code}/{mode}/{day}.suspendFlag",
        )
        if suspend_flag not in (0.0, 1.0):
            raise QmtReadonlyProbeError("QMT suspendFlag must be 0 or 1")
        prices = [values[field] for field in ("open", "high", "low", "close")]
        suspended_zero = all(price == 0 for price in prices) \
            and values["volume"] == 0 and suspend_flag == 1
        if any(price <= 0 for price in prices) and not suspended_zero:
            raise QmtReadonlyProbeError(f"QMT OHLC is invalid: {code}/{mode}/{day}")
        if not suspended_zero and (
            values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < values["low"]
        ):
            raise QmtReadonlyProbeError(f"QMT OHLC is invalid: {code}/{mode}/{day}")
        if values["volume"] < 0 or values["amount"] < 0:
            raise QmtReadonlyProbeError(f"QMT volume/amount is invalid: {day}")
        rows.append({
            "date": day,
            **values,
            "suspend_flag": int(suspend_flag),
        })
        if len(rows) > MAX_ROWS_PER_OBSERVATION:
            raise QmtReadonlyProbeError(
                f"QMT row cap exceeded for {code}/{mode}"
            )
    rows.sort(key=lambda row: row["date"])
    if not rows:
        raise QmtReadonlyProbeError(f"QMT local cache is empty for {code}/{mode}")
    if len(rows) > MAX_ROWS_PER_OBSERVATION:
        raise QmtReadonlyProbeError(
            f"QMT row cap exceeded for {code}/{mode}: {len(rows)}"
        )
    if len({row["date"] for row in rows}) != len(rows):
        raise QmtReadonlyProbeError(f"QMT returned duplicate dates for {code}/{mode}")
    return rows


def _load_xtdata():
    try:
        from xtquant import xtdata  # type: ignore[import-not-found]
    except ImportError as exc:
        raise QmtReadonlyProbeError(
            "xtquant.xtdata is unavailable; run this probe inside the Windows QMT Python environment"
        ) from exc
    return xtdata


def _xtquant_version(xtdata) -> str:
    try:
        return importlib.metadata.version("xtquant")
    except importlib.metadata.PackageNotFoundError:
        value = getattr(xtdata, "__version__", "unknown")
        return str(value) if isinstance(value, (str, int, float)) else "unknown"


def build_qmt_readonly_probe(
    *,
    codes: list[str],
    start: str,
    end: str,
    adjustment_modes: list[str] | None = None,
    created_at: str | None = None,
    xtdata_module=None,
) -> dict:
    """Read only already-local QMT bars and return a sealed diagnostic artifact."""
    canonical_codes = sorted({_code(code) for code in codes})
    if not canonical_codes:
        raise QmtReadonlyProbeError("at least one QMT code is required")
    if len(canonical_codes) > MAX_CODES:
        raise QmtReadonlyProbeError(f"QMT probe supports at most {MAX_CODES} codes")
    start = _date(start, "start")
    end = _date(end, "end")
    if start > end:
        raise QmtReadonlyProbeError("start must not exceed end")
    if (datetime.strptime(end, "%Y-%m-%d").date()
            - datetime.strptime(start, "%Y-%m-%d").date()).days + 1 \
            > MAX_LOOKBACK_DAYS:
        raise QmtReadonlyProbeError(
            f"QMT probe supports at most {MAX_LOOKBACK_DAYS} calendar days"
        )
    modes = adjustment_modes or ["raw", "qfq"]
    if len(modes) != len(set(modes)) or any(mode not in _DIVIDEND_TYPES for mode in modes):
        raise QmtReadonlyProbeError("adjustment_modes must be unique raw/qfq values")
    created_at = _timestamp(
        created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_at",
    )
    xtdata = xtdata_module or _load_xtdata()
    observations = []
    for code in canonical_codes:
        for mode in modes:
            api_params = {
                "field_list": list(_QMT_FIELDS),
                "stock_list": [code],
                "period": "1d",
                "start_time": start.replace("-", ""),
                "end_time": end.replace("-", ""),
                "count": MAX_ROWS_PER_OBSERVATION,
                "dividend_type": _DIVIDEND_TYPES[mode],
                "fill_data": False,
            }
            response = xtdata.get_market_data_ex(**api_params)
            captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if not isinstance(response, dict) or set(response) != {code}:
                raise QmtReadonlyProbeError(
                    f"QMT response identity mismatch for {code}/{mode}"
                )
            frame = response[code]
            try:
                frame_size = len(frame)
            except TypeError as exc:
                raise QmtReadonlyProbeError(
                    f"QMT frame size is unavailable for {code}/{mode}"
                ) from exc
            if frame_size > MAX_ROWS_PER_OBSERVATION:
                raise QmtReadonlyProbeError(
                    f"QMT row cap exceeded for {code}/{mode}: {frame_size}"
                )
            rows = _normalise_frame(
                frame, code=code, mode=mode, start=start, end=end
            )
            observation = {
                "code": code,
                "adjustment_mode": mode,
                "dividend_type": _DIVIDEND_TYPES[mode],
                "captured_at": captured_at,
                "fields": list(FIELDS),
                "api_method": "xtdata.get_market_data_ex",
                "api_params": api_params,
                "price_identity": {
                    "source": "qmt_xtdata",
                    "adjustment_mode": mode,
                    "adjustment_version": (
                        f"xtdata-dividend_type-{_DIVIDEND_TYPES[mode]}"
                    ),
                    "volume_unit": "provider_unverified",
                    "amount_unit": "provider_unverified",
                    "finality": "unverified_current_observation",
                },
                "coverage": {
                    "status": "observed_subset_unverified",
                    "start": rows[0]["date"],
                    "end": rows[-1]["date"],
                    "watermark": rows[-1]["date"],
                    "row_count": len(rows),
                },
                "rows": rows,
                "response_sha256": _hash(rows),
            }
            observation["receipt_sha256"] = _hash(observation)
            observations.append(observation)
    for code in canonical_codes:
        code_observations = {
            item["adjustment_mode"]: item for item in observations
            if item["code"] == code
        }
        if {"raw", "qfq"}.issubset(code_observations):
            raw_dates = [row["date"] for row in code_observations["raw"]["rows"]]
            qfq_dates = [row["date"] for row in code_observations["qfq"]["rows"]]
            if raw_dates != qfq_dates:
                raise QmtReadonlyProbeError(
                    f"QMT raw/qfq date coverage differs for {code}"
                )
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "authority_grade": "diagnostic",
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "source_authentication": "unverified",
        "quality_status": "local_cache_observation",
        "permitted_uses": PERMITTED_USES,
        "created_at": created_at,
        "producer": {
            "name": "stockdata.qmt_readonly_probe",
            "api": "xtdata.get_market_data_ex",
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "xtquant_version": _xtquant_version(xtdata),
            "qmt_build": "unknown",
            "identity_status": "unverified",
        },
        "request": {
            "codes": canonical_codes,
            "start": start,
            "end": end,
            "period": "1d",
            "adjustment_modes": modes,
            "field_list": list(_QMT_FIELDS),
            "count": MAX_ROWS_PER_OBSERVATION,
            "adjustment_parameters": {
                mode: _DIVIDEND_TYPES[mode] for mode in modes
            },
            "fill_data": False,
        },
        "observations": observations,
    }
    artifact = {**unsigned, "artifact_sha256": _hash(unsigned)}
    if len(_canonical(artifact)) > MAX_ARTIFACT_BYTES:
        raise QmtReadonlyProbeError("QMT probe artifact byte cap exceeded")
    return artifact


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QmtReadonlyProbeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _verify_row(row: object, *, start: str, end: str) -> None:
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        raise QmtReadonlyProbeError("QMT probe row schema is invalid")
    day = _date(row.get("date"), "row.date")
    if not start <= day <= end:
        raise QmtReadonlyProbeError("QMT probe row is outside request scope")
    values = {
        field: _number(row.get(field), f"row.{field}")
        for field in ("open", "high", "low", "close", "volume", "amount")
    }
    suspend_flag = row.get("suspend_flag")
    if not isinstance(suspend_flag, int) or isinstance(suspend_flag, bool) \
            or suspend_flag not in (0, 1):
        raise QmtReadonlyProbeError("QMT probe suspend_flag is invalid")
    prices = [values[field] for field in ("open", "high", "low", "close")]
    suspended_zero = all(price == 0 for price in prices) \
        and values["volume"] == 0 and suspend_flag == 1
    if any(price <= 0 for price in prices) and not suspended_zero:
        raise QmtReadonlyProbeError("QMT probe row OHLC is invalid")
    if not suspended_zero and (
        values["high"] < max(values["open"], values["close"])
        or values["low"] > min(values["open"], values["close"])
        or values["high"] < values["low"]
    ):
        raise QmtReadonlyProbeError("QMT probe row OHLC is invalid")
    if values["volume"] < 0 or values["amount"] < 0:
        raise QmtReadonlyProbeError("QMT probe row volume/amount is invalid")


def verify_qmt_readonly_probe(payload: object) -> dict:
    """Verify a probe without importing QMT or selecting it as market data."""
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise QmtReadonlyProbeError("QMT probe artifact schema is incomplete")
    supplied_hash = payload.get("artifact_sha256")
    unsigned = {key: value for key, value in payload.items()
                if key != "artifact_sha256"}
    if supplied_hash != _hash(unsigned):
        raise QmtReadonlyProbeError("QMT probe artifact hash mismatch")
    if payload.get("schema_version") != SCHEMA_VERSION \
            or payload.get("authority_grade") != "diagnostic" \
            or payload.get("decision_eligible") is not False \
            or payload.get("decision_authority") is not False \
            or payload.get("actions") != [] \
            or payload.get("source_authentication") != "unverified" \
            or payload.get("quality_status") != "local_cache_observation" \
            or payload.get("permitted_uses") != PERMITTED_USES:
        raise QmtReadonlyProbeError("QMT probe authority contract is invalid")
    created_at = _timestamp_value(payload.get("created_at"), "created_at")
    producer = payload.get("producer")
    if not isinstance(producer, dict) or set(producer) != {
        "name", "api", "platform", "python_version", "xtquant_version",
        "qmt_build", "identity_status",
    } or producer.get("name") != "stockdata.qmt_readonly_probe" \
            or producer.get("api") != "xtdata.get_market_data_ex" \
            or producer.get("platform") != "Windows" \
            or not isinstance(producer.get("python_version"), str) \
            or not producer["python_version"] \
            or not isinstance(producer.get("xtquant_version"), str) \
            or not producer["xtquant_version"] \
            or not isinstance(producer.get("qmt_build"), str) \
            or not producer["qmt_build"] \
            or producer.get("identity_status") != "unverified":
        raise QmtReadonlyProbeError("QMT probe producer identity is invalid")
    request = payload.get("request")
    if not isinstance(request, dict) or set(request) != {
        "codes", "start", "end", "period", "adjustment_modes", "field_list",
        "count", "adjustment_parameters", "fill_data",
    } or request.get("period") != "1d" \
            or request.get("field_list") != _QMT_FIELDS \
            or request.get("count") != MAX_ROWS_PER_OBSERVATION \
            or request.get("fill_data") is not False:
        raise QmtReadonlyProbeError("QMT probe request contract is invalid")
    codes = request.get("codes")
    modes = request.get("adjustment_modes")
    if not isinstance(codes, list) or not codes or len(codes) > MAX_CODES \
            or codes != sorted(set(codes)) \
            or any(not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code)
                   for code in codes) \
            or not isinstance(modes, list) or not modes \
            or len(modes) != len(set(modes)) \
            or any(mode not in _DIVIDEND_TYPES for mode in modes) \
            or request.get("adjustment_parameters") != {
                mode: _DIVIDEND_TYPES[mode] for mode in modes
            }:
        raise QmtReadonlyProbeError("QMT probe request identity is invalid")
    start = _date(request.get("start"), "request.start")
    end = _date(request.get("end"), "request.end")
    span = (datetime.strptime(end, "%Y-%m-%d").date()
            - datetime.strptime(start, "%Y-%m-%d").date()).days + 1
    if start > end or span > MAX_LOOKBACK_DAYS:
        raise QmtReadonlyProbeError("QMT probe request date scope is invalid")
    observations = payload.get("observations")
    expected_pairs = [(code, mode) for code in codes for mode in modes]
    if not isinstance(observations, list) or len(observations) != len(expected_pairs):
        raise QmtReadonlyProbeError("QMT probe observation coverage is incomplete")
    actual_pairs = []
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {
            "code", "adjustment_mode", "dividend_type", "captured_at",
            "fields", "api_method", "api_params", "price_identity",
            "coverage", "rows", "response_sha256", "receipt_sha256",
        }:
            raise QmtReadonlyProbeError("QMT probe observation schema is invalid")
        code = observation.get("code")
        mode = observation.get("adjustment_mode")
        actual_pairs.append((code, mode))
        expected_params = {
            "field_list": list(_QMT_FIELDS),
            "stock_list": [code],
            "period": "1d",
            "start_time": start.replace("-", ""),
            "end_time": end.replace("-", ""),
            "count": MAX_ROWS_PER_OBSERVATION,
            "dividend_type": _DIVIDEND_TYPES.get(mode),
            "fill_data": False,
        }
        expected_identity = {
            "source": "qmt_xtdata",
            "adjustment_mode": mode,
            "adjustment_version": (
                f"xtdata-dividend_type-{_DIVIDEND_TYPES.get(mode)}"
            ),
            "volume_unit": "provider_unverified",
            "amount_unit": "provider_unverified",
            "finality": "unverified_current_observation",
        }
        if mode not in _DIVIDEND_TYPES \
                or observation.get("dividend_type") != _DIVIDEND_TYPES[mode] \
                or observation.get("fields") != list(FIELDS) \
                or observation.get("api_method") != "xtdata.get_market_data_ex" \
                or observation.get("api_params") != expected_params \
                or observation.get("price_identity") != expected_identity \
                or _timestamp_value(
                    observation.get("captured_at"), "captured_at"
                ) < created_at:
            raise QmtReadonlyProbeError("QMT probe observation identity is invalid")
        rows = observation.get("rows")
        if not isinstance(rows, list) or not rows \
                or len(rows) > MAX_ROWS_PER_OBSERVATION \
                or observation.get("response_sha256") != _hash(rows):
            raise QmtReadonlyProbeError("QMT probe row closure is invalid")
        unsigned_observation = {
            key: value for key, value in observation.items()
            if key != "receipt_sha256"
        }
        if observation.get("receipt_sha256") != _hash(unsigned_observation):
            raise QmtReadonlyProbeError("QMT probe receipt closure is invalid")
        for row in rows:
            _verify_row(row, start=start, end=end)
        dates = [row["date"] for row in rows]
        if dates != sorted(set(dates)):
            raise QmtReadonlyProbeError("QMT probe dates must be unique and sorted")
        if observation.get("coverage") != {
            "status": "observed_subset_unverified",
            "start": dates[0],
            "end": dates[-1],
            "watermark": dates[-1],
            "row_count": len(dates),
        }:
            raise QmtReadonlyProbeError("QMT probe coverage closure is invalid")
    if actual_pairs != expected_pairs:
        raise QmtReadonlyProbeError(
            "QMT probe observation identity coverage is invalid"
        )
    for code in codes:
        code_observations = {
            item["adjustment_mode"]: item for item in observations
            if item["code"] == code
        }
        if {"raw", "qfq"}.issubset(code_observations):
            raw_dates = [row["date"] for row in code_observations["raw"]["rows"]]
            qfq_dates = [row["date"] for row in code_observations["qfq"]["rows"]]
            if raw_dates != qfq_dates:
                raise QmtReadonlyProbeError(
                    "QMT probe raw/qfq date coverage differs"
                )
    if len(_canonical(payload)) > MAX_ARTIFACT_BYTES:
        raise QmtReadonlyProbeError("QMT probe artifact byte cap exceeded")
    return payload


def load_qmt_readonly_probe(path: str | Path) -> dict:
    artifact_path = Path(path)
    try:
        metadata = artifact_path.lstat()
    except OSError as exc:
        raise QmtReadonlyProbeError("QMT probe artifact is unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise QmtReadonlyProbeError(
            "QMT probe artifact must be a single-link regular file"
        )
    if metadata.st_size > MAX_ARTIFACT_BYTES:
        raise QmtReadonlyProbeError("QMT probe artifact byte cap exceeded")
    raw = artifact_path.read_bytes()
    try:
        payload = json.loads(
            raw.decode("ascii"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtReadonlyProbeError("QMT probe artifact is unreadable") from exc
    if raw != _canonical(payload):
        raise QmtReadonlyProbeError("QMT probe artifact bytes are not canonical")
    return verify_qmt_readonly_probe(payload)


def write_qmt_readonly_probe(output_root: str | Path, artifact: dict) -> Path:
    verify_qmt_readonly_probe(artifact)
    supplied_hash = artifact.get("artifact_sha256")
    unsigned = {key: value for key, value in artifact.items()
                if key != "artifact_sha256"}
    if supplied_hash != _hash(unsigned):
        raise QmtReadonlyProbeError("QMT probe artifact is not sealed")
    output_root = Path(output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / f"{supplied_hash}.json"
    raw = _canonical(artifact)
    try:
        with output.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            existing = load_qmt_readonly_probe(output)
        except QmtReadonlyProbeError as exc:
            raise QmtReadonlyProbeError(
                "QMT probe output conflicts with existing file"
            ) from exc
        if existing != artifact or output.read_bytes() != raw:
            raise QmtReadonlyProbeError("QMT probe output conflicts with existing file")
    return output
