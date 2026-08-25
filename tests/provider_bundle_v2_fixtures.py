from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal, Mapping

import pytest


CASE_NAMES = (
    "valid-blocked",
    "valid-complete",
    "legacy",
    "missing",
    "malformed",
    "truncated-ledger",
    "registration-drift",
    "state-rollback",
    "forged-continuity",
)


@dataclass(frozen=True)
class ProviderBundleV2Case:
    name: str
    bundle_file: Path
    expected_outcome: Literal["blocked", "complete", "reject"]
    expected_ready: bool | None
    expected_semantic_calls: int
    expected_readiness_calls: int
    expected_phase: Literal["exported", "pre-semantic", "semantic"]
    artifact_sha256: Mapping[str, str]
    baseline_sha256: Mapping[str, str]
    final_ledger_terminal_ordinal: int | None = None
    database_hashes: tuple[str, str] | None = None
    expected_cause_reason: str | None = None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_content_addressed(directory: Path, raw: bytes) -> tuple[Path, str]:
    digest = _sha256(raw)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / digest
    path.write_bytes(raw)
    return path, digest


def _set_locator(bundle: dict[str, object], field: str, old: object, path: Path, digest: str) -> None:
    locator = deepcopy(old)
    assert isinstance(locator, dict)
    reference = deepcopy(locator["reference"])
    assert isinstance(reference, dict)
    reference["identifier"] = digest
    bundle[field] = {"reference": reference, "path": str(path)}


def _write_bundle(directory: Path, bundle: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bundle.json"
    path.write_bytes(_canonical(bundle))
    return path


def _bundle_paths(bundle: Mapping[str, object]) -> tuple[Path, ...]:
    values: list[object] = [
        bundle["checkout"],
        bundle["database"],
        bundle["registration"],
        bundle["ledger_snapshot"],
        bundle["continuity_closure"],
        *bundle["source_receipts"],
        bundle["execution_adjustment_identity"],
        bundle["signal_adjustment_identity"],
        bundle["exact_panel"],
        *dict(bundle["components"]).values(),
        bundle["readiness_report"],
    ]
    return tuple(Path(value["path"]) for value in values if isinstance(value, Mapping))


def _manifest(paths: tuple[Path, ...] | list[Path]) -> dict[str, str]:
    return {str(path): _sha256(path.read_bytes()) for path in paths}


def _capture_ordinal_ten_checkpoint(
    original, checkpoint: Path
):
    def append(prepared: dict[str, object], lease: object, spec: object) -> None:
        original(prepared, lease, spec)
        if int(spec.step_ordinal) != 10:
            return
        source = Path(prepared["database"])
        destination = sqlite3.connect(checkpoint)
        try:
            source_connection = sqlite3.connect(source)
            try:
                source_connection.backup(destination)
            finally:
                source_connection.close()
        finally:
            destination.close()

    return append


def build_provider_bundle_v2_cases(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Mapping[str, ProviderBundleV2Case]:
    """Build the nine frozen cases from runtime materialized provider data."""

    from test_verified_provider_readiness import (
        _fixture,
        _materialize,
    )
    import test_verified_provider_readiness as readiness_tests

    root.mkdir(parents=True, exist_ok=True)
    source_root = root / "source"
    source_root.mkdir()
    checkpoint = source_root / "ordinal-10.sqlite"
    original_append = readiness_tests._append_provider_attempt
    monkeypatch.setattr(
        readiness_tests,
        "_append_provider_attempt",
        _capture_ordinal_ten_checkpoint(original_append, checkpoint),
    )

    complete_fixture = _fixture(source_root, complete_calendar=True)
    complete_result = _materialize(
        complete_fixture,
        monkeypatch,
        name="valid-complete",
    )
    complete_bundle = Path(complete_result["bundle_file"])
    complete_value = json.loads(complete_bundle.read_text(encoding="ascii"))
    baseline_manifest = _manifest([complete_bundle, *_bundle_paths(complete_value)])
    from stockdata.collector_continuity import parse_collector_ledger

    final_history = parse_collector_ledger(
        Path(complete_value["ledger_snapshot"]["path"]).read_bytes()
    )
    final_terminal_ordinal = final_history[-1]["event"]["step_ordinal"]

    blocked_authorities = dict(complete_fixture.authority_files)
    blocked_authorities.pop(sorted(blocked_authorities)[0])
    blocked_result = _materialize(
        complete_fixture,
        monkeypatch,
        name="valid-blocked",
        authority_files=blocked_authorities,
    )

    cases: dict[str, ProviderBundleV2Case] = {
        "valid-complete": ProviderBundleV2Case(
            "valid-complete", complete_bundle, "complete", True, 1, 1,
            "exported", {}, baseline_manifest,
        ),
        "valid-blocked": ProviderBundleV2Case(
            "valid-blocked", Path(blocked_result["bundle_file"]), "blocked", False,
            1, 1, "exported", {}, baseline_manifest,
        ),
    }

    def negative_case(
        name: str,
        bundle: dict[str, object],
        artifacts: list[Path],
        *,
        semantic: bool,
    ) -> None:
        case_directory = root / name
        path = _write_bundle(case_directory, bundle)
        cases[name] = ProviderBundleV2Case(
            name,
            path,
            "reject",
            None,
            1 if semantic else 0,
            0,
            "semantic" if semantic else "pre-semantic",
            _manifest(artifacts),
            baseline_manifest,
        )

    legacy = deepcopy(complete_value)
    legacy["schema_version"] = "stockdata-rqgm-provider-bundle/1"
    negative_case("legacy", legacy, [], semantic=False)

    missing = deepcopy(complete_value)
    del missing["continuity_closure"]
    negative_case("missing", missing, [], semantic=False)

    malformed = deepcopy(complete_value)
    malformed_path, _ = _write_content_addressed(
        root / "malformed" / "artifacts", b"{malformed"
    )
    _set_locator(
        malformed,
        "continuity_closure",
        complete_value["continuity_closure"],
        malformed_path,
        _sha256(b"{malformed"),
    )
    negative_case("malformed", malformed, [malformed_path], semantic=True)

    truncated = deepcopy(complete_value)
    ledger_raw = Path(complete_value["ledger_snapshot"]["path"]).read_bytes()
    truncated_raw = ledger_raw[:-2] + b"\n"
    truncated_path, _ = _write_content_addressed(
        root / "truncated-ledger" / "artifacts", truncated_raw
    )
    _set_locator(
        truncated,
        "ledger_snapshot",
        complete_value["ledger_snapshot"],
        truncated_path,
        _sha256(truncated_raw),
    )
    negative_case("truncated-ledger", truncated, [truncated_path], semantic=True)

    registration_drift = deepcopy(complete_value)
    registration = json.loads(
        Path(complete_value["registration"]["path"]).read_text(encoding="ascii")
    )
    registration["source"] = "fixture-registration-drift"
    registration_raw = _canonical(registration)
    registration_path, registration_digest = _write_content_addressed(
        root / "registration-drift" / "artifacts", registration_raw
    )
    _set_locator(
        registration_drift,
        "registration",
        complete_value["registration"],
        registration_path,
        registration_digest,
    )
    drift_closure = json.loads(
        Path(complete_value["continuity_closure"]["path"]).read_text(encoding="ascii")
    )
    drift_closure["registration_sha256"] = registration_digest
    drift_closure_raw = _canonical(drift_closure)
    drift_closure_path, drift_closure_digest = _write_content_addressed(
        root / "registration-drift" / "artifacts", drift_closure_raw
    )
    _set_locator(
        registration_drift,
        "continuity_closure",
        complete_value["continuity_closure"],
        drift_closure_path,
        drift_closure_digest,
    )
    negative_case(
        "registration-drift",
        registration_drift,
        [registration_path, drift_closure_path],
        semantic=True,
    )

    rollback = deepcopy(complete_value)
    rollback_raw = checkpoint.read_bytes()
    rollback_path, rollback_digest = _write_content_addressed(
        root / "state-rollback" / "artifacts", rollback_raw
    )
    _set_locator(rollback, "database", complete_value["database"], rollback_path, rollback_digest)
    rollback_closure = json.loads(
        Path(complete_value["continuity_closure"]["path"]).read_text(encoding="ascii")
    )
    rollback_closure["snapshot_database_reference"]["identifier"] = rollback_digest
    rollback_closure_raw = _canonical(rollback_closure)
    rollback_closure_path, rollback_closure_digest = _write_content_addressed(
        root / "state-rollback" / "artifacts", rollback_closure_raw
    )
    _set_locator(
        rollback,
        "continuity_closure",
        complete_value["continuity_closure"],
        rollback_closure_path,
        rollback_closure_digest,
    )
    negative_case(
        "state-rollback",
        rollback,
        [rollback_path, rollback_closure_path],
        semantic=True,
    )
    final_database_digest = complete_value["database"]["reference"]["identifier"]
    assert final_terminal_ordinal == 11
    assert rollback_digest != final_database_digest
    cases["state-rollback"] = replace(
        cases["state-rollback"],
        final_ledger_terminal_ordinal=final_terminal_ordinal,
        database_hashes=(rollback_digest, final_database_digest),
        expected_cause_reason="collector snapshot logical state drifted",
    )

    forged = deepcopy(complete_value)
    forged_closure = json.loads(
        Path(complete_value["continuity_closure"]["path"]).read_text(encoding="ascii")
    )
    forged_closure["ready"] = True
    forged_raw = _canonical(forged_closure)
    forged_path, forged_digest = _write_content_addressed(
        root / "forged-continuity" / "artifacts", forged_raw
    )
    _set_locator(
        forged,
        "continuity_closure",
        complete_value["continuity_closure"],
        forged_path,
        forged_digest,
    )
    negative_case("forged-continuity", forged, [forged_path], semantic=True)

    return {name: cases[name] for name in CASE_NAMES}
