from __future__ import annotations

import hashlib
import json

import pytest

from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
)
from stockdata.companion_snapshot import build_companion_snapshot
from stockdata.provider_export import EXPORT_SCHEMA, export_verified_provider_receipt
from stockdata.rqgm_provider_contract import (
    CHECKOUT_SCHEMA,
    COMPONENT_SCHEMAS,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    SOURCE_RECEIPT_SCHEMA,
    ProviderArtifactReference,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _write_ref(tmp_path, name, kind, schema, raw):
    path = tmp_path / name
    path.write_bytes(raw)
    reference = ProviderArtifactReference(
        kind, hashlib.sha256(raw).hexdigest(), schema
    )
    return reference, path


def _locator(reference, path):
    return {"reference": reference.to_dict(), "path": str(path)}


def _adjustment(role: str) -> bytes:
    return _canonical(
        {
            "schema_version": (
                EXECUTION_ADJUSTMENT_SCHEMA
                if role == "execution"
                else SIGNAL_ADJUSTMENT_SCHEMA
            ),
            "price_role": role,
            "source": "baostock",
            "adjustment_mode": "raw",
            "adjustment_version": "baostock-raw-v1",
        }
    )


def _bundle(tmp_path):
    checkout, checkout_path = _write_ref(
        tmp_path, "checkout.bin", "stock-data-checkout", CHECKOUT_SCHEMA, b"checkout"
    )
    database, database_path = _write_ref(
        tmp_path, "database.bin", "stock-data-database", DATABASE_SCHEMA, b"database"
    )
    receipt, receipt_path = _write_ref(
        tmp_path,
        "receipt.json",
        "stock-data-source-receipt",
        SOURCE_RECEIPT_SCHEMA,
        b"source-response",
    )
    execution_adjustment, execution_path = _write_ref(
        tmp_path,
        "execution-adjustment.json",
        "stock-data-execution-adjustment",
        EXECUTION_ADJUSTMENT_SCHEMA,
        _adjustment("execution"),
    )
    signal_adjustment, signal_path = _write_ref(
        tmp_path,
        "signal-adjustment.json",
        "stock-data-signal-adjustment",
        SIGNAL_ADJUSTMENT_SCHEMA,
        _adjustment("signal"),
    )
    panel_raw = _canonical(["000001.SZ@2026-01-02"])
    panel, panel_path = _write_ref(
        tmp_path,
        "panel.json",
        "stock-data-exact-panel",
        EXACT_PANEL_SCHEMA,
        panel_raw,
    )
    component_refs = {}
    component_locators = {}
    for component in REQUIRED_COMPONENTS:
        reference, path = _write_ref(
            tmp_path,
            f"{component}.json",
            f"stock-data-{component.replace('_', '-')}",
            COMPONENT_SCHEMAS[component],
            f"blocked:{component}".encode("ascii"),
        )
        component_refs[component] = reference
        component_locators[component] = _locator(reference, path)
    companion = build_companion_snapshot(
        coverage_start="2026-01-02",
        coverage_end="2026-01-02",
        checkout=checkout,
        database=database,
        source_receipts=[receipt],
        execution_adjustment_identity=execution_adjustment,
        signal_adjustment_identity=signal_adjustment,
        exact_panel=panel,
        components=component_refs,
    )
    report = {
        "schema_version": READINESS_REPORT_SCHEMA,
        "ready": False,
        "request": {
            "database_sha256": database.identifier,
            "execution_adjustment_sha256": execution_adjustment.identifier,
            "signal_adjustment_sha256": signal_adjustment.identifier,
            "panel_sha256": panel.identifier,
            "panel_size": 1,
            "companion_snapshot_sha256": companion.snapshot_sha256,
        },
        "blockers": [
            {"component": component, "code": f"{component}_blocked"}
            for component in REQUIRED_COMPONENTS
        ],
        "components": {
            component: {
                "ready": False,
                "blockers": [{"code": f"{component}_blocked"}],
            }
            for component in REQUIRED_COMPONENTS
        },
    }
    report_raw = _canonical(report)
    report_path = tmp_path / "readiness.json"
    report_path.write_bytes(report_raw)
    report_reference = ProviderArtifactReference(
        "stock-data-readiness-report",
        hashlib.sha256(report_raw).hexdigest(),
        READINESS_REPORT_SCHEMA,
    )
    bundle = {
        "schema_version": EXPORT_SCHEMA,
        "coverage_start": "2026-01-02",
        "coverage_end": "2026-01-02",
        "checkout": _locator(checkout, checkout_path),
        "database": _locator(database, database_path),
        "source_receipts": [_locator(receipt, receipt_path)],
        "execution_adjustment_identity": _locator(
            execution_adjustment, execution_path
        ),
        "signal_adjustment_identity": _locator(signal_adjustment, signal_path),
        "exact_panel": _locator(panel, panel_path),
        "components": component_locators,
        "readiness_report": _locator(report_reference, report_path),
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(_canonical(bundle))
    return bundle_path, database_path


def test_read_only_export_emits_verified_blocked_receipt(tmp_path) -> None:
    bundle_path, _ = _bundle(tmp_path)

    exported = export_verified_provider_receipt(bundle_path)

    assert exported["schema_version"] == EXPORT_SCHEMA
    assert exported["ready"] is False
    assert exported["contract"]["repository_owner"] == "stock_data"
    assert exported["companion_snapshot"]["components"].keys() == set(
        REQUIRED_COMPONENTS
    )


def test_export_rejects_drift_and_never_repairs_source_files(tmp_path) -> None:
    bundle_path, database_path = _bundle(tmp_path)
    database_path.write_bytes(b"tampered-database")
    before = database_path.read_bytes()

    with pytest.raises(ValueError, match="database artifact content has drifted"):
        export_verified_provider_receipt(bundle_path)
    assert database_path.read_bytes() == before
