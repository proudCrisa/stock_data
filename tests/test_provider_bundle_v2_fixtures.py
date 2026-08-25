from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import stockdata.provider_export as provider_export
from stockdata.collector_continuity import CollectorContinuityError
from stockdata.provider_export import EXPORT_SCHEMA, export_verified_provider_receipt

from provider_bundle_v2_fixtures import (
    CASE_NAMES,
    ProviderBundleV2Case,
    build_provider_bundle_v2_cases,
)


@pytest.fixture(scope="module")
def provider_cases(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ProviderBundleV2Case]:
    patch = pytest.MonkeyPatch()
    try:
        yield dict(
            build_provider_bundle_v2_cases(
                tmp_path_factory.mktemp("provider-bundle-v2"), patch
            )
        )
    finally:
        patch.undo()


def test_provider_bundle_v2_catalog_is_frozen_and_content_addressed(provider_cases) -> None:
    assert tuple(provider_cases) == CASE_NAMES
    assert set(provider_cases) == set(CASE_NAMES)
    for case in provider_cases.values():
        for raw_path, digest in case.artifact_sha256.items():
            path = Path(raw_path)
            assert path.name == digest
            assert path.read_bytes()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("name", CASE_NAMES)
def test_provider_bundle_v2_case_contract(name: str, provider_cases, monkeypatch) -> None:
    case = provider_cases[name]
    semantic_calls = 0
    readiness_calls = 0
    original_semantic = provider_export.verify_registered_collector_materialization_snapshot
    original_readiness = provider_export.verify_bound_readiness

    def semantic(*args, **kwargs):
        nonlocal semantic_calls
        semantic_calls += 1
        return original_semantic(*args, **kwargs)

    def readiness(*args, **kwargs):
        nonlocal readiness_calls
        readiness_calls += 1
        return original_readiness(*args, **kwargs)

    monkeypatch.setattr(provider_export, "verify_registered_collector_materialization_snapshot", semantic)
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)

    before_baseline = dict(case.baseline_sha256)
    if case.expected_outcome == "reject":
        with pytest.raises(ValueError) as raised:
            export_verified_provider_receipt(case.bundle_file)
        actual_phase = "semantic" if semantic_calls else "pre-semantic"
        if name == "state-rollback":
            assert case.final_ledger_terminal_ordinal == 11
            assert case.database_hashes is not None
            checkpoint_hash, final_hash = case.database_hashes
            assert checkpoint_hash != final_hash
            assert isinstance(raised.value.__cause__, CollectorContinuityError)
            assert str(raised.value.__cause__) == case.expected_cause_reason
    else:
        exported = export_verified_provider_receipt(case.bundle_file)
        actual_phase = "exported"
        assert set(exported) == {
            "schema_version",
            "ready",
            "contract",
            "companion_snapshot",
            "readiness_report",
        }
        assert exported["schema_version"] == EXPORT_SCHEMA
        assert exported["ready"] is case.expected_ready
        assert "trading" not in exported
        assert "release" not in exported

    assert actual_phase == case.expected_phase
    assert semantic_calls == case.expected_semantic_calls
    assert readiness_calls == case.expected_readiness_calls
    assert case.baseline_sha256 == before_baseline
    assert {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in case.baseline_sha256
    } == before_baseline
