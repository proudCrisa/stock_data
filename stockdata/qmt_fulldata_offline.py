"""Offline replay of saved QMT full-data daily-bar evidence.

This module never opens a network connection.  Production volume-unit evidence
is limited to six sealed M1 request/response pairs and is not a general QMT
daily-bar unit declaration.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .daily_bar_product import _hash, _validate_bar
from .liquidity_amount_product import _timestamp


SOURCE = "qmt.fulldata"
REQUEST_TYPE = "history_kline"
RESPONSE_STATUS = "ok"
VOLUME_EVIDENCE_SCHEMA = "stockdata-qmt-fulldata-volume-unit-evidence/1"
PRODUCTION_VOLUME_EVIDENCE_ID = "approved-a08-qmt-fulldata-volume-hand/2"
TEST_ONLY_VOLUME_EVIDENCE_ID = "test-only-qmt-fulldata-volume-hand/1"
_MODES = {"raw": "none", "qfq": "front"}
_REQUEST_PARAMS = {"symbol", "period", "start", "end", "count", "dividend_type"}
_RESPONSE_FIELDS = {"data", "dividend_type", "id", "status", "type"}
_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_CROSSPROOF = {
    "summary_sha256": "33dbe1a4f2c6afe097fecd05b0205d2b7323409aafbf70bd83b9e70d1d720230",
    "comparison_sha256": "cbecfa86d40a29c313a81901e1fd0f62828ef82994315061954cd0360da5d950",
}
APPROVED_VOLUME_PAIRS = {
    ("511010.SH", "raw"): ("729da01236c02556fd204ae28f5a78add4de3cf90b297e62d12125c804acaf4c", "dccab6e2db1e3ec8d9fa266255e4c90c40b62273d0038717b18f12914c9d1d68", "2026-09-14T16:49:44.279094+08:00"),
    ("511010.SH", "qfq"): ("ea80547a1f88109534863ba89a07c9022faf68c9173ded0b447f7e7adf7e90f5", "19785ad55bff8b3ccb4a80cccc77b1e8658ef1a4a57773524cf4a40d44171dd0", "2026-09-14T16:53:43.303818+08:00"),
    ("518880.SH", "raw"): ("27e6aede4d7541d8420975cf1283b0b8c07df709c59e26ed4f0f8e1314fa1306", "997ff4284ea9e988032e2d467f3e4c34876639850c66c5f5b035bd9266d23ada", "2026-09-14T16:57:41.984161+08:00"),
    ("518880.SH", "qfq"): ("b158c6880b50655582a2378f3b93eb540c25bb31c66423ea368a26908113002e", "c7f250b9b284f10510ad0d1c350f63288e67896fa289063c13a7ae536c45d3d1", "2026-09-14T16:59:40.020452+08:00"),
    ("561980.SH", "raw"): ("830f96f010bf04304ea5fe38be9eca9d7f66a795961d2c37e20a703c8f9de73b", "0fc48344b634286ac898844ec980fe03fa7d30fe9526838179348c5a45a976d3", "2026-09-14T17:03:38.669521+08:00"),
    ("561980.SH", "qfq"): ("fb230c275baf003f7c2b5ac044f6f658ffc693ad3312712f6202ad88ec4a7764", "4ccbec979ebc0fafcf0009f8110dce96b71d35856b48570021896447a619621c", "2026-09-14T17:07:38.097633+08:00"),
}

SEALED_BUNDLE_SCHEMA = "trading-sealed-qmt-capture-bundle/1"
SEALED_IDENTITY_SCHEMA = "a08-qmt-six-role-identity/1"


def _file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root):
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_file_sha256(path)))
    return digest.hexdigest()


def load_sealed_capture_directory(path, *, expected_tree_sha256, asof,
                                  expected_symbols):
    """Load the exact reviewed A08 capture set without opening a network path."""
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise ValueError("sealed QMT capture directory identity differs")
    root = supplied.resolve()
    expected_symbols = sorted(expected_symbols)
    approved_symbols = sorted({symbol for symbol, _ in APPROVED_VOLUME_PAIRS})
    if not root.is_dir() or expected_symbols != approved_symbols \
            or any(item.is_symlink() for item in root.rglob("*")) \
            or _tree_sha256(root) != expected_tree_sha256:
        raise ValueError("sealed QMT capture directory identity differs")
    required = {
        "results.json", "volume-crossproof.json", "six-role-identity.json",
        "sealed-capture-bundle.json",
    }
    pair_names = {f"{symbol}-{role}.json" for symbol in approved_symbols
                  for role in ("raw", "qfq")}
    required |= {f"requests/{name}" for name in pair_names}
    required |= {f"responses/{name}" for name in pair_names}
    try:
        results = json.loads((root / "results.json").read_text(encoding="utf-8"))
        identity = json.loads((root / "six-role-identity.json").read_text(encoding="utf-8"))
        crossproof = json.loads((root / "volume-crossproof.json").read_text(encoding="utf-8"))
        bundle = json.loads((root / "sealed-capture-bundle.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("sealed QMT capture directory is unreadable") from exc
    if not isinstance(results, list):
        raise ValueError("sealed QMT retry provenance differs")
    for item in results:
        if not isinstance(item, dict) or item.get("allow_pool_fallback") is not False:
            raise ValueError("sealed QMT retry provenance differs")
        for field in ("request_path", "response_path"):
            if field in item:
                relative = item[field]
                if not isinstance(relative, str) or not relative.startswith("attempts/"):
                    raise ValueError("sealed QMT retry path differs")
                required.add(relative)
    actual = {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()}
    if actual != required:
        raise ValueError("sealed QMT capture inventory differs")
    if not isinstance(identity, dict) or identity.get("schema_version") != SEALED_IDENTITY_SCHEMA \
            or identity.get("source") != SOURCE or identity.get("allow_pool_fallback") is not False \
            or identity.get("asof") != asof \
            or identity.get("volume_crossproof_sha256") != _file_sha256(root / "volume-crossproof.json") \
            or _file_sha256(root / "six-role-identity.json") != _CROSSPROOF["summary_sha256"] \
            or _file_sha256(root / "volume-crossproof.json") != _CROSSPROOF["comparison_sha256"]:
        raise ValueError("sealed QMT identity or crossproof differs")
    expected_keys = {(symbol, role) for symbol in approved_symbols for role in ("raw", "qfq")}
    proof_keys = set()
    if not isinstance(crossproof, list) or len(crossproof) != len(approved_symbols):
        raise ValueError("sealed QMT volume crossproof differs")
    for proof in crossproof:
        if not isinstance(proof, dict) or proof.get("symbol") not in approved_symbols \
                or proof.get("common_day") != asof.replace("-", "") \
                or any(proof.get(field) is not True for field in (
                    "qmt_raw_qfq_dates_equal", "qmt_raw_qfq_volume_equal",
                    "qmt_volume_matches_tencent_lots",
                    "qmt_times_100_matches_tencent_shares",
                    "ohlc_equal_within_1e_9", "ratio_in_qmt_ohlc_range")):
            raise ValueError("sealed QMT volume crossproof differs")
        proof_keys.add(proof["symbol"])
    if proof_keys != set(approved_symbols):
        raise ValueError("sealed QMT volume crossproof differs")
    successful = {}
    grouped = {key: [] for key in expected_keys}
    for item in results:
        key = (item.get("symbol"), item.get("role"))
        if key not in grouped:
            raise ValueError("sealed QMT retry role differs")
        grouped[key].append(item)
        validation = item.get("validation")
        if isinstance(validation, dict) and validation.get("status") == "ok":
            successful[key] = item
    for key, attempts in grouped.items():
        if len(attempts) not in (1, 2) or key not in successful \
                or attempts[-1] is not successful[key] \
                or (len(attempts) == 2 and (
                    attempts[0].get("error") != "QmtError: fulldata \u8d85\u65f6"
                    or "response_path" in attempts[0])):
            raise ValueError("sealed QMT retry provenance differs")
    if set(successful) != expected_keys:
        raise ValueError("sealed QMT successful roles differ")
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "asof", "captures"} \
            or bundle.get("schema_version") != SEALED_BUNDLE_SCHEMA or bundle.get("asof") != asof \
            or not isinstance(bundle.get("captures"), list):
        raise ValueError("sealed QMT capture bundle differs")
    sealed = {}
    for entry in bundle["captures"]:
        if not isinstance(entry, dict) or set(entry) != {"symbol", "role", "capture"}:
            raise ValueError("sealed QMT capture entry differs")
        role = entry["role"]
        adjustment = {"execution": "raw", "signal": "qfq"}.get(role)
        key = (entry["symbol"], adjustment)
        output_key = (entry["symbol"], role)
        capture = entry["capture"]
        if key not in expected_keys or output_key in sealed \
                or capture.get("observed_at") != successful[key].get("finished_at"):
            raise ValueError("sealed QMT capture role or completion differs")
        sealed[output_key] = capture
    if set(sealed) != {(symbol, role) for symbol in approved_symbols
                       for role in ("execution", "signal")}:
        raise ValueError("sealed QMT capture panel differs")
    return sealed


def build_capture(*, request, response, observed_at, volume_unit_evidence,
                  request_raw=None, response_raw=None):
    """Keep the original canonical wire objects with the external observation time."""
    return {"source": SOURCE, "observed_at": observed_at, "request": request,
            "response": response, "volume_unit_evidence": volume_unit_evidence,
            "request_raw": request_raw, "response_raw": response_raw}


def _verify_volume_evidence(evidence, *, request, response, request_raw,
                            response_raw, observed_at, symbol, adjustment,
                            allow_test_volume_evidence):
    base = {"schema_version", "evidence_id", "volume_unit", "request_sha256",
            "response_sha256", "evidence_sha256"}
    expected = base | set(_CROSSPROOF) if isinstance(evidence, dict) \
        and evidence.get("evidence_id") == PRODUCTION_VOLUME_EVIDENCE_ID else base
    if not isinstance(evidence, dict) or set(evidence) != expected \
            or evidence.get("schema_version") != VOLUME_EVIDENCE_SCHEMA \
            or evidence.get("volume_unit") != "hand" \
            or evidence.get("request_sha256") != _hash(request) \
            or evidence.get("response_sha256") != _hash(response):
        raise ValueError("QMT full-data volume-unit evidence is invalid")
    body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    if evidence.get("evidence_sha256") != _hash(body):
        raise ValueError("QMT full-data volume-unit evidence hash differs")
    identifier = evidence["evidence_id"]
    if allow_test_volume_evidence and identifier == TEST_ONLY_VOLUME_EVIDENCE_ID:
        return
    if identifier != PRODUCTION_VOLUME_EVIDENCE_ID or not isinstance(request_raw, str) \
            or not isinstance(response_raw, str) or (symbol, adjustment) not in APPROVED_VOLUME_PAIRS:
        raise ValueError("QMT full-data volume unit lacks approved source evidence")
    if any(evidence.get(key) != value for key, value in _CROSSPROOF.items()):
        raise ValueError("QMT full-data volume crossproof differs")
    request_digest, response_digest, completed_at = APPROVED_VOLUME_PAIRS[(symbol, adjustment)]
    if _timestamp(observed_at) != _timestamp(completed_at):
        raise ValueError("QMT full-data observation differs approved completion")
    try:
        if json.loads(request_raw) != request or json.loads(response_raw) != response:
            raise ValueError("QMT full-data approved bytes differ structured capture")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("QMT full-data approved bytes are invalid") from exc
    if hashlib.sha256(request_raw.encode("utf-8")).hexdigest() != request_digest \
            or hashlib.sha256(response_raw.encode("utf-8")).hexdigest() != response_digest:
        raise ValueError("QMT full-data approved pair bytes differ")


def replay(capture, *, symbol, start, asof, adjustment, cutoff,
           allow_test_volume_evidence=False):
    """Validate saved wire evidence and return exact rows without filling gaps."""
    if not isinstance(capture, dict) or set(capture) != {
            "source", "observed_at", "request", "response", "volume_unit_evidence",
            "request_raw", "response_raw"} \
            or capture.get("source") != SOURCE:
        raise ValueError("QMT full-data capture identity differs")
    observed = _timestamp(capture["observed_at"])
    if observed <= _timestamp(f"{asof}T15:00:00+08:00") or observed >= cutoff:
        raise ValueError("QMT full-data observation is outside final pre-cutoff window")
    request, response = capture["request"], capture["response"]
    if not isinstance(request, dict) or set(request) != {"params", "request_id", "type"} \
            or request.get("type") != REQUEST_TYPE or not isinstance(request.get("request_id"), str) \
            or not request["request_id"]:
        raise ValueError("QMT full-data request schema differs")
    params = request.get("params")
    expected_dividend = _MODES.get(adjustment)
    if not isinstance(params, dict) or set(params) != _REQUEST_PARAMS \
            or params.get("symbol") != symbol or params.get("period") != "1d" \
            or params.get("dividend_type") != expected_dividend \
            or not isinstance(params.get("count"), int) or isinstance(params["count"], bool) \
            or params["count"] <= 0 or any(not isinstance(params[key], str) for key in ("start", "end")):
        raise ValueError("QMT full-data request binding differs")
    if (params["start"] and params["start"] != start.replace("-", "")) \
            or (params["end"] and params["end"] != asof.replace("-", "")):
        raise ValueError("QMT full-data request range differs")
    if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS \
            or response.get("id") != request["request_id"] \
            or response.get("type") != REQUEST_TYPE or response.get("status") != RESPONSE_STATUS \
            or response.get("dividend_type") != expected_dividend:
        raise ValueError("QMT full-data response binding differs")
    _verify_volume_evidence(capture["volume_unit_evidence"], request=request, response=response,
                            request_raw=capture["request_raw"], response_raw=capture["response_raw"],
                            observed_at=capture["observed_at"],
                            symbol=symbol, adjustment=adjustment,
                            allow_test_volume_evidence=allow_test_volume_evidence)
    data = response.get("data")
    if not isinstance(data, dict) or set(data) != {symbol}:
        raise ValueError("QMT full-data response symbol closure differs")
    record = data[symbol]
    if not isinstance(record, dict) or set(record) != {"columns", "dividend_type", "index"} \
            or record.get("dividend_type") != expected_dividend:
        raise ValueError("QMT full-data response record differs")
    index, columns = record.get("index"), record.get("columns")
    if not isinstance(index, list) or not index or not isinstance(columns, dict) \
            or set(columns) != set(_BAR_FIELDS) or len(index) > params["count"]:
        raise ValueError("QMT full-data response rows are malformed")
    if any(not isinstance(day, str) or len(day) != 8 or not day.isdigit() for day in index) \
            or index != sorted(set(index)):
        raise ValueError("QMT full-data response dates are invalid")
    if any(not isinstance(columns[field], list) or len(columns[field]) != len(index)
           for field in _BAR_FIELDS):
        raise ValueError("QMT full-data response columns are malformed")
    rows = []
    for offset, day in enumerate(index):
        row = {"date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
               **{field: columns[field][offset] for field in _BAR_FIELDS}}
        try:
            row = {key: value if key == "date" else float(value) for key, value in row.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("QMT full-data row is nonnumeric") from exc
        if any(not math.isfinite(row[field]) for field in _BAR_FIELDS):
            raise ValueError("QMT full-data row is nonfinite")
        _validate_bar(row)
        rows.append(row)
    selected = [row for row in rows if start <= row["date"] <= asof]
    if len(selected) < 20 or selected[-1]["date"] != asof:
        raise ValueError("QMT full-data lacks history or exact final watermark")
    return [{**row, "volume": row["volume"] * 100.0} for row in selected], adjustment
