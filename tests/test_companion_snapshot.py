from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from stockdata.companion_snapshot import (
    build_companion_snapshot,
    verify_bound_readiness,
    verify_companion_snapshot,
)
from stockdata.rqgm_provider_contract import (
    CHECKOUT_SCHEMA,
    COMPONENT_SCHEMAS,
    COMPANION_SNAPSHOT_SCHEMA,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    EXECUTION_ADJUSTMENT_SCHEMA,
    REQUIRED_COMPONENTS,
    READINESS_REPORT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    SOURCE_RECEIPT_SCHEMA,
    ProviderArtifactReference,
    build_rqgm_provider_contract,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _ref(kind: str, schema: str, value: str) -> ProviderArtifactReference:
    return ProviderArtifactReference(kind, _sha(value), schema)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _adjustment(role: str) -> dict[str, str]:
    return {
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


def _adjustment_ref(role: str) -> ProviderArtifactReference:
    raw = _canonical(_adjustment(role))
    return ProviderArtifactReference(
        f"stock-data-{role}-adjustment",
        hashlib.sha256(raw).hexdigest(),
        (
            EXECUTION_ADJUSTMENT_SCHEMA
            if role == "execution"
            else SIGNAL_ADJUSTMENT_SCHEMA
        ),
    )


def _values() -> dict[str, object]:
    return {
        "coverage_start": "2026-01-01",
        "coverage_end": "2026-01-31",
        "checkout": _ref("stock-data-checkout", CHECKOUT_SCHEMA, "checkout"),
        "database": _ref("stock-data-database", DATABASE_SCHEMA, "database"),
        "source_receipts": [
            _ref("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, "receipt-b"),
            _ref("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, "receipt-a"),
        ],
        "execution_adjustment_identity": _adjustment_ref("execution"),
        "signal_adjustment_identity": _adjustment_ref("signal"),
        "exact_panel": _ref(
            "stock-data-exact-panel", EXACT_PANEL_SCHEMA, "panel"
        ),
        "components": {
            component: _ref(
                f"stock-data-{component.replace('_', '-')}",
                COMPONENT_SCHEMAS[component],
                f"component:{component}",
            )
            for component in REQUIRED_COMPONENTS
        },
    }
def _contents(snapshot) -> dict[ProviderArtifactReference, bytes]:
    contents = {
        snapshot.checkout: b"checkout",
        snapshot.database: b"database",
        snapshot.execution_adjustment_identity: _canonical(
            _adjustment("execution")
        ),
        snapshot.signal_adjustment_identity: _canonical(_adjustment("signal")),
        snapshot.exact_panel: b"panel",
    }
    contents.update(
        {
            receipt: (
                b"receipt-a"
                if receipt.identifier == _sha("receipt-a")
                else b"receipt-b"
            )
            for receipt in snapshot.source_receipts
        }
    )
    contents.update(
        {
            reference: f"component:{component}".encode("ascii")
            for component, reference in snapshot.components
        }
    )
    return contents


def _contract_and_blocked_report(snapshot):
    components = {
        component: {
            "ready": False,
            "blockers": [{"code": f"{component}_blocked"}],
        }
        for component in REQUIRED_COMPONENTS
    }
    report = {
        "schema_version": READINESS_REPORT_SCHEMA,
        "ready": False,
        "request": {
            "database_sha256": snapshot.database.identifier,
            "execution_adjustment_sha256": (
                snapshot.execution_adjustment_identity.identifier
            ),
            "signal_adjustment_sha256": (
                snapshot.signal_adjustment_identity.identifier
            ),
            "panel_sha256": snapshot.exact_panel.identifier,
            "panel_size": 1,
            "companion_snapshot_sha256": snapshot.snapshot_sha256,
        },
        "blockers": [
            {"component": component, "code": f"{component}_blocked"}
            for component in REQUIRED_COMPONENTS
        ],
        "components": components,
    }
    readiness = ProviderArtifactReference(
        "stock-data-readiness-report",
        hashlib.sha256(_canonical(report)).hexdigest(),
        READINESS_REPORT_SCHEMA,
    )
    contract = build_rqgm_provider_contract(
        checkout=snapshot.checkout,
        database=snapshot.database,
        source_receipts=snapshot.source_receipts,
        execution_adjustment_identity=snapshot.execution_adjustment_identity,
        signal_adjustment_identity=snapshot.signal_adjustment_identity,
        exact_panel=snapshot.exact_panel,
        readiness_report=readiness,
        companion_snapshot=ProviderArtifactReference(
            "stock-data-companion-snapshot",
            snapshot.snapshot_sha256,
            COMPANION_SNAPSHOT_SCHEMA,
        ),
    )
    return contract, report


def test_companion_snapshot_binds_every_provider_authority() -> None:
    snapshot = build_companion_snapshot(**_values())
    replay = build_companion_snapshot(**_values())

    assert replay == snapshot
    assert tuple(dict(snapshot.components)) == REQUIRED_COMPONENTS
    assert [
        receipt.identifier for receipt in snapshot.source_receipts
    ] == sorted(receipt.identifier for receipt in snapshot.source_receipts)
    assert snapshot.to_dict()["snapshot_sha256"] == snapshot.snapshot_sha256


@pytest.mark.parametrize("missing", REQUIRED_COMPONENTS)
def test_companion_snapshot_requires_all_nine_components(missing: str) -> None:
    values = _values()
    values["components"].pop(missing)

    with pytest.raises(ValueError, match="component set or order"):
        build_companion_snapshot(**values)


def test_component_or_database_drift_changes_snapshot_identity() -> None:
    values = _values()
    baseline = build_companion_snapshot(**values)
    values["components"] = dict(values["components"])
    values["components"]["market_rules"] = _ref(
        "stock-data-market-rules",
        COMPONENT_SCHEMAS["market_rules"],
        "changed-rules",
    )
    changed_component = build_companion_snapshot(**values)

    values = _values()
    values["database"] = _ref(
        "stock-data-database", DATABASE_SCHEMA, "changed-database"
    )
    changed_database = build_companion_snapshot(**values)

    assert len(
        {
            baseline.snapshot_sha256,
            changed_component.snapshot_sha256,
            changed_database.snapshot_sha256,
        }
    ) == 3


def test_execution_and_signal_adjustment_roles_cannot_be_swapped() -> None:
    values = _values()
    values["execution_adjustment_identity"] = values[
        "signal_adjustment_identity"
    ]

    with pytest.raises(ValueError, match="content identity mismatch|wrong"):
        build_companion_snapshot(**values)


def test_snapshot_dataclass_rejects_post_build_tampering() -> None:
    snapshot = build_companion_snapshot(**_values())

    with pytest.raises(ValueError, match="content identity mismatch"):
        replace(snapshot, coverage_end="2026-02-01")


def test_bound_readiness_is_exposed_only_after_every_artifact_is_reread() -> None:
    snapshot = build_companion_snapshot(**_values())
    contents = _contents(snapshot)
    contract, report = _contract_and_blocked_report(snapshot)

    assert (
        verify_bound_readiness(
            report=report,
            contract=contract,
            companion_snapshot=snapshot.to_dict(),
            content_reader=contents.__getitem__,
        )
        is False
    )


def test_artifact_drift_fails_before_invalid_readiness_is_examined() -> None:
    snapshot = build_companion_snapshot(**_values())
    contents = _contents(snapshot)
    contents[snapshot.database] = b"modified-database"
    contract, _ = _contract_and_blocked_report(snapshot)

    with pytest.raises(ValueError, match="database artifact content has drifted"):
        verify_bound_readiness(
            report="not-a-report",
            contract=contract,
            companion_snapshot=snapshot,
            content_reader=contents.__getitem__,
        )


def test_component_drift_and_non_byte_reader_are_rejected() -> None:
    snapshot = build_companion_snapshot(**_values())
    contents = _contents(snapshot)
    market_rules = dict(snapshot.components)["market_rules"]
    contents[market_rules] = b"modified-market-rules"

    with pytest.raises(ValueError, match="market-rules artifact content has drifted"):
        verify_companion_snapshot(snapshot, content_reader=contents.__getitem__)
    with pytest.raises(ValueError, match="must return bytes"):
        verify_companion_snapshot(snapshot, content_reader=lambda _: "not-bytes")


def test_reader_crash_never_falls_through_to_readiness() -> None:
    snapshot = build_companion_snapshot(**_values())
    contract, _ = _contract_and_blocked_report(snapshot)

    def crashed_reader(_):
        raise OSError("simulated interrupted read")

    with pytest.raises(OSError, match="interrupted read"):
        verify_bound_readiness(
            report={"ready": True},
            contract=contract,
            companion_snapshot=snapshot,
            content_reader=crashed_reader,
        )


def test_old_companion_cannot_replay_against_changed_contract() -> None:
    old_snapshot = build_companion_snapshot(**_values())
    old_contents = _contents(old_snapshot)
    new_values = _values()
    new_values["database"] = _ref(
        "stock-data-database", DATABASE_SCHEMA, "new-database"
    )
    new_snapshot = build_companion_snapshot(**new_values)
    new_contract, new_report = _contract_and_blocked_report(new_snapshot)

    with pytest.raises(ValueError, match="different companion snapshot"):
        verify_bound_readiness(
            report=new_report,
            contract=new_contract,
            companion_snapshot=old_snapshot,
            content_reader=old_contents.__getitem__,
        )
