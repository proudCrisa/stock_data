"""Portable signed MainNetFlow rows for Trading's existing factor logic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from types import MappingProxyType

from .authority import load_enrolled_trust_registry_bytes
from .provider_authority_admission import (
    SOURCE_RECEIPT_SCHEMA,
    AdmittedProviderAuthority,
    admit_signed_component_authority,
)
from .ticker import normalize


FORMAL_FUND_FLOW_SCHEMA = "stockdata-formal-fund-flow-supplement/1"
FUND_FLOW_CAPTURE_SCHEMA = "stockdata-westock-asfund-capture/1"
FUND_FLOW_ARTIFACT_SCHEMA = "stockdata-fund-flow/1"
SOURCE = "westock"
SOURCE_VERSION = "asfund/1"
SOURCE_FIELD = "MainNetFlow"
UNIT = "CNY"
MIN_SESSIONS = 39


@dataclass(frozen=True)
class FundFlowRow:
    date: str
    main_net_flow: float


@dataclass(frozen=True)
class FundFlowBindings:
    registry_sha256: str
    provider_manifest_sha256: str
    decision_cutoff: str
    calendar_artifact_sha256: str
    calendar_signature_sha256: str
    fund_flow_artifact_sha256: str
    fund_flow_signature_sha256: str
    source_receipt_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedFundFlowSupplement:
    symbols: tuple[str, ...]
    sessions: tuple[str, ...]
    rows_by_symbol: Mapping[str, tuple[FundFlowRow, ...]]
    bindings: FundFlowBindings


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
        raise ValueError("fund-flow evidence is not canonical JSON data") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite JSON number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite JSON number") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{field} must be a finite JSON number")
    return normalized


def _day(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical ISO date")
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise ValueError(f"{field} must be a canonical ISO date")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be a canonical timezone-aware timestamp"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.isoformat() != value
    ):
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
    return parsed


def _decision_cutoff(value: object) -> tuple[datetime, str]:
    if isinstance(value, str) and value.endswith("Z"):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise ValueError("decision_cutoff must be canonical") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
            raise ValueError("decision_cutoff must be canonical")
        return parsed, parsed.isoformat()
    parsed = _timestamp(value, "decision_cutoff")
    return parsed, parsed.isoformat()


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("fund-flow symbols must be a sequence")
    symbols = tuple(values)
    if (
        not symbols
        or any(not isinstance(symbol, str) for symbol in symbols)
        or symbols != tuple(sorted(set(symbols)))
        or any(normalize(symbol) != symbol for symbol in symbols)
    ):
        raise ValueError("fund-flow symbols must be canonical, sorted, and unique")
    return symbols


def _wire_code(symbol: str) -> str:
    digits, separator, exchange = symbol.partition(".")
    if separator != "." or exchange not in {"SH", "SZ"}:
        raise ValueError("westock asfund supports only canonical SH/SZ symbols")
    return f"{exchange.lower()}{digits}"


def validate_fund_flow_record(
    payload: object, *, panel_entry: str | None = None
) -> Mapping[str, object]:
    """Validate one signed provider-reported MainNetFlow JSON number."""

    fields = {
        "source",
        "source_version",
        "source_field",
        "code",
        "date",
        "main_net_flow",
        "unit",
        "provider_manifest_sha256",
        "asof",
        "decision_cutoff",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise ValueError("fund_flow record payload is incomplete")
    if (
        payload["source"] != SOURCE
        or payload["source_version"] != SOURCE_VERSION
        or payload["source_field"] != SOURCE_FIELD
        or payload["unit"] != UNIT
    ):
        raise ValueError("fund_flow source identity differs")
    code = payload["code"]
    if not isinstance(code, str) or normalize(code) != code:
        raise ValueError("fund_flow code must be canonical")
    session = _day(payload["date"], "fund_flow date")
    asof = _day(payload["asof"], "fund_flow asof")
    if session > asof:
        raise ValueError("fund_flow date is after asof")
    _finite_number(payload["main_net_flow"], "fund_flow main_net_flow")
    _sha256(payload["provider_manifest_sha256"], "provider_manifest_sha256")
    _decision_cutoff(payload["decision_cutoff"])
    if panel_entry != f"{code}@{session}":
        raise ValueError("fund_flow payload differs panel entry")
    return payload


def _calendar_window(
    calendar: AdmittedProviderAuthority,
    *,
    symbols: tuple[str, ...],
    asof: str,
    decision_cutoff: str,
) -> tuple[str, ...]:
    if calendar.component != "trading_calendar":
        raise ValueError(
            "fund-flow window requires admitted trading_calendar authority"
        )
    entries = tuple(sorted(calendar.payload_by_panel))
    sessions = tuple(sorted({entry.split("@")[1] for entry in entries}))
    if len(sessions) < MIN_SESSIONS or not sessions or sessions[-1] != asof:
        raise ValueError(
            "fund-flow calendar requires at least 39 sessions ending at asof"
        )
    expected = tuple(
        sorted(f"{symbol}@{session}" for symbol in symbols for session in sessions)
    )
    if entries != expected:
        raise ValueError("fund-flow calendar differs exact symbol-session window")
    cutoff, admission_cutoff = _decision_cutoff(decision_cutoff)
    if set(calendar.decision_cutoff_by_panel.values()) != {admission_cutoff}:
        raise ValueError("fund-flow calendar differs formal decision cutoff")

    phases_by_session: dict[str, Mapping[str, object]] = {}
    for session in sessions:
        phases = calendar.payload_by_panel[f"{symbols[0]}@{session}"]
        if any(
            calendar.payload_by_panel[f"{symbol}@{session}"] != phases
            for symbol in symbols[1:]
        ):
            raise ValueError("fund-flow calendar phases differ by symbol")
        phases_by_session[session] = phases
    for current, following in zip(sessions, sessions[1:]):
        next_cutoff = _timestamp(
            phases_by_session[current]["next_session_decision_cutoff_at"],
            "next_session_decision_cutoff_at",
        )
        if next_cutoff.date().isoformat() != following:
            raise ValueError("fund-flow calendar session chain has a gap")
    final_phases = phases_by_session[asof]
    close = _timestamp(final_phases["session_close_at"], "session_close_at")
    next_cutoff = _timestamp(
        final_phases["next_session_decision_cutoff_at"],
        "next_session_decision_cutoff_at",
    )
    if not close < cutoff < next_cutoff:
        raise ValueError("fund-flow cutoff is outside signed finality window")
    return sessions


def build_fund_flow_authority_inputs(
    captures: Sequence[Mapping[str, object]],
    *,
    expected_symbols: Sequence[str],
    calendar_authority: AdmittedProviderAuthority,
    provider_manifest_sha256: str,
    asof: str,
    decision_cutoff: str,
) -> dict[str, object]:
    """Replay an exact declared calendar window into signable fund-flow inputs."""

    symbols = _symbols(expected_symbols)
    asof = _day(asof, "asof")
    manifest = _sha256(provider_manifest_sha256, "provider_manifest_sha256")
    cutoff, _ = _decision_cutoff(decision_cutoff)
    sessions = _calendar_window(
        calendar_authority,
        symbols=symbols,
        asof=asof,
        decision_cutoff=decision_cutoff,
    )
    final_close = _timestamp(
        calendar_authority.payload_by_panel[f"{symbols[0]}@{asof}"]["session_close_at"],
        "session_close_at",
    )

    evidence: dict[str, object] = {}
    receipts: dict[str, object] = {}
    records: list[dict[str, object]] = []
    for capture in captures:
        if not isinstance(capture, Mapping) or set(capture) != {
            "schema_version",
            "source",
            "source_version",
            "request",
            "observed_at",
            "response",
        }:
            raise ValueError("fund-flow capture schema is incomplete")
        if (
            capture["schema_version"] != FUND_FLOW_CAPTURE_SCHEMA
            or capture["source"] != SOURCE
            or capture["source_version"] != SOURCE_VERSION
        ):
            raise ValueError("fund-flow capture source identity differs")
        request = capture["request"]
        response = capture["response"]
        if not isinstance(request, Mapping) or set(request) != {"code", "start", "end"}:
            raise ValueError("fund-flow capture request is incomplete")
        if not isinstance(response, Mapping) or set(response) != {
            "field",
            "unit",
            "rows",
        }:
            raise ValueError("fund-flow capture response is incomplete")
        wire_code = request["code"]
        if not isinstance(wire_code, str):
            raise ValueError("fund-flow request code is invalid")
        code = normalize(wire_code)
        if wire_code != _wire_code(code):
            raise ValueError(
                "fund-flow request code is not canonical westock wire format"
            )
        if code not in symbols or code in evidence:
            raise ValueError("fund-flow capture symbols differ exact requested set")
        if request["start"] != sessions[0] or request["end"] != sessions[-1]:
            raise ValueError("fund-flow request differs signed calendar window")
        if response["field"] != SOURCE_FIELD or response["unit"] != UNIT:
            raise ValueError("fund-flow response source field or unit differs")
        observed = _timestamp(capture["observed_at"], "fund-flow observed_at")
        if not final_close <= observed < cutoff:
            raise ValueError("fund-flow observation is outside finality window")
        rows = response["rows"]
        if not isinstance(rows, list) or len(rows) != len(sessions):
            raise ValueError("fund-flow response differs exact signed session window")
        observed_sessions: list[str] = []
        code_records: list[dict[str, object]] = []
        for row, session in zip(rows, sessions):
            if not isinstance(row, Mapping) or set(row) != {"date", SOURCE_FIELD}:
                raise ValueError("fund-flow response row is incomplete")
            row_day = _day(row["date"], "fund-flow response date")
            value = row[SOURCE_FIELD]
            if row_day != session:
                raise ValueError(
                    "fund-flow response has a missing or unordered session"
                )
            _finite_number(value, "fund-flow MainNetFlow")
            observed_sessions.append(row_day)
            payload = {
                "source": SOURCE,
                "source_version": SOURCE_VERSION,
                "source_field": SOURCE_FIELD,
                "code": code,
                "date": row_day,
                "main_net_flow": value,
                "unit": UNIT,
                "provider_manifest_sha256": manifest,
                "asof": asof,
                "decision_cutoff": decision_cutoff,
            }
            code_records.append(
                {
                    "panel_entry": f"{code}@{row_day}",
                    "payload": payload,
                    "record_sha256": _hash(payload),
                    "source_receipt_ids": [],
                    "effective_at": str(
                        calendar_authority.payload_by_panel[f"{code}@{row_day}"][
                            "session_close_at"
                        ]
                    ),
                    "available_at": observed.isoformat(),
                }
            )
        if tuple(observed_sessions) != sessions:
            raise ValueError("fund-flow response differs signed sessions")
        retained_capture = deepcopy(dict(capture))
        receipt = {
            "schema_version": SOURCE_RECEIPT_SCHEMA,
            "source": f"{SOURCE}:{SOURCE_VERSION}:{SOURCE_FIELD}:{UNIT}",
            "observed_at": observed.isoformat(),
            "response_sha256": _hash(
                {
                    "request": retained_capture["request"],
                    "response": retained_capture["response"],
                }
            ),
            "bindings": [
                {
                    "component": "fund_flow",
                    "panel_entry": record["panel_entry"],
                    "record_sha256": record["record_sha256"],
                }
                for record in code_records
            ],
        }
        receipt_id = _hash(receipt)
        for record in code_records:
            record["source_receipt_ids"] = [receipt_id]
        evidence[code] = retained_capture
        receipts[receipt_id] = receipt
        records.extend(code_records)
    if set(evidence) != set(symbols):
        raise ValueError("fund-flow captures do not cover exact requested symbols")
    records.sort(key=lambda record: str(record["panel_entry"]))
    artifact = {
        "schema_version": FUND_FLOW_ARTIFACT_SCHEMA,
        "component": "fund_flow",
        "panel": [record["panel_entry"] for record in records],
        "records": records,
    }
    return {
        "source_evidence": {code: evidence[code] for code in symbols},
        "artifact": artifact,
        "source_receipts": {
            receipt_id: receipts[receipt_id] for receipt_id in sorted(receipts)
        },
    }


def verify_formal_fund_flow(
    payload: object,
    *,
    expected_registry_sha256: str,
    provider_manifest_sha256: str,
    asof: str,
    decision_cutoff: str,
    expected_symbols: Sequence[str],
) -> VerifiedFundFlowSupplement:
    """Verify one complete portable package and return raw semantic rows."""

    fields = {
        "schema_version",
        "provider_manifest_sha256",
        "asof",
        "decision_cutoff",
        "symbols",
        "registry",
        "calendar",
        "fund_flow",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise ValueError("formal fund-flow supplement schema is incomplete")
    symbols = _symbols(expected_symbols)
    manifest = _sha256(provider_manifest_sha256, "provider_manifest_sha256")
    asof = _day(asof, "asof")
    _, admission_cutoff = _decision_cutoff(decision_cutoff)
    if (
        payload["schema_version"] != FORMAL_FUND_FLOW_SCHEMA
        or payload["provider_manifest_sha256"] != manifest
        or payload["asof"] != asof
        or payload["decision_cutoff"] != decision_cutoff
        or payload["symbols"] != list(symbols)
    ):
        raise ValueError(
            "formal fund-flow supplement differs external decision identity"
        )
    registry = load_enrolled_trust_registry_bytes(
        _canonical(payload["registry"]),
        expected_sha256=_sha256(expected_registry_sha256, "expected_registry_sha256"),
    )
    calendar_inputs = payload["calendar"]
    if not isinstance(calendar_inputs, Mapping) or set(calendar_inputs) != {
        "artifact",
        "source_receipts",
        "authority_envelope",
    }:
        raise ValueError("formal fund-flow calendar closure is incomplete")
    calendar_artifact = calendar_inputs["artifact"]
    if not isinstance(calendar_artifact, Mapping) or not isinstance(
        calendar_artifact.get("panel"), list
    ):
        raise ValueError("formal fund-flow calendar artifact is invalid")
    calendar = admit_signed_component_authority(
        component="trading_calendar",
        artifact_value=calendar_artifact,
        authority_envelope=calendar_inputs["authority_envelope"],
        expected_panel=calendar_artifact["panel"],
        bound_source_receipts=calendar_inputs["source_receipts"],
        registry=registry,
        current_decision_observation_cutoff=admission_cutoff,
    )
    sessions = _calendar_window(
        calendar,
        symbols=symbols,
        asof=asof,
        decision_cutoff=decision_cutoff,
    )
    fund_inputs = payload["fund_flow"]
    if not isinstance(fund_inputs, Mapping) or set(fund_inputs) != {
        "source_evidence",
        "artifact",
        "source_receipts",
        "authority_envelope",
    }:
        raise ValueError("formal fund-flow evidence closure is incomplete")
    source_evidence = fund_inputs["source_evidence"]
    if not isinstance(source_evidence, Mapping) or set(source_evidence) != set(symbols):
        raise ValueError("formal fund-flow source evidence differs exact symbols")
    rebuilt = build_fund_flow_authority_inputs(
        [source_evidence[symbol] for symbol in symbols],
        expected_symbols=symbols,
        calendar_authority=calendar,
        provider_manifest_sha256=manifest,
        asof=asof,
        decision_cutoff=decision_cutoff,
    )
    if any(_canonical(fund_inputs[key]) != _canonical(rebuilt[key]) for key in rebuilt):
        raise ValueError("formal fund-flow signed source closure drifted")
    artifact = fund_inputs["artifact"]
    if not isinstance(artifact, Mapping) or not isinstance(artifact.get("panel"), list):
        raise ValueError("formal fund-flow artifact is invalid")
    authority = admit_signed_component_authority(
        component="fund_flow",
        artifact_value=artifact,
        authority_envelope=fund_inputs["authority_envelope"],
        expected_panel=artifact["panel"],
        bound_source_receipts=fund_inputs["source_receipts"],
        registry=registry,
        decision_cutoff_by_panel={
            entry: admission_cutoff for entry in artifact["panel"]
        },
    )
    envelope_payload = fund_inputs["authority_envelope"]["payload"]
    final_close = _timestamp(
        calendar.payload_by_panel[f"{symbols[0]}@{asof}"]["session_close_at"],
        "session_close_at",
    )
    cutoff, _ = _decision_cutoff(decision_cutoff)
    for field in ("effective_at", "available_at"):
        value = _timestamp(envelope_payload[field], f"fund-flow envelope {field}")
        if not final_close <= value < cutoff:
            raise ValueError("fund-flow envelope is outside finality window")

    rows_by_symbol: dict[str, tuple[FundFlowRow, ...]] = {}
    for symbol in symbols:
        rows_by_symbol[symbol] = tuple(
            FundFlowRow(
                date=session,
                main_net_flow=float(
                    authority.payload_by_panel[f"{symbol}@{session}"]["main_net_flow"]
                ),
            )
            for session in sessions
        )
    return VerifiedFundFlowSupplement(
        symbols=symbols,
        sessions=sessions,
        rows_by_symbol=MappingProxyType(rows_by_symbol),
        bindings=FundFlowBindings(
            registry_sha256=registry.registry_sha256,
            provider_manifest_sha256=manifest,
            decision_cutoff=decision_cutoff,
            calendar_artifact_sha256=calendar.artifact.identifier,
            calendar_signature_sha256=calendar.signature_id,
            fund_flow_artifact_sha256=authority.artifact.identifier,
            fund_flow_signature_sha256=authority.signature_id,
            source_receipt_ids=authority.source_receipt_ids,
        ),
    )


__all__ = [
    "FORMAL_FUND_FLOW_SCHEMA",
    "FUND_FLOW_ARTIFACT_SCHEMA",
    "FUND_FLOW_CAPTURE_SCHEMA",
    "FundFlowBindings",
    "FundFlowRow",
    "VerifiedFundFlowSupplement",
    "build_fund_flow_authority_inputs",
    "validate_fund_flow_record",
    "verify_formal_fund_flow",
]
