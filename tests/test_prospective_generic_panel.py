from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

import stockdata.collector_continuity as continuity
import stockdata.future_panel_registration as registration_module
from stockdata.cli import main
from stockdata.collector_continuity import CollectorContinuityError
from stockdata.future_panel_registration import (
    FuturePanelRegistrationError,
    PROSPECTIVE_PANEL_MODE,
    PROSPECTIVE_REGISTRATION_SCHEMA,
)
from stockdata.provider_export import export_verified_provider_data_manifest
from prospective_panel_fixture import (
    build_completed_prospective_panel,
    deny_external_io,
)
from test_collector_attempt_protocol import _prepared
from test_component_availability import (
    _artifact as availability_artifact,
    _sha as availability_sha,
    _verify as verify_availability,
)
from test_provider_authority_admission import _canonical, _write_json


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    deny_external_io(monkeypatch)


def _registration_command(
    *, output: Path, database: Path, panel_file: Path, registration: dict[str, object]
) -> list[str]:
    files = registration["prerequisite_files"]
    assert isinstance(files, dict)
    command = [
        "future-panel-register",
        "--output",
        str(output),
        "--database",
        str(database),
        "--panel-file",
        str(panel_file),
        "--calendar-file",
        str(files["trading_calendar"]),
        "--calendar-authority",
        str(files["trading_calendar_authority"]),
        "--market-rules-file",
        str(files["market_rules"]),
        "--market-rules-authority",
        str(files["market_rules_authority"]),
        "--panel-mode",
        PROSPECTIVE_PANEL_MODE,
    ]
    for receipt in files["source_receipts"]:
        command.extend(("--source-receipt", str(receipt)))
    return command


def _materialize_command(output: Path, inputs: dict[str, object]) -> list[str]:
    command = [
        "rqgm-provider-materialize",
        "--output-dir",
        str(output),
        "--database",
        str(inputs["database_file"]),
        "--registration-file",
        str(inputs["registration_file"]),
        "--snapshot-staging-directory",
        str(inputs["snapshot_staging_directory"]),
        "--panel-file",
        str(inputs["panel_file"]),
        "--execution-adjustment-file",
        str(inputs["execution_adjustment_file"]),
        "--signal-adjustment-file",
        str(inputs["signal_adjustment_file"]),
        "--source",
        str(inputs["source"]),
    ]
    for receipt in inputs["source_receipt_files"]:
        command.extend(("--source-receipt", str(receipt)))
    for component, path in inputs["component_files"].items():
        command.extend(("--component-file", f"{component}={path}"))
    for component, path in inputs["component_authority_files"].items():
        command.extend(("--component-authority", f"{component}={path}"))
    return command


def test_prospective_cli_collector_snapshot_and_ordinary_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = build_completed_prospective_panel(tmp_path, monkeypatch)
    inputs = case["inputs"]
    registration = case["registration"]
    panel = case["panel"]
    schedule = case["prepared"]["schedule"]
    assert registration["schema_version"] == PROSPECTIVE_REGISTRATION_SCHEMA
    assert registration["panel_mode"] == PROSPECTIVE_PANEL_MODE
    assert registration["workspace_count"] == len(panel) == 8
    assert registration["panel_sha256"] == hashlib.sha256(_canonical(panel)).hexdigest()
    assert len(schedule) == 16
    assert schedule[-1].step_ordinal == 15
    assert schedule[-1].command[-2:] == ("--panel-mode", PROSPECTIVE_PANEL_MODE)

    receipt_id = availability_sha("prospective-source-response")
    availability = availability_artifact(panel, receipt_id)
    assert verify_availability(availability, panel, [receipt_id]).panel_size == 8
    wrong_availability = deepcopy(availability)
    wrong_availability["panel"] = panel[:-1]
    with pytest.raises(ValueError, match="differs from the exact panel"):
        verify_availability(wrong_availability, panel, [receipt_id])

    capsys.readouterr()
    output = tmp_path / "ordinary-provider"
    assert main(_materialize_command(output, inputs)) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert main(
        ["rqgm-provider-export", "--bundle-file", materialized["bundle_file"]]
    ) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["ready"] is False
    assert exported["contract"]["exact_panel"]["identifier"] == hashlib.sha256(
        _canonical(panel)
    ).hexdigest()
    assert exported["readiness_report"]["request"]["panel_size"] == 8


def test_complete_prospective_authority_materializes_ready_ordinary_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = build_completed_prospective_panel(
        tmp_path, monkeypatch, provider_ready=True
    )
    capsys.readouterr()
    output = tmp_path / "ready-ordinary-provider"
    assert main(_materialize_command(output, case["inputs"])) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert main(
        ["rqgm-provider-export", "--bundle-file", materialized["bundle_file"]]
    ) == 0
    exported = json.loads(capsys.readouterr().out)
    assert materialized["receipt"]["ready"] is True
    assert exported["ready"] is True
    manifest = export_verified_provider_data_manifest(materialized["bundle_file"])
    assert manifest["schema_version"] == "stockdata-rqgm-provider-data-manifest/1"
    assert manifest["decision_authority"] is False
    assert manifest["provider_export"] == exported


@pytest.mark.parametrize(
    "panel",
    [
        ["000001.SZ@2026-09-07", "300750.SZ@2026-09-08"],
        ["000001.SZ@2026-09-04"],
    ],
)
def test_prospective_prepare_rejects_sparse_or_nonfuture_panel_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    panel: list[str],
) -> None:
    monkeypatch.setattr(
        registration_module,
        "_now",
        lambda: datetime.fromisoformat("2026-09-04T12:00:00+08:00"),
    )
    panel_file = _write_json(tmp_path / "panel.json", panel)
    database = tmp_path / "future.sqlite"
    ledger = Path(continuity.default_collector_ledger_path(database))
    with pytest.raises(FuturePanelRegistrationError):
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
    assert not database.exists()
    assert not ledger.exists()


@pytest.mark.parametrize(
    ("prepare_at", "sessions"),
    [
        ("2026-09-03T12:00:00+08:00", ("2026-09-04",)),
        ("2026-09-04T12:00:00+08:00", ("2026-09-05",)),
    ],
)
def test_prospective_registration_rejects_nonfuture_or_weekend_session_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare_at: str,
    sessions: tuple[str, ...],
) -> None:
    signed = build_completed_prospective_panel(tmp_path / "authority", monkeypatch)
    panel = [f"000001.SZ@{session}" for session in sessions]
    panel_file = _write_json(tmp_path / "invalid-panel.json", panel)
    database = tmp_path / "invalid.sqlite"
    monkeypatch.setattr(
        registration_module, "_now", lambda: datetime.fromisoformat(prepare_at)
    )
    assert main(
        [
            "future-panel-prepare",
            "--database",
            str(database),
            "--panel-file",
            str(panel_file),
            "--panel-mode",
            PROSPECTIVE_PANEL_MODE,
        ]
    ) == 0
    monkeypatch.setattr(
        registration_module,
        "_now",
        lambda: datetime.fromisoformat("2026-09-04T12:00:00+08:00"),
    )
    ledger = Path(continuity.default_collector_ledger_path(database))
    database_before = database.read_bytes()
    ledger_before = ledger.read_bytes()
    output = tmp_path / "invalid-registration.json"
    with pytest.raises(FuturePanelRegistrationError, match="future trading weekday"):
        main(
            _registration_command(
                output=output,
                database=database,
                panel_file=panel_file,
                registration=signed["registration"],
            )
        )
    assert not output.exists()
    assert database.read_bytes() == database_before
    assert ledger.read_bytes() == ledger_before


def test_prospective_registration_requires_enrolled_signed_authority_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production_registry_loader = registration_module.load_provider_trust_registry
    signed = build_completed_prospective_panel(tmp_path / "authority", monkeypatch)
    panel_file = signed["inputs"]["panel_file"]
    database = tmp_path / "unenrolled.sqlite"
    assert main(
        [
            "future-panel-prepare",
            "--database",
            str(database),
            "--panel-file",
            str(panel_file),
            "--panel-mode",
            PROSPECTIVE_PANEL_MODE,
        ]
    ) == 0
    monkeypatch.setattr(
        registration_module, "load_provider_trust_registry", production_registry_loader
    )
    production_registry = production_registry_loader()
    assert not production_registry._trust_roots
    assert not production_registry._signers
    ledger = Path(continuity.default_collector_ledger_path(database))
    database_before = database.read_bytes()
    ledger_before = ledger.read_bytes()
    output = tmp_path / "unenrolled-registration.json"
    with pytest.raises(
        FuturePanelRegistrationError, match="different trust registry"
    ):
        main(
            _registration_command(
                output=output,
                database=database,
                panel_file=panel_file,
                registration=signed["registration"],
            )
        )
    assert not output.exists()
    assert database.read_bytes() == database_before
    assert ledger.read_bytes() == ledger_before


def test_registration_schema_and_ledger_mode_cannot_be_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    symbols = (
        "000001.SZ",
        "000002.SZ",
        "000063.SZ",
        "000100.SZ",
        "000157.SZ",
        "000166.SZ",
        "000333.SZ",
        "000568.SZ",
        "000725.SZ",
        "000858.SZ",
        "002415.SZ",
        "300750.SZ",
    )
    sessions = ("2026-09-07", "2026-09-08", "2026-09-09")
    prospective = build_completed_prospective_panel(
        tmp_path / "prospective",
        monkeypatch,
        symbols=symbols,
        sessions=sessions,
    )
    assert prospective["registration"]["workspace_count"] == 36
    authority = continuity._decode_registered_schedule_authority(
        prospective["inputs"]["registration_file"].read_bytes()
    )
    assert authority["panel_mode"] == PROSPECTIVE_PANEL_MODE

    registration = deepcopy(prospective["registration"])
    registration.pop("panel_mode")
    with pytest.raises(CollectorContinuityError, match="fields are not exact"):
        continuity._decode_registered_schedule_authority(_canonical(registration))
    for field, value in (
        ("workspace_count", 35),
        ("panel_sha256", "0" * 64),
        ("authority_mode", "trusted_local_mechanical"),
    ):
        drifted = deepcopy(prospective["registration"])
        drifted[field] = value
        with pytest.raises(CollectorContinuityError):
            continuity._decode_registered_schedule_authority(_canonical(drifted))

    prospective_ledger = list(
        continuity.parse_collector_ledger(
            prospective["prepared"]["ledger"].read_bytes()
        )
    )
    prospective_ledger[1] = deepcopy(prospective_ledger[1])
    prospective_ledger[1]["event"] = deepcopy(prospective_ledger[1]["event"])
    prospective_ledger[1]["event"].pop("panel_mode")
    with pytest.raises(CollectorContinuityError, match="binding drifted"):
        continuity._validate_registered_schedule_ledger(
            authority, prospective_ledger
        )

    downcast = deepcopy(prospective["registration"])
    downcast["schema_version"] = "rqgm-forward-panel-registration/5"
    downcast.pop("panel_mode")
    downcast["authority_mode"] = "trusted_local_mechanical"
    downcast_authority = continuity._decode_registered_schedule_authority(
        _canonical(downcast)
    )
    with pytest.raises(CollectorContinuityError, match="binding drifted"):
        continuity._validate_registered_schedule_ledger(
            downcast_authority,
            continuity.parse_collector_ledger(
                prospective["prepared"]["ledger"].read_bytes()
            ),
        )

    panel_file = prospective["inputs"]["panel_file"]
    generic_database = tmp_path / "generic-prepared.sqlite"
    assert main(
        [
            "future-panel-prepare",
            "--database",
            str(generic_database),
            "--panel-file",
            str(panel_file),
            "--panel-mode",
            PROSPECTIVE_PANEL_MODE,
        ]
    ) == 0
    generic_ledger = Path(continuity.default_collector_ledger_path(generic_database))
    generic_before = generic_ledger.read_bytes()
    legacy_register = _registration_command(
        output=tmp_path / "legacy-from-generic.json",
        database=generic_database,
        panel_file=panel_file,
        registration=prospective["registration"],
    )
    panel_mode_index = legacy_register.index("--panel-mode")
    del legacy_register[panel_mode_index : panel_mode_index + 2]
    with pytest.raises(FuturePanelRegistrationError, match="cohort identity"):
        main(legacy_register)
    assert not (tmp_path / "legacy-from-generic.json").exists()
    assert generic_ledger.read_bytes() == generic_before

    legacy_database = tmp_path / "legacy-prepared.sqlite"
    assert main(
        [
            "future-panel-prepare",
            "--database",
            str(legacy_database),
            "--panel-file",
            str(panel_file),
        ]
    ) == 0
    legacy_prepared_ledger = Path(
        continuity.default_collector_ledger_path(legacy_database)
    )
    legacy_before = legacy_prepared_ledger.read_bytes()
    with pytest.raises(FuturePanelRegistrationError, match="cohort identity"):
        main(
            _registration_command(
                output=tmp_path / "generic-from-legacy.json",
                database=legacy_database,
                panel_file=panel_file,
                registration=prospective["registration"],
            )
        )
    assert not (tmp_path / "generic-from-legacy.json").exists()
    assert legacy_prepared_ledger.read_bytes() == legacy_before

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy = _prepared(legacy_root, monkeypatch)
    legacy_raw = legacy["registration"].read_bytes()
    authority = continuity._decode_registered_schedule_authority(legacy_raw)
    ledger = list(continuity.parse_collector_ledger(legacy["ledger"].read_bytes()))
    ledger[1] = deepcopy(ledger[1])
    ledger[1]["event"] = deepcopy(ledger[1]["event"])
    ledger[1]["event"]["panel_mode"] = PROSPECTIVE_PANEL_MODE
    with pytest.raises(CollectorContinuityError, match="binding drifted"):
        continuity._validate_registered_schedule_ledger(authority, ledger)
