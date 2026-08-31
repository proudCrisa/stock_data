from __future__ import annotations

import builtins
import hashlib
import inspect
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

import stockdata.collector_continuity as continuity
import stockdata.provider_export as provider_export
from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
)
from stockdata.companion_snapshot import build_companion_snapshot
from stockdata.provider_export import (
    EXPORT_SCHEMA,
    export_verified_provider_receipt,
    resolve_trusted_local_research_replay_inputs,
)
from stockdata.provider_materializer import materialize_provider_bundle
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


BUNDLE_SCHEMA = "stockdata-rqgm-provider-bundle/2"
BUNDLE_FIELDS = {
    "schema_version",
    "coverage_start",
    "coverage_end",
    "checkout",
    "database",
    "registration",
    "ledger_snapshot",
    "continuity_closure",
    "source_receipts",
    "execution_adjustment_identity",
    "signal_adjustment_identity",
    "exact_panel",
    "components",
    "readiness_report",
}


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
    registration, registration_path = _write_ref(
        tmp_path,
        "registration.json",
        "stock-data-forward-panel-registration",
        "rqgm-forward-panel-registration/4",
        b"registration",
    )
    ledger, ledger_path = _write_ref(
        tmp_path,
        "ledger.jsonl",
        "stock-data-forward-collector-ledger-snapshot",
        "stockdata-forward-collector-ledger-snapshot/1",
        b"ledger\n",
    )
    closure, closure_path = _write_ref(
        tmp_path,
        "continuity-closure.json",
        "stock-data-collector-continuity-closure",
        "stockdata-forward-collector-continuity-closure/1",
        b"continuity",
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
        "schema_version": BUNDLE_SCHEMA,
        "coverage_start": "2026-01-02",
        "coverage_end": "2026-01-02",
        "checkout": _locator(checkout, checkout_path),
        "database": _locator(database, database_path),
        "registration": _locator(registration, registration_path),
        "ledger_snapshot": _locator(ledger, ledger_path),
        "continuity_closure": _locator(closure, closure_path),
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


def _real_bundle(tmp_path, monkeypatch):
    from test_provider_materializer import _collector_inputs

    inputs, prepared, staging = _collector_inputs(tmp_path, monkeypatch)
    result = materialize_provider_bundle(output_dir=tmp_path / "bundle", **inputs)
    return Path(result["bundle_file"]), prepared, staging, result


def _semantic_verifier():
    value = getattr(
        continuity, "verify_registered_collector_materialization_snapshot", None
    )
    if not callable(value):
        pytest.fail(
            "missing task 4.4 API: verify_registered_collector_materialization_snapshot"
        )
    return value


def test_read_only_export_emits_verified_blocked_receipt(tmp_path, monkeypatch) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)

    exported = export_verified_provider_receipt(bundle_path)

    assert exported["schema_version"] == EXPORT_SCHEMA
    assert set(exported) == {
        "schema_version",
        "ready",
        "contract",
        "companion_snapshot",
        "readiness_report",
    }
    assert not ({"registration", "ledger_snapshot", "continuity_closure"} & set(exported))
    assert exported["ready"] is False
    assert exported["contract"]["repository_owner"] == "stock_data"
    assert exported["companion_snapshot"]["components"].keys() == set(
        REQUIRED_COMPONENTS
    )


def test_export_rejects_drift_and_never_repairs_source_files(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    database_path = Path(bundle["database"]["path"])
    database_path.chmod(0o600)
    database_path.write_bytes(b"tampered-database")
    before = database_path.read_bytes()

    with pytest.raises(ValueError, match="database artifact content has drifted"):
        export_verified_provider_receipt(bundle_path)
    assert database_path.read_bytes() == before


def _rewrite_bundle(bundle_path, mutate) -> dict[str, object]:
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    mutate(bundle)
    bundle_path.write_bytes(_canonical(bundle))
    return bundle


def _all_locators(bundle: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    return [
        ("checkout", bundle["checkout"]),
        ("database", bundle["database"]),
        ("registration", bundle["registration"]),
        ("ledger_snapshot", bundle["ledger_snapshot"]),
        ("continuity_closure", bundle["continuity_closure"]),
        ("source_receipts[0]", bundle["source_receipts"][0]),
        ("execution_adjustment_identity", bundle["execution_adjustment_identity"]),
        ("signal_adjustment_identity", bundle["signal_adjustment_identity"]),
        ("exact_panel", bundle["exact_panel"]),
        *(
            (f"components.{component}", bundle["components"][component])
            for component in REQUIRED_COMPONENTS
        ),
        ("readiness_report", bundle["readiness_report"]),
    ]


class _CloseFailure:
    def __init__(self, label: str, error: BaseException, attempts: list[str]) -> None:
        self.label = label
        self.error = error
        self.attempts = attempts

    def close(self) -> None:
        self.attempts.append(self.label)
        raise self.error


def _retained_close_failure(
    label: str,
    error: BaseException,
    attempts: list[str],
    *,
    raw: bytes = b"",
) -> object:
    return provider_export._RetainedArtifact(
        Path(f"/{label}"),
        _CloseFailure(label, error, attempts),
        raw,
        label,
    )


@pytest.mark.parametrize(
    ("mutation", "pattern"),
    [
        ("legacy-schema", r"schema|/2"),
        ("missing", r"schema|incomplete"),
        ("extra", r"schema|incomplete"),
        ("wrong-registration-kind", r"registration.*kind|kind.*registration"),
        ("wrong-registration-schema", r"registration.*schema|schema.*registration"),
        ("detached-v5-schema", r"semantic|continuity"),
        ("legacy-registration", r"registration.*schema|schema.*registration|/4"),
        ("wrong-ledger-kind", r"ledger.*kind|kind.*ledger"),
        ("wrong-ledger-schema", r"ledger.*schema|schema.*ledger"),
        ("wrong-closure-kind", r"closure.*kind|kind.*closure"),
        ("wrong-closure-schema", r"closure.*schema|schema.*closure"),
        ("wrong-hash", r"content|identifier|hash|drift"),
        ("same-reference-two-paths", r"reference|alias|multiple"),
        ("same-path-two-references", r"path|alias|multiple"),
    ],
)
def test_bundle_input_v2_rejects_wrong_or_aliased_continuity_locators(
    tmp_path, mutation, pattern
) -> None:
    bundle_path, _ = _bundle(tmp_path)

    def mutate(bundle):
        if mutation == "legacy-schema":
            bundle["schema_version"] = EXPORT_SCHEMA
        elif mutation == "missing":
            bundle.pop("continuity_closure")
        elif mutation == "extra":
            bundle["continuity"] = True
        elif mutation == "wrong-registration-kind":
            bundle["registration"]["reference"]["kind"] = "stock-data-registration"
        elif mutation == "wrong-registration-schema":
            bundle["registration"]["reference"]["schema_version"] = (
                "rqgm-forward-panel-registration/6"
            )
        elif mutation == "detached-v5-schema":
            bundle["registration"]["reference"]["schema_version"] = (
                "rqgm-forward-panel-registration/5"
            )
        elif mutation == "legacy-registration":
            bundle["registration"]["reference"]["schema_version"] = (
                "rqgm-forward-panel-registration/3"
            )
        elif mutation == "wrong-ledger-kind":
            bundle["ledger_snapshot"]["reference"]["kind"] = "stock-data-ledger"
        elif mutation == "wrong-ledger-schema":
            bundle["ledger_snapshot"]["reference"]["schema_version"] = (
                "stockdata-forward-collector-ledger-snapshot/2"
            )
        elif mutation == "wrong-closure-kind":
            bundle["continuity_closure"]["reference"]["kind"] = (
                "stock-data-continuity-closure"
            )
        elif mutation == "wrong-closure-schema":
            bundle["continuity_closure"]["reference"]["schema_version"] = (
                "stockdata-forward-collector-continuity-closure/2"
            )
        elif mutation == "wrong-hash":
            bundle["continuity_closure"]["reference"]["identifier"] = "0" * 64
        elif mutation == "same-reference-two-paths":
            bundle["ledger_snapshot"]["reference"] = deepcopy(
                bundle["registration"]["reference"]
            )
        else:
            bundle["ledger_snapshot"]["path"] = bundle["registration"]["path"]

    _rewrite_bundle(bundle_path, mutate)
    with pytest.raises(ValueError, match=pattern):
        export_verified_provider_receipt(bundle_path)


@pytest.mark.parametrize("field", ["registration", "ledger_snapshot", "continuity_closure"])
def test_bundle_input_v2_rejects_symlinked_continuity_locator(tmp_path, field) -> None:
    bundle_path, _ = _bundle(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    target = bundle[field]["path"]
    link = tmp_path / f"{field}-link"
    link.symlink_to(target)
    bundle[field]["path"] = str(link)
    bundle_path.write_bytes(_canonical(bundle))

    with pytest.raises(ValueError, match=r"regular|symlink|canonical"):
        export_verified_provider_receipt(bundle_path)


def test_bundle_input_v2_requires_canonical_absolute_locator_paths(tmp_path) -> None:
    bundle_path, _ = _bundle(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    absolute = bundle["continuity_closure"]["path"]
    bundle["continuity_closure"]["path"] = os.path.join(
        os.path.dirname(absolute), ".", os.path.basename(absolute)
    )
    bundle_path.write_bytes(_canonical(bundle))

    with pytest.raises(ValueError, match=r"canonical|path"):
        export_verified_provider_receipt(bundle_path)


def test_bundle_input_v2_exact_schema_is_frozen_without_task_4_4_semantics(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))

    assert bundle["schema_version"] == BUNDLE_SCHEMA
    assert set(bundle) == BUNDLE_FIELDS
    for field in ("registration", "ledger_snapshot", "continuity_closure"):
        assert set(bundle[field]) == {"reference", "path"}
    exported = export_verified_provider_receipt(bundle_path)
    assert exported["schema_version"] == EXPORT_SCHEMA
    assert exported["ready"] is False


def test_export_v5_rejects_components_detached_from_registration_prerequisites(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _ = _bundle(tmp_path)
    bundle = json.loads(bundle_path.read_bytes())
    registration_path = Path(bundle["registration"]["path"])
    registration = {
        "schema_version": "rqgm-forward-panel-registration/5",
        "authority_mode": "trusted_local_mechanical",
        "prerequisites": {
            "source_receipt_ids": ["a" * 64],
            "trading_calendar": {"artifact_sha256": "c" * 64},
            "market_rule_prerequisite": {"artifact_sha256": "d" * 64},
        },
    }
    registration_raw = _canonical(registration)
    registration_path.write_bytes(registration_raw)
    bundle["registration"]["reference"]["schema_version"] = (
        "rqgm-forward-panel-registration/5"
    )
    bundle["registration"]["reference"]["identifier"] = hashlib.sha256(
        registration_raw
    ).hexdigest()
    bundle_path.write_bytes(_canonical(bundle))

    # Isolate the missing registration/prerequisite cross-check from the
    # continuity and readiness checks covered by their dedicated tests.
    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        provider_export, "_require_trusted_local_negative_readiness", lambda report: None
    )
    monkeypatch.setattr(
        provider_export, "verify_bound_readiness", lambda **kwargs: False
    )

    with pytest.raises(ValueError, match="registration|prerequisite|receipt|artifact"):
        export_verified_provider_receipt(bundle_path)


def test_bundle_input_v1_is_rejected_before_any_locator_or_gate(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _ = _bundle(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    assert bundle["schema_version"] == BUNDLE_SCHEMA
    assert set(bundle) == BUNDLE_FIELDS
    bundle["schema_version"] = "stockdata-rqgm-provider-bundle/1"
    bundle_path.write_bytes(_canonical(bundle))

    calls = {"locator": 0, "semantic": 0, "readiness": 0}
    original_locator = provider_export._locator
    semantic_api = _semantic_verifier()
    readiness_api = provider_export.verify_bound_readiness

    def locator(*args, **kwargs):
        calls["locator"] += 1
        return original_locator(*args, **kwargs)

    def semantic(*args, **kwargs):
        calls["semantic"] += 1
        return semantic_api(*args, **kwargs)

    def readiness(*args, **kwargs):
        calls["readiness"] += 1
        return readiness_api(*args, **kwargs)

    monkeypatch.setattr(provider_export, "_locator", locator)
    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)

    with pytest.raises(ValueError, match=r"unsupported.*schema|schema"):
        export_verified_provider_receipt(bundle_path)

    assert calls == {"locator": 0, "semantic": 0, "readiness": 0}


def test_export_retains_every_locator_fd_through_semantic_readiness_and_recheck(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    locator_paths = {
        str(Path(locator["path"])) for _, locator in _all_locators(bundle)
    }
    semantic_api = _semantic_verifier()
    original_open = provider_export.open_nofollow_regular
    opened = []

    def tracked_open(path, *args, **kwargs):
        retained = original_open(path, *args, **kwargs)
        opened.append(retained)
        return retained

    monkeypatch.setattr(provider_export, "open_nofollow_regular", tracked_open)
    calls: list[str] = []
    retained_locators = []

    def semantic(*args, **kwargs):
        calls.append("semantic")
        by_path = {
            item.identity.canonical_path: item
            for item in opened
            if item.identity.canonical_path in locator_paths
        }
        assert set(by_path) == locator_paths
        for item in by_path.values():
            os.fstat(item.descriptor)
        retained_locators[:] = by_path.values()
        return semantic_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
        raising=False,
    )
    original_readiness = provider_export.verify_bound_readiness

    def readiness(*args, **kwargs):
        calls.append("readiness")
        assert calls == ["semantic", "readiness"]
        for item in retained_locators:
            os.fstat(item.descriptor)
        return original_readiness(*args, **kwargs)

    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)

    exported = export_verified_provider_receipt(bundle_path)

    assert exported["ready"] is False
    assert calls == ["semantic", "readiness"]
    for item in retained_locators:
        with pytest.raises(OSError):
            os.fstat(item.descriptor)


@pytest.mark.parametrize(
    "drift",
    [
        "database-replacement",
        "registration-replacement",
        "ledger-replacement",
        "closure-replacement",
        "same-inode-closure",
        "component-replacement",
    ],
)
def test_export_rejects_post_semantic_aba_or_same_inode_drift_and_closes_fd(
    tmp_path, monkeypatch, drift
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    field = {
        "database-replacement": "database",
        "registration-replacement": "registration",
        "ledger-replacement": "ledger_snapshot",
        "closure-replacement": "continuity_closure",
        "same-inode-closure": "continuity_closure",
    }.get(drift)
    locator = (
        bundle["components"]["execution_prices"]
        if field is None
        else bundle[field]
    )
    target = Path(locator["path"])
    original_raw = target.read_bytes()
    semantic_api = _semantic_verifier()
    retained_database = []
    semantic_calls = 0

    def semantic(*args, **kwargs):
        nonlocal semantic_calls
        semantic_calls += 1
        retained_database.append(args[3])
        semantic_api(*args, **kwargs)
        if drift == "same-inode-closure":
            target.chmod(0o600)
            target.write_bytes(original_raw + b"\n")
        else:
            replacement = target.with_name(f"replacement-{target.name}")
            replacement.write_bytes(original_raw)
            os.replace(replacement, target)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
        raising=False,
    )

    with pytest.raises(ValueError, match=r"drift|identity|changed|truncated"):
        export_verified_provider_receipt(bundle_path)

    assert semantic_calls == 1
    assert len(retained_database) == 1
    with pytest.raises(OSError):
        os.fstat(retained_database[0].descriptor)


def test_export_rejects_snapshot_sidecar_before_readiness_and_closes_fd(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    database_path = Path(bundle["database"]["path"])
    Path(str(database_path) + "-wal").write_bytes(b"forbidden")
    semantic_api = _semantic_verifier()
    retained = []
    calls: list[str] = []

    def semantic(*args, **kwargs):
        calls.append("semantic")
        retained.append(args[3])
        return semantic_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
        raising=False,
    )
    monkeypatch.setattr(
        provider_export,
        "verify_bound_readiness",
        lambda *args, **kwargs: calls.append("readiness")
        or pytest.fail("sidecar reached readiness"),
    )

    with pytest.raises((ValueError, continuity.CollectorContinuityError)):
        export_verified_provider_receipt(bundle_path)

    assert calls == ["semantic"]
    with pytest.raises(OSError):
        os.fstat(retained[0].descriptor)


def test_export_rejects_exact_panel_mismatched_from_valid_collector_registration_before_readiness(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    panel_locator = bundle["exact_panel"]
    panel = json.loads(Path(panel_locator["path"]).read_text(encoding="ascii"))
    mismatched_raw = _canonical(panel[:-1])
    mismatched_path = Path(panel_locator["path"]).with_name(
        hashlib.sha256(mismatched_raw).hexdigest()
    )
    mismatched_path.write_bytes(mismatched_raw)
    panel_locator["path"] = str(mismatched_path)
    panel_locator["reference"]["identifier"] = hashlib.sha256(
        mismatched_raw
    ).hexdigest()
    bundle_path.write_bytes(_canonical(bundle))
    semantic_api = _semantic_verifier()
    calls: list[str] = []

    def semantic(*args, **kwargs):
        calls.append("semantic")
        return semantic_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(
        provider_export,
        "verify_bound_readiness",
        lambda *args, **kwargs: calls.append("readiness")
        or pytest.fail("mismatched exact panel reached readiness"),
    )

    with pytest.raises(ValueError, match=r"panel|registration|continuity|semantic"):
        export_verified_provider_receipt(bundle_path)

    assert calls == ["semantic"]


@pytest.mark.parametrize("alias_index", range(1, 10 + len(REQUIRED_COMPONENTS)))
def test_bundle_rejects_every_different_path_hardlink_identity_alias(
    tmp_path, alias_index
) -> None:
    bundle_path, _ = _bundle(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="ascii"))
    locators = _all_locators(bundle)
    first_field, first = locators[alias_index - 1]
    second_field, second = locators[alias_index]
    first_path = Path(first["path"])
    second_path = Path(second["path"])
    shared_raw = first_path.read_bytes()
    second_path.unlink()
    os.link(first_path, second_path)
    second["reference"]["identifier"] = hashlib.sha256(shared_raw).hexdigest()
    bundle_path.write_bytes(_canonical(bundle))

    assert first_path != second_path
    assert (first_path.stat().st_dev, first_path.stat().st_ino) == (
        second_path.stat().st_dev,
        second_path.stat().st_ino,
    )
    with pytest.raises(ValueError, match=r"alias|identity|inode"):
        export_verified_provider_receipt(bundle_path)


def test_public_export_rejects_symlinked_final_bundle_entry(tmp_path) -> None:
    bundle_path, _ = _bundle(tmp_path)
    retained = tmp_path / "retained-bundle-bytes"
    bundle_path.rename(retained)
    bundle_path.symlink_to(retained)

    with pytest.raises(ValueError, match=r"bundle\.json|no-follow|regular|symlink"):
        export_verified_provider_receipt(bundle_path)


def test_public_export_rejects_bundle_beneath_symlinked_parent(tmp_path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    bundle_path, _ = _bundle(real_parent)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    aliased_bundle = alias_parent / "bundle.json"

    assert aliased_bundle != bundle_path
    with pytest.raises(ValueError, match=r"parent|no-follow|symlink|canonical"):
        export_verified_provider_receipt(aliased_bundle)


def test_public_export_accepts_only_final_bundle_basename(tmp_path) -> None:
    bundle_path, _ = _bundle(tmp_path)
    unpublished = tmp_path / ".bundle-deadbeef.json"
    bundle_path.rename(unpublished)

    with pytest.raises(ValueError, match=r"bundle\.json|basename|published"):
        export_verified_provider_receipt(unpublished)


def test_cleanup_only_export_attempts_every_locator_and_bundle_close_in_native_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(b"{}")
    scenarios = (
        (
            builtins.ExceptionGroup,
            (OSError("locator one"), RuntimeError("locator two"), ValueError("bundle")),
        ),
        (
            builtins.BaseExceptionGroup,
            (
                OSError("locator one"),
                KeyboardInterrupt("locator two"),
                SystemExit("bundle"),
            ),
        ),
    )

    for expected_group, failures in scenarios:
        attempts: list[str] = []
        locator_failures = (
            _retained_close_failure("locator-one", failures[0], attempts),
            _retained_close_failure("locator-two", failures[1], attempts),
        )
        outer = _retained_close_failure("bundle", failures[2], attempts, raw=b"{}")

        def verify_core(bundle, bundle_raw, retained) -> dict[str, object]:
            del bundle, bundle_raw
            retained.extend(locator_failures)
            return {"ready": False}

        monkeypatch.setattr(provider_export, "_verify_provider_bundle_core", verify_core)
        monkeypatch.setattr(provider_export, "_open_retained", lambda path, field: outer)

        with pytest.raises(expected_group) as raised:
            export_verified_provider_receipt(bundle_path)

        assert raised.value.exceptions == (failures[1], failures[0], failures[2])
        assert attempts == ["locator-two", "locator-one", "bundle"]


def test_body_validation_export_keeps_body_and_cleanup_order_and_alias_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(b"{}")
    attempts: list[str] = []
    body_error = ValueError("readiness validation failed")
    locator_failures = (
        _retained_close_failure("locator-one", OSError("locator one"), attempts),
        _retained_close_failure("locator-two", RuntimeError("locator two"), attempts),
    )
    outer = _retained_close_failure(
        "bundle", OSError("bundle"), attempts, raw=b"{}"
    )
    calls: list[str] = []

    def verify_core(bundle, bundle_raw, retained) -> dict[str, object]:
        del bundle, bundle_raw
        calls.extend(("semantic", "readiness"))
        retained.extend(locator_failures)
        raise body_error

    monkeypatch.setattr(provider_export, "_verify_provider_bundle_core", verify_core)
    monkeypatch.setattr(provider_export, "_open_retained", lambda path, field: outer)

    with pytest.raises(builtins.ExceptionGroup) as raised:
        export_verified_provider_receipt(bundle_path)

    assert calls == ["semantic", "readiness"]
    assert raised.value.exceptions == (
        body_error,
        locator_failures[1].opened.error,
        locator_failures[0].opened.error,
        outer.opened.error,
    )
    assert attempts == ["locator-two", "locator-one", "bundle"]

    fallback_attempts: list[str] = []
    fallback_locator = _retained_close_failure(
        "fallback-locator", RuntimeError("fallback locator"), fallback_attempts
    )

    def fallback_core(bundle, bundle_raw, retained) -> dict[str, object]:
        del bundle, bundle_raw
        retained.append(fallback_locator)
        raise body_error

    monkeypatch.setattr(provider_export, "_verify_provider_bundle_core", fallback_core)
    monkeypatch.setattr(provider_export, "_ExceptionGroup", None)
    monkeypatch.setattr(provider_export, "_BaseExceptionGroup", None)

    with pytest.raises(ValueError, match=r"additional cleanup failures: 1 \(RuntimeError\)") as fallback:
        provider_export._verify_provider_bundle({}, b"{}")

    assert fallback.value.__cause__ is body_error
    assert fallback_attempts == ["fallback-locator"]


def _resolver_policy_binding(bundle: dict[str, object]) -> dict[str, object]:
    market_rule_reference = bundle["components"]["market_rules"]["reference"]
    binding = {
        "research_authorization_reference": {
            "schema_version": "stockdata-test-research-authorization/1",
            "sha256": "a" * 64,
        },
        "shared_cash_policy_reference": {
            "schema_version": "rqgm-trusted-local-shared-cash-policy/1",
            "sha256": "b" * 64,
        },
        "market_rule_cost_policy_binding": {
            "schema_version": "stockdata-market-rule-cost-policy-binding/1",
            "policy_reference": {
                "schema_version": "stockdata-test-market-rule-policy/1",
                "sha256": "c" * 64,
            },
            "market_rule_artifact_reference": deepcopy(market_rule_reference),
        },
        "risk_policy_reference": {
            "schema_version": "rqgm-trusted-local-risk-policy/1",
            "sha256": "d" * 64,
        },
    }
    cost_binding = binding["market_rule_cost_policy_binding"]
    cost_binding["sha256"] = hashlib.sha256(_canonical(cost_binding)).hexdigest()
    return binding


def test_research_input_resolver_has_exact_keyword_only_api() -> None:
    parameters = inspect.signature(
        resolve_trusted_local_research_replay_inputs
    ).parameters

    assert list(parameters) == ["bundle_file", "replay_policy_binding"]
    assert parameters["bundle_file"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["bundle_file"].default is inspect.Parameter.empty
    assert parameters["replay_policy_binding"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["replay_policy_binding"].default is inspect.Parameter.empty
    assert not {
        "candidate_export",
        "current_db",
        "database",
        "latest",
        "result",
        "completeness",
        "readiness",
        "authority",
        "cache",
        "callback",
        "signature",
    } & set(parameters)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle: bundle["exact_panel"].update(
            {"reference": {**bundle["exact_panel"]["reference"], "identifier": "0" * 64}}
        ),
        lambda bundle: bundle["ledger_snapshot"].update(
            {"reference": {**bundle["ledger_snapshot"]["reference"], "identifier": "1" * 64}}
        ),
        lambda bundle: bundle["components"]["market_rules"].update(
            {"reference": {**bundle["components"]["market_rules"]["reference"], "identifier": "2" * 64}}
        ),
        lambda bundle: bundle.update({"readiness_report": {"ready": True}}),
    ],
)
def test_research_input_resolver_rejects_bundle_drift_without_partial_output(
    tmp_path, monkeypatch, mutation
) -> None:
    bundle_path, _, _, _ = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_bytes())
    policy_binding = _resolver_policy_binding(bundle)
    mutation(bundle)
    bundle_path.write_bytes(_canonical(bundle))
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            bundle_path,
            replay_policy_binding=policy_binding,
        )

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "argument_name, argument_value",
    [
        ("candidate_export", {"schema_version": "stockdata-rqgm-research-replay-export/1"}),
        ("current_db", "/tmp/current.sqlite"),
        ("latest", True),
        ("result", {"profit": 1}),
        ("completeness", True),
        ("readiness", True),
        ("authority", True),
    ],
)
def test_research_input_resolver_does_not_expose_forbidden_authority_inputs(
    argument_name: str, argument_value: object
) -> None:
    del argument_value
    parameters = inspect.signature(
        resolve_trusted_local_research_replay_inputs
    ).parameters
    assert argument_name not in parameters


def test_research_input_resolver_rejects_noncanonical_or_regular_provider_inputs(
    tmp_path, monkeypatch
) -> None:
    bundle_path, _, _, result = _real_bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_bytes())
    policy_binding = _resolver_policy_binding(bundle)
    regular_export = result["receipt"]

    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            str(bundle_path.relative_to(tmp_path)),
            replay_policy_binding=policy_binding,
        )
    assert regular_export["ready"] is False
