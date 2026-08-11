"""Read-only export of a verified stock_data contract and readiness receipt."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from stockdata.companion_snapshot import (
    build_companion_snapshot,
    verify_bound_readiness,
)
from stockdata.rqgm_provider_contract import (
    COMPANION_SNAPSHOT_SCHEMA,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    ProviderArtifactReference,
    build_rqgm_provider_contract,
)


EXPORT_SCHEMA = "stockdata-rqgm-provider-export/1"


def _read_json(path: Path, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is unreadable or invalid JSON") from exc


def _locator(value: object, field: str) -> tuple[ProviderArtifactReference, Path]:
    if not isinstance(value, Mapping) or set(value) != {"reference", "path"}:
        raise ValueError(f"{field} locator is incomplete")
    reference = ProviderArtifactReference.from_dict(value["reference"])
    path_value = value["path"]
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{field} locator path is invalid")
    candidate = Path(path_value).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{field} locator must name a regular file")
    path = candidate.resolve()
    if not path.is_file():
        raise ValueError(f"{field} locator must name a regular file")
    return reference, path


def export_verified_provider_receipt(bundle_file: str | Path) -> dict[str, object]:
    """Verify one immutable bundle and return its contract without any writes."""

    bundle_path = Path(bundle_file).expanduser().resolve()
    bundle = _read_json(bundle_path, "provider bundle")
    required = {
        "schema_version",
        "coverage_start",
        "coverage_end",
        "checkout",
        "database",
        "source_receipts",
        "execution_adjustment_identity",
        "signal_adjustment_identity",
        "exact_panel",
        "components",
        "readiness_report",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != required:
        raise ValueError("provider bundle schema is incomplete")
    if bundle["schema_version"] != EXPORT_SCHEMA:
        raise ValueError("unsupported provider export schema")
    paths: dict[ProviderArtifactReference, Path] = {}

    def bind(value: object, field: str) -> ProviderArtifactReference:
        reference, path = _locator(value, field)
        existing = paths.get(reference)
        if existing is not None and existing != path:
            raise ValueError("one artifact reference resolves to multiple files")
        paths[reference] = path
        return reference

    checkout = bind(bundle["checkout"], "checkout")
    database = bind(bundle["database"], "database")
    receipt_values = bundle["source_receipts"]
    component_values = bundle["components"]
    if not isinstance(receipt_values, list) or not isinstance(
        component_values, Mapping
    ):
        raise ValueError("provider bundle receipt or component locators are malformed")
    if set(component_values) != set(REQUIRED_COMPONENTS):
        raise ValueError("provider bundle component set is incomplete")
    source_receipts = tuple(
        bind(value, f"source_receipts[{index}]")
        for index, value in enumerate(receipt_values)
    )
    execution_adjustment = bind(
        bundle["execution_adjustment_identity"],
        "execution_adjustment_identity",
    )
    signal_adjustment = bind(
        bundle["signal_adjustment_identity"],
        "signal_adjustment_identity",
    )
    exact_panel = bind(bundle["exact_panel"], "exact_panel")
    components = {
        component: bind(component_values[component], f"components.{component}")
        for component in REQUIRED_COMPONENTS
    }
    readiness_reference, readiness_path = _locator(
        bundle["readiness_report"], "readiness_report"
    )
    if (
        readiness_reference.kind != "stock-data-readiness-report"
        or readiness_reference.schema_version != READINESS_REPORT_SCHEMA
    ):
        raise ValueError("readiness report reference has wrong kind or schema")

    companion = build_companion_snapshot(
        coverage_start=bundle["coverage_start"],
        coverage_end=bundle["coverage_end"],
        checkout=checkout,
        database=database,
        source_receipts=source_receipts,
        execution_adjustment_identity=execution_adjustment,
        signal_adjustment_identity=signal_adjustment,
        exact_panel=exact_panel,
        components=components,
    )
    contract = build_rqgm_provider_contract(
        checkout=checkout,
        database=database,
        source_receipts=source_receipts,
        execution_adjustment_identity=execution_adjustment,
        signal_adjustment_identity=signal_adjustment,
        exact_panel=exact_panel,
        readiness_report=readiness_reference,
        companion_snapshot=ProviderArtifactReference(
            "stock-data-companion-snapshot",
            companion.snapshot_sha256,
            COMPANION_SNAPSHOT_SCHEMA,
        ),
    )
    report = _read_json(readiness_path, "readiness report")

    def content_reader(reference: ProviderArtifactReference) -> bytes:
        path = paths.get(reference)
        if path is None:
            raise ValueError(f"no provider path is bound for {reference.kind}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{reference.kind} became unreadable") from exc

    ready = verify_bound_readiness(
        report=report,
        contract=contract,
        companion_snapshot=companion,
        content_reader=content_reader,
    )
    return {
        "schema_version": EXPORT_SCHEMA,
        "ready": ready,
        "contract": contract.to_dict(),
        "companion_snapshot": companion.to_dict(),
        "readiness_report": report,
    }
