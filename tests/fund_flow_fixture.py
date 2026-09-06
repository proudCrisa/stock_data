from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)

from stockdata.authority import SIGNER_ENROLLMENT_SCHEMA, TRUST_REGISTRY_SCHEMA
from stockdata.formal_fund_flow import (
    FORMAL_FUND_FLOW_SCHEMA,
    FUND_FLOW_CAPTURE_SCHEMA,
    _decision_cutoff,
    build_fund_flow_authority_inputs,
    verify_formal_fund_flow,
)
from stockdata.provider_authority_admission import SOURCE_RECEIPT_SCHEMA
from stockdata.provider_authority_publisher import publish_authority_envelope
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(key)).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _sessions(asof: str, count: int) -> tuple[str, ...]:
    result: list[str] = []
    current = date.fromisoformat(asof)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current -= timedelta(days=1)
    return tuple(reversed(result))


def _next_weekday(day: str) -> str:
    current = date.fromisoformat(day) + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def _registry(root: Ed25519PrivateKey, signer: Ed25519PrivateKey) -> dict[str, object]:
    root_id = _key_id(root)
    signer_id = _key_id(signer)
    roles = ["fund_flow", "trading_calendar"]
    enrollment = {
        "publisher_key_id": signer_id,
        "trust_root_id": root_id,
        "public_key_base64": _b64(_public(signer)),
        "component_roles": roles,
        "valid_from": "2025-01-01T00:00:00+08:00",
        "valid_until": "2027-01-01T00:00:00+08:00",
    }
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": signer_id,
        "publisher_public_key_base64": enrollment["public_key_base64"],
        "trust_root_id": root_id,
        "component_roles": roles,
        "valid_from": enrollment["valid_from"],
        "valid_until": enrollment["valid_until"],
    }
    enrollment["authorization_signature_base64"] = _b64(
        root.sign(_canonical(authorization))
    )
    return {
        "schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "trust_roots": [
            {"trust_root_id": root_id, "public_key_base64": _b64(_public(root))}
        ],
        "signer_enrollments": [enrollment],
    }


def _calendar_inputs(
    symbols: Sequence[str],
    sessions: Sequence[str],
    *,
    observed_at: str,
    gap_after: int | None = None,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for symbol in symbols:
        for index, session in enumerate(sessions):
            following = (
                sessions[index + 1]
                if index + 1 < len(sessions)
                else _next_weekday(session)
            )
            if gap_after == index:
                following = _next_weekday(following)
            payload = {
                "decision_cutoff_at": f"{session}T09:25:00+08:00",
                "is_trading_day": True,
                "next_session_decision_cutoff_at": f"{following}T09:25:00+08:00",
                "session_close_at": f"{session}T15:00:00+08:00",
            }
            records.append(
                {
                    "panel_entry": f"{symbol}@{session}",
                    "payload": payload,
                    "record_sha256": _hash(payload),
                    "source_receipt_ids": [],
                    "effective_at": f"{session}T00:00:00+08:00",
                    "available_at": observed_at,
                }
            )
    records.sort(key=lambda record: str(record["panel_entry"]))
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": "signed-calendar-fixture",
        "observed_at": observed_at,
        "response_sha256": _hash([record["payload"] for record in records]),
        "bindings": [
            {
                "component": "trading_calendar",
                "panel_entry": record["panel_entry"],
                "record_sha256": record["record_sha256"],
            }
            for record in records
        ],
    }
    receipt_id = _hash(receipt)
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    return {
        "artifact": {
            "schema_version": COMPONENT_SCHEMAS["trading_calendar"],
            "component": "trading_calendar",
            "panel": [record["panel_entry"] for record in records],
            "records": records,
        },
        "source_receipts": {receipt_id: receipt},
    }


def _publish(
    root: Path,
    monkeypatch,
    *,
    name: str,
    component: str,
    inputs: Mapping[str, object],
    registry: Mapping[str, object],
    registry_sha256: str,
    signer: Ed25519PrivateKey,
    effective_at: str,
    available_at: str,
    decision_cutoff: str,
):
    directory = root / name
    directory.mkdir()
    registry_file = directory / "registry.json"
    artifact_file = directory / "artifact.json"
    registry_file.write_bytes(_canonical(registry))
    artifact_file.write_bytes(_canonical(inputs["artifact"]))
    receipt_files = []
    for receipt_id, receipt in inputs["source_receipts"].items():
        receipt_file = directory / f"{receipt_id}.json"
        receipt_file.write_bytes(_canonical(receipt))
        receipt_files.append(receipt_file)
    private = signer.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    monkeypatch.setenv("STOCKDATA_TEST_FUND_FLOW_KEY", _b64(private))
    kwargs = {
        "component": component,
        "registry_file": registry_file,
        "registry_sha256": registry_sha256,
        "artifact_file": artifact_file,
        "source_receipt_files": receipt_files,
        "signer_private_key_env": "STOCKDATA_TEST_FUND_FLOW_KEY",
        "output_file": directory / "authority.json",
        "effective_at": effective_at,
        "available_at": available_at,
    }
    if component == "trading_calendar":
        kwargs["current_decision_observation_cutoff"] = decision_cutoff
    else:
        kwargs["decision_cutoff_by_panel"] = {
            entry: decision_cutoff for entry in inputs["artifact"]["panel"]
        }
    return publish_authority_envelope(**kwargs)


def make_signed_fund_flow_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    symbols: Sequence[str],
    session_count: int = 80,
    values_by_symbol: Mapping[str, Sequence[int | float]] | None = None,
    asof: str = "2026-08-31",
    decision_cutoff: str | None = None,
    provider_manifest_sha256: str = "a" * 64,
) -> dict[str, object]:
    """Build, sign, and verify a portable offline fund-flow fixture."""

    external_io_attempts: list[str] = []

    def deny_external_io(name: str):
        def blocked(*args, **kwargs):
            external_io_attempts.append(name)
            raise AssertionError(f"fund-flow fixture attempted external I/O: {name}")

        return blocked

    monkeypatch.setattr(socket, "getaddrinfo", deny_external_io("socket.getaddrinfo"))
    monkeypatch.setattr(
        socket.socket, "__init__", deny_external_io("socket.socket.__init__")
    )
    monkeypatch.setattr(
        socket, "create_connection", deny_external_io("socket.create_connection")
    )
    monkeypatch.setattr(
        subprocess.Popen, "__init__", deny_external_io("subprocess.Popen.__init__")
    )
    monkeypatch.setattr(subprocess, "run", deny_external_io("subprocess.run"))
    monkeypatch.setattr(os, "system", deny_external_io("os.system"))
    monkeypatch.setattr(
        os, "posix_spawn", deny_external_io("os.posix_spawn"), raising=False
    )
    monkeypatch.setattr(
        os, "posix_spawnp", deny_external_io("os.posix_spawnp"), raising=False
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    symbols = tuple(symbols)
    decision_cutoff = decision_cutoff or f"{asof}T16:10:00+08:00"
    admission_cutoff = _decision_cutoff(decision_cutoff)[1]
    sessions = _sessions(asof, session_count)
    root = Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
    signer = Ed25519PrivateKey.from_private_bytes(bytes([12]) * 32)
    registry = _registry(root, signer)
    registry_sha256 = _hash(registry)

    calendar_inputs = _calendar_inputs(
        symbols,
        sessions,
        observed_at=f"{asof}T15:00:00+08:00",
    )
    published_calendar = _publish(
        tmp_path,
        monkeypatch,
        name="calendar",
        component="trading_calendar",
        inputs=calendar_inputs,
        registry=registry,
        registry_sha256=registry_sha256,
        signer=signer,
        effective_at=f"{asof}T15:00:00+08:00",
        available_at=f"{asof}T15:00:00+08:00",
        decision_cutoff=admission_cutoff,
    )
    calendar = {**calendar_inputs, "authority_envelope": published_calendar.envelope}

    captures = []
    for symbol_index, symbol in enumerate(symbols):
        digits, exchange = symbol.split(".")
        values = (
            list(values_by_symbol[symbol])
            if values_by_symbol is not None
            else [float(symbol_index * 100 + index) for index in range(session_count)]
        )
        if len(values) != session_count:
            raise ValueError(
                "fixture values must cover the exact declared session window"
            )
        captures.append(
            {
                "schema_version": FUND_FLOW_CAPTURE_SCHEMA,
                "source": "westock",
                "source_version": "asfund/1",
                "request": {
                    "code": f"{exchange.lower()}{digits}",
                    "start": sessions[0],
                    "end": sessions[-1],
                },
                "observed_at": f"{asof}T15:00:00+08:00",
                "response": {
                    "field": "MainNetFlow",
                    "unit": "CNY",
                    "rows": [
                        {"date": session, "MainNetFlow": value}
                        for session, value in zip(sessions, values)
                    ],
                },
            }
        )
    fund_inputs = build_fund_flow_authority_inputs(
        captures,
        expected_symbols=symbols,
        calendar_authority=published_calendar.admitted,
        provider_manifest_sha256=provider_manifest_sha256,
        asof=asof,
        decision_cutoff=decision_cutoff,
    )
    published_fund = _publish(
        tmp_path,
        monkeypatch,
        name="fund-flow",
        component="fund_flow",
        inputs=fund_inputs,
        registry=registry,
        registry_sha256=registry_sha256,
        signer=signer,
        effective_at=f"{asof}T15:00:00+08:00",
        available_at=f"{asof}T15:00:00+08:00",
        decision_cutoff=admission_cutoff,
    )
    payload = {
        "schema_version": FORMAL_FUND_FLOW_SCHEMA,
        "provider_manifest_sha256": provider_manifest_sha256,
        "asof": asof,
        "decision_cutoff": decision_cutoff,
        "symbols": list(symbols),
        "registry": registry,
        "calendar": calendar,
        "fund_flow": {**fund_inputs, "authority_envelope": published_fund.envelope},
    }
    verified = verify_formal_fund_flow(
        payload,
        expected_registry_sha256=registry_sha256,
        provider_manifest_sha256=provider_manifest_sha256,
        asof=asof,
        decision_cutoff=decision_cutoff,
        expected_symbols=symbols,
    )
    return {
        "payload": payload,
        "expected_registry_sha256": registry_sha256,
        "provider_manifest_sha256": provider_manifest_sha256,
        "asof": asof,
        "decision_cutoff": decision_cutoff,
        "expected_symbols": symbols,
        "calendar_authority": published_calendar.admitted,
        "verified": verified,
        "external_io_attempts": external_io_attempts,
    }
