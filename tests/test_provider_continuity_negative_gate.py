from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import stockdata.collector_continuity as continuity
import stockdata.companion_snapshot as companion_snapshot
import stockdata.provider_export as provider_export
import stockdata.provider_materializer as provider_materializer
from stockdata.provider_export import export_verified_provider_receipt
from stockdata.provider_materializer import ProviderMaterializationError
from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS
from test_verified_provider_readiness import (
    SIGNED,
    _canonical,
    _fixture,
    _materialize,
)


INTRINSIC = frozenset(
    {"execution_prices", "signal_prices", "decision_context"}
)


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="ascii"))


def _bundle_report(bundle_file: Path) -> dict[str, object]:
    bundle = _read_json(bundle_file)
    return _read_json(Path(bundle["readiness_report"]["path"]))


def _capture_candidate_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    original = provider_materializer._export_verified_provider_receipt

    def capture(bundle_file: str | Path) -> dict[str, object]:
        reports.append(deepcopy(_bundle_report(Path(bundle_file))))
        return original(bundle_file)

    monkeypatch.setattr(
        provider_materializer,
        "_export_verified_provider_receipt",
        capture,
    )
    return reports


def _assert_no_continuity_authority(value: object) -> None:
    raw = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).lower()
    assert "continuity" not in raw
    assert '"path":' not in raw


def _blocked_inputs(fixture, blocked_component: str) -> dict[str, Path]:
    if blocked_component in SIGNED:
        return {
            component: path
            for component, path in fixture.authority_files.items()
            if component != blocked_component
        }
    if blocked_component in INTRINSIC:
        path = fixture.component_files[blocked_component]
        path.write_bytes(path.read_bytes() + b" ")
    elif blocked_component == "availability_records":
        path = fixture.component_files[blocked_component]
        value = _read_json(path)
        value["records"].pop()
        path.write_bytes(_canonical(value))
    return dict(fixture.authority_files)


def _eligible_invalid_authority_inputs(
    fixture,
    component: str,
    mutation: str,
) -> dict[str, Path]:
    dependencies = {
        "trading_calendar": (),
        "universe": ("trading_calendar",),
        "market_rules": ("trading_calendar", "instrument_status"),
    }
    authority_files = {
        dependency: fixture.authority_files[dependency]
        for dependency in (*dependencies[component], component)
    }
    path = authority_files[component]
    if mutation == "json":
        path.write_bytes(b"{")
    else:
        envelope = _read_json(path)
        envelope["signature_base64"] = base64.b64encode(b"0" * 64).decode(
            "ascii"
        )
        path.write_bytes(_canonical(envelope))
    return authority_files


@pytest.mark.parametrize("blocked_component", REQUIRED_COMPONENTS)
def test_valid_continuity_preserves_each_component_negative_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_component: str,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    authority_files = _blocked_inputs(fixture, blocked_component)
    candidate_reports = _capture_candidate_reports(monkeypatch)
    semantic_api = (
        provider_export.verify_registered_collector_materialization_snapshot
    )
    materializer_admission_api = (
        provider_materializer.admit_signed_component_authority
    )
    semantic_calls = 0
    materializer_admissions = 0

    def semantic(*args, **kwargs) -> None:
        nonlocal semantic_calls
        semantic_calls += 1
        assert semantic_api(*args, **kwargs) is None

    def materializer_admission(*args, **kwargs):
        nonlocal materializer_admissions
        materializer_admissions += 1
        return materializer_admission_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(
        provider_materializer,
        "admit_signed_component_authority",
        materializer_admission,
    )

    materialized = _materialize(
        fixture,
        monkeypatch,
        name=f"blocked-{blocked_component}",
        authority_files=authority_files,
    )
    exported = export_verified_provider_receipt(Path(materialized["bundle_file"]))

    assert semantic_calls == 2
    assert len(candidate_reports) == 1
    candidate = candidate_reports[0]
    report = materialized["receipt"]["readiness_report"]
    assert candidate == report == exported["readiness_report"]
    assert exported == materialized["receipt"]
    assert set(report["components"]) == set(REQUIRED_COMPONENTS)
    if blocked_component == "trading_calendar":
        assert materializer_admissions == 0
        assert semantic_calls == 2
        for component in SIGNED:
            evidence = report["components"][component]
            assert evidence["ready"] is False
            assert "provider_component_authority_not_attested" in {
                blocker["code"] for blocker in evidence["blockers"]
            }
        for component in INTRINSIC:
            evidence = report["components"][component]
            assert evidence["ready"] is False
            assert "signed_calendar_required_for_intrinsic_reconstruction" in {
                blocker["code"] for blocker in evidence["blockers"]
            }
        availability = report["components"]["availability_records"]
        assert availability["ready"] is False
        assert "availability_depends_on_unready_component" in {
            blocker["code"] for blocker in availability["blockers"]
        }
        assert report["ready"] is False
    assert report["components"][blocked_component]["ready"] is False
    assert report["components"][blocked_component]["blockers"]
    expected_code = (
        "provider_component_authority_not_attested"
        if blocked_component in SIGNED
        else "intrinsic_component_byte_mismatch"
        if blocked_component in INTRINSIC
        else "availability_verification_failed"
    )
    assert expected_code in {
        blocker["code"]
        for blocker in report["components"][blocked_component]["blockers"]
    }
    component_ready = {
        component: report["components"][component]["ready"]
        for component in REQUIRED_COMPONENTS
    }
    assert report["ready"] is all(component_ready.values())
    assert report["ready"] is False
    for component, ready in component_ready.items():
        evidence = report["components"][component]
        _assert_no_continuity_authority(evidence)
        if ready:
            assert evidence["blockers"] == []
            continue
        for blocker in evidence["blockers"]:
            assert {**blocker, "component": component} in report["blockers"]
    if any(
        not component_ready[component]
        for component in REQUIRED_COMPONENTS
        if component != "availability_records"
    ):
        assert component_ready["availability_records"] is False


def test_complete_nine_component_continuity_is_reverified_once_per_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    semantic_api = (
        provider_export.verify_registered_collector_materialization_snapshot
    )
    materializer_admission_api = (
        provider_materializer.admit_signed_component_authority
    )
    readmission_api = companion_snapshot.admit_signed_component_authority
    semantic_calls = 0
    materializer_admissions = 0
    readmissions = 0

    def semantic(*args, **kwargs) -> None:
        nonlocal semantic_calls
        semantic_calls += 1
        assert semantic_api(*args, **kwargs) is None

    def materializer_admission(*args, **kwargs):
        nonlocal materializer_admissions
        materializer_admissions += 1
        return materializer_admission_api(*args, **kwargs)

    def readmission(*args, **kwargs):
        nonlocal readmissions
        readmissions += 1
        return readmission_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(
        provider_materializer,
        "admit_signed_component_authority",
        materializer_admission,
    )
    monkeypatch.setattr(
        companion_snapshot,
        "admit_signed_component_authority",
        readmission,
    )

    materialized = _materialize(
        fixture,
        monkeypatch,
        name="complete-nine-components",
    )
    assert materializer_admissions == 5
    assert semantic_calls == 1
    assert readmissions == 5
    assert materialized["receipt"]["ready"] is True
    _assert_no_continuity_authority(materialized["receipt"])

    previous = materialized["receipt"]
    for _ in range(2):
        before_semantic = semantic_calls
        before_readmission = readmissions
        exported = export_verified_provider_receipt(
            Path(materialized["bundle_file"])
        )
        assert semantic_calls - before_semantic == 1
        assert readmissions - before_readmission == 5
        assert exported == previous
        _assert_no_continuity_authority(exported["readiness_report"])
        _assert_no_continuity_authority(exported["contract"])
        _assert_no_continuity_authority(exported["companion_snapshot"])
        _assert_no_continuity_authority(exported)
        previous = exported


def test_ready_report_with_semantically_broken_continuity_never_reaches_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    materialized = _materialize(
        fixture,
        monkeypatch,
        name="broken-continuity",
    )
    bundle_file = Path(materialized["bundle_file"])
    bundle = _read_json(bundle_file)
    closure_locator = bundle["continuity_closure"]
    closure = _read_json(Path(closure_locator["path"]))
    closure["registration_sha256"] = "0" * 64
    closure_raw = _canonical(closure)
    changed_path = Path(closure_locator["path"]).with_name(
        hashlib.sha256(closure_raw).hexdigest()
    )
    changed_path.write_bytes(closure_raw)
    closure_locator["path"] = str(changed_path)
    closure_locator["reference"]["identifier"] = hashlib.sha256(
        closure_raw
    ).hexdigest()
    bundle_file.write_bytes(_canonical(bundle))
    assert changed_path.read_bytes() == _canonical(_read_json(changed_path))
    assert closure_locator["reference"]["identifier"] == hashlib.sha256(
        changed_path.read_bytes()
    ).hexdigest()
    disk_report = _bundle_report(bundle_file)
    assert disk_report["ready"] is True
    assert set(disk_report["components"]) == set(REQUIRED_COMPONENTS)
    assert all(
        disk_report["components"][component]["ready"] is True
        for component in REQUIRED_COMPONENTS
    )

    semantic_api = (
        provider_export.verify_registered_collector_materialization_snapshot
    )
    semantic_calls = 0
    readiness_calls = 0
    readmissions = 0

    def semantic(*args, **kwargs) -> None:
        nonlocal semantic_calls
        semantic_calls += 1
        return semantic_api(*args, **kwargs)

    def readiness(*args, **kwargs):
        nonlocal readiness_calls
        readiness_calls += 1
        pytest.fail("broken continuity reached readiness verification")

    def readmission(*args, **kwargs):
        nonlocal readmissions
        readmissions += 1
        pytest.fail("broken continuity reached authority re-admission")

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)
    monkeypatch.setattr(
        companion_snapshot,
        "admit_signed_component_authority",
        readmission,
    )
    outward: list[dict[str, object]] = []

    with pytest.raises(ValueError, match="continuity semantic verification failed"):
        outward.append(export_verified_provider_receipt(bundle_file))

    assert semantic_calls == 1
    assert readiness_calls == 0
    assert readmissions == 0
    assert outward == []


def test_materializer_semantic_failure_cleans_candidate_without_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    candidate_reports = _capture_candidate_reports(monkeypatch)
    admission_api = provider_materializer.admit_signed_component_authority
    admissions = 0
    readiness_calls = 0

    def admission(*args, **kwargs):
        nonlocal admissions
        admissions += 1
        return admission_api(*args, **kwargs)

    def reject_semantic(*args, **kwargs) -> None:
        del args, kwargs
        raise continuity.CollectorContinuityError(
            "injected task 4.5 continuity failure"
        )

    def readiness(*args, **kwargs):
        nonlocal readiness_calls
        readiness_calls += 1
        pytest.fail("semantic failure reached readiness verification")

    monkeypatch.setattr(
        provider_materializer,
        "admit_signed_component_authority",
        admission,
    )
    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        reject_semantic,
    )
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)
    output = fixture.root / "injected-semantic-failure"
    outward: list[dict[str, object]] = []

    with pytest.raises(
        (ValueError, ProviderMaterializationError),
        match="continuity|semantic",
    ):
        outward.append(
            _materialize(
                fixture,
                monkeypatch,
                name="injected-semantic-failure",
            )
        )

    assert admissions == 5
    assert readiness_calls == 0
    assert len(candidate_reports) == 1
    assert candidate_reports[0]["ready"] is True
    assert outward == []
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["json", "signature"])
def test_calendar_authority_invalid_before_any_downstream_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    authority_files = _eligible_invalid_authority_inputs(
        fixture, "trading_calendar", mutation
    )
    downstream_calls = {"semantic": 0, "readiness": 0, "outward": 0}
    semantic_api = provider_export.verify_registered_collector_materialization_snapshot
    readiness_api = provider_export.verify_bound_readiness
    outward_api = provider_materializer._export_verified_provider_receipt

    def semantic(*args, **kwargs):
        downstream_calls["semantic"] += 1
        return semantic_api(*args, **kwargs)

    def readiness(*args, **kwargs):
        downstream_calls["readiness"] += 1
        return readiness_api(*args, **kwargs)

    def outward(*args, **kwargs):
        downstream_calls["outward"] += 1
        return outward_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)
    monkeypatch.setattr(
        provider_materializer, "_export_verified_provider_receipt", outward
    )
    output = fixture.root / f"invalid-calendar-{mutation}"

    with pytest.raises(ValueError, match="(?i)json|signature|authority"):
        _materialize(
            fixture,
            monkeypatch,
            name=f"invalid-calendar-{mutation}",
            authority_files=authority_files,
        )

    assert downstream_calls == {"semantic": 0, "readiness": 0, "outward": 0}
    assert not output.exists()


@pytest.mark.parametrize(
    ("component", "dependencies"),
    [
        ("universe", ("trading_calendar",)),
        ("market_rules", ("trading_calendar", "instrument_status")),
    ],
)
def test_eligible_invalid_signed_authority_rejects_without_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    dependencies: tuple[str, ...],
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    authority_files = _eligible_invalid_authority_inputs(
        fixture, component, "signature"
    )
    assert tuple(authority_files) == (*dependencies, component)
    downstream_calls = {"semantic": 0, "readiness": 0, "outward": 0}
    semantic_api = provider_export.verify_registered_collector_materialization_snapshot
    readiness_api = provider_export.verify_bound_readiness
    outward_api = provider_materializer._export_verified_provider_receipt

    def semantic(*args, **kwargs):
        downstream_calls["semantic"] += 1
        return semantic_api(*args, **kwargs)

    def readiness(*args, **kwargs):
        downstream_calls["readiness"] += 1
        return readiness_api(*args, **kwargs)

    def outward(*args, **kwargs):
        downstream_calls["outward"] += 1
        return outward_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        semantic,
    )
    monkeypatch.setattr(provider_export, "verify_bound_readiness", readiness)
    monkeypatch.setattr(
        provider_materializer, "_export_verified_provider_receipt", outward
    )
    output = fixture.root / f"invalid-{component}"

    with pytest.raises(ValueError, match="(?i)signature|authority"):
        _materialize(
            fixture,
            monkeypatch,
            name=f"invalid-{component}",
            authority_files=authority_files,
        )

    assert downstream_calls == {"semantic": 0, "readiness": 0, "outward": 0}
    assert not output.exists()
