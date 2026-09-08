"""Reusable offline fixture for the signed prospective-panel producer path."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_collector_phase_orchestration import _append_completed_attempt
from test_provider_authority_admission import (
    _artifact,
    _canonical,
    _envelope,
    _payload,
    _registry,
    _source_receipt,
    _write_json,
)

import stockdata.future_panel_registration as registration_module
from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    verify_adjustment_identity,
)
from stockdata.authority import AUTHORITY_COMPONENT_ROLES
from stockdata.cli import main
from stockdata.collector_continuity import (
    acquire_collector_phase_lease,
    default_collector_ledger_path,
    freeze_collector_step_schedule,
)
from stockdata.future_panel_registration import (
    PROSPECTIVE_PANEL_MODE,
    PROSPECTIVE_REGISTRATION_SCHEMA,
)
from stockdata.market_rules import _symbol_board
from stockdata.provider_intrinsic import reconstruct_intrinsic_evidence
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS, REQUIRED_COMPONENTS


def deny_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this fixture's real CLI path local even on an unexpected branch."""

    def denied(*args: object, **kwargs: object) -> None:
        pytest.fail("offline prospective fixture forbids network and child processes")

    monkeypatch.setattr(socket.socket, "__init__", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    # Patch the class initializer so already-bound Popen defaults also fail.
    monkeypatch.setattr(subprocess.Popen, "__init__", denied)
    monkeypatch.setattr(os, "system", denied)
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, denied)


def _synthetic_market_rules_artifact(
    panel: list[str], *, available_at: str | None = None, include_st_regime: bool = True
) -> tuple[dict[str, object], dict[str, object]]:
    sessions = sorted({entry.rsplit("@", 1)[1] for entry in panel})
    records: list[dict[str, object]] = []
    for entry in panel:
        symbol, day = entry.split("@", 1)
        board = _symbol_board(symbol)
        for is_st in (False, True) if include_st_regime else (False,):
            payload = _payload("market_rules", sessions[0])
            payload.update(
                {
                    "policy_id": (
                        f"synthetic-{board.lower()}-{symbol[-2:].lower()}-"
                        f"{'st' if is_st else 'nonst'}-v1"
                    ),
                    "board": board,
                    "exchange": symbol[-2:],
                    "effective_from": sessions[0],
                    "effective_until": sessions[-1],
                    "is_st": is_st,
                }
            )
            records.append(
                {
                    "panel_entry": entry,
                    "payload": payload,
                    "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                    "source_receipt_ids": [],
                    "effective_at": f"{day}T00:00:00+08:00",
                    "available_at": available_at or f"{day}T08:00:00+08:00",
                }
            )
    records.sort(key=lambda record: (record["panel_entry"], record["record_sha256"]))
    artifact = {
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
        "component": "market_rules",
        "panel": panel,
        "records": records,
    }
    receipt = _source_receipt("market_rules", artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    return artifact, receipt


def build_completed_prospective_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbols: tuple[str, ...] = ("000001.SZ", "300750.SZ"),
    sessions: tuple[str, ...] = (
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
    ),
    provider_ready: bool = False,
    registered_at: datetime | None = None,
) -> dict[str, object]:
    """Build a signed prospective registration and completed offline collector."""

    deny_external_io(monkeypatch)
    root_dir = tmp_path / "stockdata-prospective"
    root_dir.mkdir(parents=True)
    panel = sorted(f"{symbol}@{session}" for symbol in symbols for session in sessions)
    registered_at = registered_at or datetime.fromisoformat("2026-09-04T12:00:00+08:00")
    prerequisite_available_at = registered_at.replace(
        hour=8, minute=0, second=0, microsecond=0
    ).isoformat()
    monkeypatch.setattr(registration_module, "_now", lambda: registered_at)

    root_key = Ed25519PrivateKey.generate()
    signer_key = Ed25519PrivateKey.generate()
    registry = _registry(
        root_dir, root_key, signer_key, tuple(AUTHORITY_COMPONENT_ROLES)
    )
    monkeypatch.setattr(
        registration_module, "load_provider_trust_registry", lambda: registry
    )

    registration_receipt_files: list[Path] = []
    prerequisite_files: dict[str, Path] = {}
    authority_files: dict[str, Path] = {}
    for component in ("trading_calendar", "market_rules"):
        if component == "market_rules":
            artifact, _ = _synthetic_market_rules_artifact(
                panel, available_at=prerequisite_available_at
            )
        else:
            artifact = _artifact(
                component,
                panel,
                "0" * 64,
                available_at=prerequisite_available_at,
            )
        receipt = _source_receipt(component, artifact)
        receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
        for record in artifact["records"]:
            record["source_receipt_ids"] = [receipt_id]
        envelope = _envelope(
            component, artifact, receipt_id, registry, root_key, signer_key
        )
        registration_receipt_files.append(
            _write_json(root_dir / f"{component}-receipt.json", receipt)
        )
        prerequisite_files[component] = _write_json(
            root_dir / f"{component}.json", artifact
        )
        authority_files[component] = _write_json(
            root_dir / f"{component}-authority.json", envelope
        )

    panel_file = _write_json(root_dir / "panel.json", panel)
    database = root_dir / "future.sqlite"
    assert (
        main(
            [
                "future-panel-prepare",
                "--database",
                str(database),
                "--panel-file",
                str(panel_file),
                "--panel-mode",
                PROSPECTIVE_PANEL_MODE,
            ]
        )
        == 0
    )
    registration_file = root_dir / "registration.json"
    register_command = [
        "future-panel-register",
        "--output",
        str(registration_file),
        "--database",
        str(database),
        "--panel-file",
        str(panel_file),
        "--calendar-file",
        str(prerequisite_files["trading_calendar"]),
        "--calendar-authority",
        str(authority_files["trading_calendar"]),
        "--market-rules-file",
        str(prerequisite_files["market_rules"]),
        "--market-rules-authority",
        str(authority_files["market_rules"]),
        "--panel-mode",
        PROSPECTIVE_PANEL_MODE,
    ]
    for receipt_file in registration_receipt_files:
        register_command.extend(("--source-receipt", str(receipt_file)))
    assert main(register_command) == 0

    schedule = freeze_collector_step_schedule(registration_file=registration_file)
    ledger = Path(default_collector_ledger_path(database))
    if provider_ready:
        import test_verified_provider_readiness as readiness_fixture

        monkeypatch.setattr(readiness_fixture, "COLLECTOR_SYMBOLS", symbols)
        monkeypatch.setattr(readiness_fixture, "COLLECTOR_SESSIONS", sessions)
        monkeypatch.setattr(readiness_fixture, "PANEL", panel)
        monkeypatch.setattr(readiness_fixture, "DAY", sessions[0])
        monkeypatch.setattr(
            readiness_fixture,
            "NEXT_SESSION",
            sessions[1] if len(sessions) > 1 else sessions[0],
        )
        decision_cutoffs = {
            entry: readiness_fixture._decision_cutoff(entry) for entry in panel
        }
        signed_calendar_phases = {
            entry: {
                "decision_cutoff_at": readiness_fixture._decision_cutoff(entry),
                "session_close_at": readiness_fixture._session_close(entry),
                "next_session_decision_cutoff_at": (
                    readiness_fixture._next_decision_cutoff(entry)
                ),
            }
            for entry in panel
        }
        monkeypatch.setattr(readiness_fixture, "DECISION_CUTOFFS", decision_cutoffs)
        monkeypatch.setattr(
            readiness_fixture, "SIGNED_CALENDAR_PHASES", signed_calendar_phases
        )
    with acquire_collector_phase_lease(ledger) as lease:
        for spec in schedule:
            if provider_ready:
                readiness_fixture._append_provider_attempt(
                    {
                        "database": database,
                        "ledger": ledger,
                        "registration": registration_file,
                    },
                    lease,
                    spec,
                )
            else:
                _append_completed_attempt(lease, spec)

    adjustment_files: dict[str, Path] = {}
    for role, schema in (
        ("execution", EXECUTION_ADJUSTMENT_SCHEMA),
        ("signal", SIGNAL_ADJUSTMENT_SCHEMA),
    ):
        adjustment_files[role] = _write_json(
            root_dir / f"{role}-adjustment.json",
            {
                "schema_version": schema,
                "price_role": role,
                "source": "tencent",
                "adjustment_mode": "raw",
                "adjustment_version": "tencent-qt-daily-v1",
            },
        )
    provider_receipt_files: list[Path] = list(registration_receipt_files)
    provider_authority_files: dict[str, Path] = {}
    if provider_ready:
        execution = verify_adjustment_identity(
            json.loads(adjustment_files["execution"].read_bytes()),
            expected_price_role="execution",
        )
        signal = verify_adjustment_identity(
            json.loads(adjustment_files["signal"].read_bytes()),
            expected_price_role="signal",
        )
        reconstructed = reconstruct_intrinsic_evidence(
            database,
            panel=panel,
            execution_adjustment=execution,
            signal_adjustment=signal,
            decision_cutoffs=readiness_fixture.DECISION_CUTOFFS,
        )
        components = {
            name: dict(value) for name, value in reconstructed.components.items()
        }
        receipt_values = {
            receipt_id: dict(value)
            for receipt_id, value in reconstructed.source_receipts.items()
        }
        authorities = {}
        for component in readiness_fixture.SIGNED:
            if component == "market_rules":
                artifact, receipt = _synthetic_market_rules_artifact(
                    panel, include_st_regime=False
                )
                receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
                envelope = _envelope(
                    component, artifact, receipt_id, registry, root_key, signer_key
                )
            else:
                artifact, receipt, envelope = readiness_fixture._external_authority(
                    component,
                    complete_calendar=True,
                    registry=registry,
                    root=root_key,
                    signer=signer_key,
                )
            components[component] = artifact
            receipt_values[readiness_fixture._sha256(receipt)] = receipt
            authorities[component] = envelope
        components["availability_records"] = readiness_fixture._availability(
            components, sorted(receipt_values)
        )
        provider_receipt_files = [
            _write_json(root_dir / f"provider-receipt-{receipt_id}.json", receipt)
            for receipt_id, receipt in sorted(receipt_values.items())
        ]
        component_files = {
            component: _write_json(
                root_dir / f"provider-{component}.json", components[component]
            )
            for component in REQUIRED_COMPONENTS
        }
        provider_authority_files = {
            component: _write_json(
                root_dir / f"provider-{component}-authority.json",
                authorities[component],
            )
            for component in readiness_fixture.SIGNED
        }
        readiness_fixture._patch_test_trust(monkeypatch, registry)
    else:
        component_files = {
            component: _write_json(
                root_dir / f"provider-{component}.json",
                {"component": component, "source": "offline-fixture"},
            )
            for component in REQUIRED_COMPONENTS
        }
    staging = root_dir / "snapshot-staging"
    staging.mkdir()
    registration = json.loads(registration_file.read_bytes())
    assert registration["schema_version"] == PROSPECTIVE_REGISTRATION_SCHEMA
    return {
        "inputs": {
            "database_file": database,
            "registration_file": registration_file,
            "snapshot_staging_directory": staging,
            "panel_file": panel_file,
            "source_receipt_files": provider_receipt_files,
            "execution_adjustment_file": adjustment_files["execution"],
            "signal_adjustment_file": adjustment_files["signal"],
            "component_files": component_files,
            "component_authority_files": provider_authority_files,
            "source": "tencent",
        },
        "prepared": {
            "database": database,
            "ledger": ledger,
            "registration": registration_file,
            "schedule": schedule,
        },
        "panel": panel,
        "registration": registration,
        "signed_prerequisite_registry": registry,
    }
