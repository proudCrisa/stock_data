from __future__ import annotations

import json

import pytest

from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
)
from stockdata.cli import main
from stockdata.provider_export import export_verified_provider_receipt
from stockdata.provider_materializer import (
    ProviderMaterializationError,
    materialize_provider_bundle,
)
from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "cache.sqlite"
    database.write_bytes(b"not-a-sqlite-database")
    panel = tmp_path / "panel.json"
    panel.write_bytes(_json(["000001.SZ@2026-01-02"]))
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_json({"source": "fixture", "observed_at": "2026-01-01T09:00:00+08:00"}))

    execution = tmp_path / "execution.json"
    execution.write_bytes(
        _json(
            {
                "schema_version": EXECUTION_ADJUSTMENT_SCHEMA,
                "price_role": "execution",
                "source": "fixture",
                "adjustment_mode": "raw",
                "adjustment_version": "fixture-raw-v1",
            }
        )
    )
    signal = tmp_path / "signal.json"
    signal.write_bytes(
        _json(
            {
                "schema_version": SIGNAL_ADJUSTMENT_SCHEMA,
                "price_role": "signal",
                "source": "fixture",
                "adjustment_mode": "raw",
                "adjustment_version": "fixture-raw-v1",
            }
        )
    )
    component_files = {}
    for component in REQUIRED_COMPONENTS:
        path = tmp_path / f"{component}.json"
        path.write_bytes(_json({"component": component, "source": "fixture"}))
        component_files[component] = path
    return {
        "database_file": database,
        "panel_file": panel,
        "source_receipt_files": [receipt],
        "execution_adjustment_file": execution,
        "signal_adjustment_file": signal,
        "component_files": component_files,
        "source": "fixture",
    }


def test_materializer_creates_verified_fail_closed_content_closure(tmp_path) -> None:
    result = materialize_provider_bundle(output_dir=tmp_path / "closure", **_inputs(tmp_path))

    assert result["receipt"]["ready"] is False
    assert (tmp_path / "closure" / "companion_snapshot.json").is_file()
    exported = export_verified_provider_receipt(result["bundle_file"])
    assert exported["contract"] == result["receipt"]["contract"]
    assert all(
        item["code"] == "provider_component_authority_not_attested"
        for item in exported["readiness_report"]["blockers"]
        if item["code"] == "provider_component_authority_not_attested"
    )


def test_materializer_rejects_component_drift_after_binding(tmp_path) -> None:
    result = materialize_provider_bundle(output_dir=tmp_path / "closure", **_inputs(tmp_path))
    database_id = result["receipt"]["contract"]["database"]["identifier"]
    (tmp_path / "closure" / "artifacts" / database_id).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="database artifact content has drifted"):
        export_verified_provider_receipt(result["bundle_file"])


def test_materializer_rejects_research_receipts_and_missing_components(tmp_path) -> None:
    inputs = _inputs(tmp_path)
    receipt = inputs["source_receipt_files"][0]
    receipt.write_bytes(_json({"schema_version": "stockdata-research-calendar/1"}))
    with pytest.raises(ProviderMaterializationError, match="research-only"):
        materialize_provider_bundle(output_dir=tmp_path / "research", **inputs)

    inputs = _inputs(tmp_path / "other")
    inputs["component_files"].pop("market_rules")
    with pytest.raises(ProviderMaterializationError, match="every required component"):
        materialize_provider_bundle(output_dir=tmp_path / "missing", **inputs)


def test_materializer_contract_identity_is_reproducible(tmp_path) -> None:
    inputs = _inputs(tmp_path / "inputs")
    first = materialize_provider_bundle(output_dir=tmp_path / "first", **inputs)
    second = materialize_provider_bundle(output_dir=tmp_path / "second", **inputs)

    assert (
        first["receipt"]["contract"]["contract_sha256"]
        == second["receipt"]["contract"]["contract_sha256"]
    )


def test_materializer_cli_writes_a_verified_blocked_bundle(tmp_path, capsys) -> None:
    inputs = _inputs(tmp_path / "inputs")
    command = [
        "rqgm-provider-materialize",
        "--output-dir", str(tmp_path / "closure"),
        "--database", str(inputs["database_file"]),
        "--panel-file", str(inputs["panel_file"]),
        "--source-receipt", str(inputs["source_receipt_files"][0]),
        "--execution-adjustment-file", str(inputs["execution_adjustment_file"]),
        "--signal-adjustment-file", str(inputs["signal_adjustment_file"]),
        "--source", "fixture",
    ]
    for component, path in inputs["component_files"].items():
        command.extend(("--component-file", f"{component}={path}"))

    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["receipt"]["ready"] is False
