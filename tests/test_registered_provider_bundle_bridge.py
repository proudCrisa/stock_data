from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import test_trusted_local_research_replay_resolver as replay_fixture
from test_trusted_local_forward_registration import _local_inputs, _register

import stockdata.collector_continuity as continuity
import stockdata.provider_export as provider_export_module
import stockdata.trusted_local_research_replay_export as replay_export_module
import stockdata.trusted_local_research_replay_materialization as replay_materialization_module
from stockdata import provider_materializer
from stockdata.cli import build_params, main
from stockdata.collector_continuity import (
    CollectorContinuityError,
    acquire_collector_phase_lease,
    default_collector_ledger_path,
    freeze_collector_step_schedule,
)
from stockdata.provider_export import (
    export_verified_provider_receipt,
    resolve_trusted_local_research_replay_inputs,
)
from stockdata.provider_materializer import (
    ProviderMaterializationError,
    materialize_registered_provider_bundle,
)
from stockdata.rqgm_provider_contract import (
    COMPONENT_SCHEMAS,
    REQUIRED_COMPONENTS,
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
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                "directory" if path.is_dir() else "file",
            )
            for path in root.rglob("*")
        )
    )


def _patch_corporate_actions(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "zero":
        return
    original = replay_fixture.capture_forward_corporate_actions

    def capture(
        cache: object,
        observation_date: str,
        *,
        fetcher: object,
        now: object = None,
    ) -> dict[str, object]:
        assert callable(fetcher)

        def replacement(
            symbols: tuple[str, ...], day: str
        ) -> replay_fixture.CapturedCorporateActions:
            captured = fetcher(symbols, day)
            receipt = deepcopy(captured.capture_receipt)
            if mode == "positive":
                fields = ["dividOperateDate"]
                raw_row = ["2026-09-09"]
                parsed_row = {"dividOperateDate": "2026-09-09"}
            else:
                fields = ["unsupportedEventType"]
                raw_row = ["opaque-event"]
                parsed_row = {"unsupportedEventType": "opaque-event"}
            for symbol in symbols:
                receipt["response"]["symbols"][symbol][0] = {
                    "year": 2025,
                    "fields": fields,
                    "rows": [raw_row],
                }
            return replay_fixture.CapturedCorporateActions(
                {symbol: [parsed_row] for symbol in symbols}, receipt
            )

        return original(
            cache,
            observation_date,
            fetcher=replacement,
            now=now,
        )

    monkeypatch.setattr(
        replay_fixture, "capture_forward_corporate_actions", capture
    )


def _patch_ambiguous_adjustment(
    monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    original = replay_fixture.sync_module.sync_symbols

    def sync_symbols(
        cache: object,
        symbols: list[str],
        start: str,
        end: str,
        **kwargs: object,
    ) -> object:
        if end == session:
            kwargs["adjustment_version"] = "tencent-qt-daily-v2"
        return original(cache, symbols, start, end, **kwargs)

    monkeypatch.setattr(replay_fixture.sync_module, "sync_symbols", sync_symbols)


def _completed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    completed: bool = True,
    corporate_actions: str = "zero",
    ambiguous_adjustment: bool = False,
) -> dict[str, object]:
    registration_root = tmp_path / "registration"
    registration_root.mkdir(parents=True)
    inputs = _local_inputs(registration_root, monkeypatch)
    _register(inputs)
    specs = freeze_collector_step_schedule(
        registration_file=inputs["output_file"]
    )
    panel = json.loads(Path(inputs["panel_file"]).read_bytes())
    symbols = sorted({entry.split("@", 1)[0] for entry in panel})
    sessions = sorted({entry.split("@", 1)[1] for entry in panel})
    ledger = Path(default_collector_ledger_path(inputs["database_file"]))
    prepared = {
        "database": inputs["database_file"],
        "ledger": ledger,
        "registration": inputs["output_file"],
    }
    if corporate_actions != "zero":
        _patch_corporate_actions(monkeypatch, corporate_actions)
    if ambiguous_adjustment:
        _patch_ambiguous_adjustment(monkeypatch, sessions[0])
    if completed:
        with acquire_collector_phase_lease(ledger) as lease:
            for spec in specs:
                replay_fixture._append_semantic_attempt(
                    prepared, lease, spec, symbols, sessions[0]
                )
    return {
        "registration": Path(inputs["output_file"]),
        "database": Path(inputs["database_file"]),
        "ledger": ledger,
        "inputs": inputs,
        "panel": panel,
    }


def _bridge(
    fixture: dict[str, object], output: Path
) -> Path:
    result = materialize_registered_provider_bundle(
        registration_file=fixture["registration"],
        database=fixture["database"],
        output_dir=output,
    )
    assert isinstance(result, Path)
    return result


RESEARCH_REPLAY_POLICY_REQUEST_SCHEMA = (
    "stockdata-rqgm-trusted-local-research-replay-policy-request/1"
)
RESEARCH_REPLAY_ENVELOPE_SCHEMA = (
    "stockdata-rqgm-trusted-local-research-replay-envelope/1"
)


def _research_replay_policy_request(
    bundle: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": RESEARCH_REPLAY_POLICY_REQUEST_SCHEMA,
        "replay_policy_binding": replay_fixture._policy_binding(bundle),
        "shared_cash_policy_body": replay_fixture._shared_cash_policy(),
        "risk_policy_body": replay_fixture._risk_policy(),
    }


def _research_replay_bridge():
    bridge = getattr(
        provider_export_module,
        "run_trusted_local_research_replay_bridge",
        None,
    )
    assert callable(bridge), "4.9 bridge public API is not implemented"
    return bridge


def _research_replay_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object], dict[str, object]]:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    bundle_file = _bridge(fixture, tmp_path / "published")
    bundle = json.loads(bundle_file.read_bytes())
    return bundle_file, bundle, _research_replay_policy_request(bundle)


def _assert_locator(locator: object, expected_schema: str | None = None) -> None:
    assert isinstance(locator, dict)
    assert set(locator) == {"reference", "path"}
    reference = locator["reference"]
    assert isinstance(reference, dict)
    assert set(reference) == {"kind", "identifier", "schema_version"}
    path = Path(locator["path"])
    assert path.is_absolute()
    assert path.is_file()
    assert reference["identifier"] == hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_schema is not None:
        assert reference["schema_version"] == expected_schema


def _assert_bundle(bundle_file: Path) -> dict[str, object]:
    assert bundle_file == bundle_file.resolve()
    raw = bundle_file.read_bytes()
    bundle = json.loads(raw)
    assert _canonical(bundle) == raw
    assert set(bundle) == BUNDLE_FIELDS
    assert bundle["schema_version"] == BUNDLE_SCHEMA
    panel = json.loads(Path(bundle["exact_panel"]["path"]).read_bytes())
    assert len(panel) == 36
    assert panel == sorted(set(panel))
    assert len(bundle["source_receipts"]) == 2
    for locator in bundle["source_receipts"]:
        _assert_locator(locator)
    _assert_locator(bundle["execution_adjustment_identity"])
    _assert_locator(bundle["signal_adjustment_identity"])
    assert bundle["execution_adjustment_identity"] != bundle[
        "signal_adjustment_identity"
    ]
    _assert_locator(bundle["checkout"])
    _assert_locator(bundle["database"])
    _assert_locator(bundle["registration"])
    _assert_locator(bundle["ledger_snapshot"])
    _assert_locator(bundle["continuity_closure"])
    _assert_locator(bundle["exact_panel"])
    assert set(bundle["components"]) == set(REQUIRED_COMPONENTS)
    for component in REQUIRED_COMPONENTS:
        locator = bundle["components"][component]
        _assert_locator(locator, COMPONENT_SCHEMAS[component])
        value = json.loads(Path(locator["path"]).read_bytes())
        assert value["schema_version"] == COMPONENT_SCHEMAS[component]
        assert value["panel"] == panel
        assert isinstance(value["records"], list)
        if component != "availability_records":
            assert len(value["records"]) == 36
    _assert_locator(bundle["readiness_report"])
    readiness = json.loads(Path(bundle["readiness_report"]["path"]).read_bytes())
    assert readiness["ready"] is False
    assert all(
        value["ready"] is False for value in readiness["components"].values()
    )
    return bundle


def test_registered_materializer_has_only_the_frozen_keyword_only_api() -> None:
    parameters = inspect.signature(
        materialize_registered_provider_bundle
    ).parameters
    assert list(parameters) == ["registration_file", "database", "output_dir"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters.values()
    )
    assert not {
        "panel",
        "adjustment",
        "component",
        "receipt",
        "callback",
        "ready",
        "authority",
        "result",
        "semantic_bytes",
    } & set(parameters)


def test_completed_v5_snapshot_materializes_exact_bundle_and_downstream_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    inputs_before = {
        path: path.read_bytes()
        for path in (
            fixture["registration"],
            fixture["database"],
            fixture["ledger"],
        )
    }
    output = tmp_path / "published"
    bundle_file = _bridge(fixture, output)
    assert bundle_file == output.resolve() / "bundle.json"
    bundle = _assert_bundle(bundle_file)
    regular = export_verified_provider_receipt(bundle_file)
    assert regular["ready"] is False
    policy = replay_fixture._policy_binding(bundle)
    resolved = resolve_trusted_local_research_replay_inputs(
        bundle_file, replay_policy_binding=policy
    )
    assert resolved["schema_version"] == (
        "stockdata-rqgm-research-replay-resolved-inputs/1"
    )
    assert set(resolved["component_payloads"]) == set(REQUIRED_COMPONENTS)
    assert all(path.read_bytes() == raw for path, raw in inputs_before.items())


def test_bridge_passes_snapshot_paths_to_existing_materializer_not_live_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    original = provider_materializer.materialize_provider_bundle
    calls: list[dict[str, object]] = []

    def delegate(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(provider_materializer, "materialize_provider_bundle", delegate)
    _bridge(fixture, tmp_path / "published")
    assert len(calls) == 1
    call = calls[0]
    assert Path(call["registration_file"]).resolve() != fixture["registration"].resolve()
    assert Path(call["database_file"]).resolve() != fixture["database"].resolve()
    assert Path(call["registration_file"]).name != fixture["registration"].name
    assert Path(call["database_file"]).name != fixture["database"].name
    assert Path(call["registration_file"]).parent == Path(call["database_file"]).parent


def test_zero_of_twelve_fails_before_output_or_snapshot_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch, completed=False)
    output = tmp_path / "published"
    before = _tree(tmp_path)
    snapshot_calls: list[object] = []
    original = provider_materializer.create_registered_collector_materialization_snapshot

    def snapshot(*args: object, **kwargs: object) -> object:
        snapshot_calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        snapshot,
    )
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert snapshot_calls == []
    assert not output.exists()
    assert _tree(tmp_path) == before


def test_existing_output_is_rejected_without_overwrite_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    output = tmp_path / "published"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"existing")
    before = _tree(tmp_path)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert _tree(tmp_path) == before
    assert sentinel.read_bytes() == b"existing"


@pytest.mark.parametrize("drift", ["registration", "database", "ledger", "prerequisite"])
def test_identity_and_prerequisite_drift_fails_without_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    if drift == "registration":
        path = fixture["registration"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif drift == "database":
        path = fixture["database"]
        path.write_bytes(path.read_bytes() + b"\x00")
    elif drift == "ledger":
        path = fixture["ledger"]
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = fixture["inputs"]["calendar_file"]
        path.write_bytes(path.read_bytes() + b"\n")
    output = tmp_path / "published"
    before = _tree(tmp_path)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert not output.exists()
    assert _tree(tmp_path) == before


def test_database_aba_replacement_fails_closed_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    database = fixture["database"]
    replacement = tmp_path / "database-aba"
    replacement.write_bytes(database.read_bytes())
    os.replace(replacement, database)
    output = tmp_path / "published"
    before = _tree(tmp_path)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert not output.exists()
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("corporate_actions", ["positive", "ambiguous"])
def test_positive_or_ambiguous_corporate_actions_fail_without_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corporate_actions: str,
) -> None:
    fixture = _completed_snapshot(
        tmp_path, monkeypatch, corporate_actions=corporate_actions
    )
    output = tmp_path / "published"
    before = _tree(tmp_path)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert not output.exists()
    assert _tree(tmp_path) == before


def test_ambiguous_adjustment_identity_fails_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "published"
    with pytest.raises(
        CollectorContinuityError, match="allowed-table complement"
    ):
        _completed_snapshot(tmp_path, monkeypatch, ambiguous_adjustment=True)
    assert not output.exists()
    assert not any(
        path.name.startswith(".registered-provider-materialize-")
        for path in tmp_path.iterdir()
    )


def test_same_inode_database_aba_fails_without_publication_or_fixture_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_root = tmp_path / "aba"
    fixture_root.mkdir()
    fixture = _completed_snapshot(fixture_root, monkeypatch)
    database = Path(fixture["database"])
    original_database_raw = database.read_bytes()
    tracked_inputs = [
        Path(fixture["registration"]),
        database,
        Path(fixture["ledger"]),
        Path(fixture["inputs"]["calendar_file"]),
        Path(fixture["inputs"]["market_rules_file"]),
        *map(Path, fixture["inputs"]["source_receipt_files"]),
    ]
    before = {path: path.read_bytes() for path in tracked_inputs}
    before_tree = _tree(fixture_root)
    original_snapshot = provider_materializer.create_registered_collector_materialization_snapshot
    vacuumed = fixture_root / "database-b.sqlite"

    def mixed_snapshot(*args: object, **kwargs: object) -> dict[str, object]:
        database_identity = os.stat(database)
        try:
            with sqlite3.connect(database) as connection:
                connection.execute("VACUUM INTO ?", (str(vacuumed),))
            replacement_raw = vacuumed.read_bytes()
            if replacement_raw == original_database_raw:
                with sqlite3.connect(vacuumed) as connection:
                    connection.execute("PRAGMA user_version = 1")
                replacement_raw = vacuumed.read_bytes()
            assert replacement_raw != original_database_raw
            with database.open("r+b") as stream:
                stream.seek(0)
                stream.truncate()
                stream.write(replacement_raw)
                stream.flush()
                os.fsync(stream.fileno())
            assert (os.stat(database).st_dev, os.stat(database).st_ino) == (
                database_identity.st_dev,
                database_identity.st_ino,
            )
            try:
                result = original_snapshot(*args, **kwargs)
                assert result["database"]["reference"]["identifier"] != hashlib.sha256(
                    original_database_raw
                ).hexdigest()
                return result
            finally:
                with database.open("r+b") as stream:
                    stream.seek(0)
                    stream.truncate()
                    stream.write(original_database_raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        finally:
            vacuumed.unlink(missing_ok=True)

    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        mixed_snapshot,
    )
    output = fixture_root / "published"
    with pytest.raises(ProviderMaterializationError, match="database|snapshot|drift"):
        _bridge(fixture, output)

    assert not output.exists()
    assert not any(
        path.name.startswith(".registered-provider-materialize-")
        for path in fixture_root.iterdir()
    )
    assert {path: path.read_bytes() for path in tracked_inputs} == before
    assert _tree(fixture_root) == before_tree


def test_transient_same_inode_database_aba_fails_in_retained_snapshot_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_root = tmp_path / "transient-aba"
    fixture_root.mkdir()
    fixture = _completed_snapshot(fixture_root, monkeypatch)
    database = Path(fixture["database"])
    original_database_raw = database.read_bytes()
    tracked_inputs = [
        Path(fixture["registration"]),
        database,
        Path(fixture["ledger"]),
        Path(fixture["inputs"]["calendar_file"]),
        Path(fixture["inputs"]["market_rules_file"]),
        *map(Path, fixture["inputs"]["source_receipt_files"]),
    ]
    before = {path: path.read_bytes() for path in tracked_inputs}
    before_tree = _tree(fixture_root)
    original_verify = continuity._verify_retained_collector_materialization_inputs
    vacuumed = fixture_root / "database-transient-b.sqlite"
    verification_calls = 0

    def transient_verify(
        values: object, *, registration_file: str, database: str
    ) -> dict[str, bytes]:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls != 2:
            return original_verify(
                values,
                registration_file=registration_file,
                database=database,
            )
        database_path = Path(database)
        database_identity = os.stat(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.execute("VACUUM INTO ?", (str(vacuumed),))
        replacement_raw = vacuumed.read_bytes()
        assert replacement_raw != original_database_raw
        with database_path.open("r+b") as stream:
            stream.seek(0)
            stream.truncate()
            stream.write(replacement_raw)
            stream.flush()
            os.fsync(stream.fileno())
        assert (os.stat(database_path).st_dev, os.stat(database_path).st_ino) == (
            database_identity.st_dev,
            database_identity.st_ino,
        )
        try:
            return original_verify(
                values,
                registration_file=registration_file,
                database=database,
            )
        finally:
            with database_path.open("r+b") as stream:
                stream.seek(0)
                stream.truncate()
                stream.write(original_database_raw)
                stream.flush()
                os.fsync(stream.fileno())
            vacuumed.unlink(missing_ok=True)

    monkeypatch.setattr(
        continuity,
        "_verify_retained_collector_materialization_inputs",
        transient_verify,
    )
    output = fixture_root / "published"
    with pytest.raises(ProviderMaterializationError, match="database|snapshot|drift"):
        _bridge(fixture, output)

    assert verification_calls == 2
    assert not output.exists()
    assert not any(
        path.name.startswith(".registered-provider-materialize-")
        for path in fixture_root.iterdir()
    )
    assert {path: path.read_bytes() for path in tracked_inputs} == before
    assert _tree(fixture_root) == before_tree


def test_price_raw_verifier_failure_cannot_complete_or_publish_registered_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registration_root = tmp_path / "registration"
    registration_root.mkdir()
    inputs = _local_inputs(registration_root, monkeypatch)
    _register(inputs)
    specs = freeze_collector_step_schedule(
        registration_file=inputs["output_file"]
    )
    panel = json.loads(Path(inputs["panel_file"]).read_bytes())
    symbols = sorted({entry.split("@", 1)[0] for entry in panel})
    sessions = sorted({entry.split("@", 1)[1] for entry in panel})
    ledger = Path(default_collector_ledger_path(inputs["database_file"]))
    prepared = {
        "database": inputs["database_file"],
        "ledger": ledger,
        "registration": inputs["output_file"],
    }
    with acquire_collector_phase_lease(ledger) as lease:
        for spec in specs[:3]:
            replay_fixture._append_semantic_attempt(
                prepared, lease, spec, symbols, sessions[0]
            )

    def reject_prices(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise CollectorContinuityError("injected price raw verifier failure")

    monkeypatch.setattr(continuity, "_verify_prices_raw", reject_prices)
    before_fds = set(os.listdir("/dev/fd"))
    with (
        pytest.raises(CollectorContinuityError, match="injected price raw verifier failure"),
        acquire_collector_phase_lease(ledger) as lease,
    ):
        replay_fixture._append_semantic_attempt(
            prepared, lease, specs[3], symbols, sessions[0]
        )
    assert set(os.listdir("/dev/fd")) == before_fds

    history = continuity.parse_collector_ledger(ledger.read_bytes())
    completed_ordinals = [
        event["event"]["step_ordinal"]
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
    ]
    assert completed_ordinals == [0, 1, 2]
    assert not any(
        event["event_type"] == "ATTEMPT_COMPLETED"
        and event["event"]["step_ordinal"] == 3
        for event in history
    )

    output = tmp_path / "published"
    bridge_fds = set(os.listdir("/dev/fd"))
    with pytest.raises(ProviderMaterializationError, match="complete|ledger|attempt"):
        _bridge(
            {"registration": Path(inputs["output_file"]), "database": Path(inputs["database_file"])},
            output,
        )
    assert set(os.listdir("/dev/fd")) == bridge_fds
    assert not output.exists()
    assert not any(
        path.name.startswith(".registered-provider-materialize-")
        for path in tmp_path.iterdir()
    )


def test_availability_closure_drift_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    original = provider_materializer.verify_component_availability_records
    calls: list[object] = []

    def drifted(artifact: object, *args: object, **kwargs: object) -> object:
        calls.append(artifact)
        mutated = deepcopy(artifact)
        mutated["records"][0]["record_sha256"] = "f" * 64
        return original(mutated, *args, **kwargs)

    monkeypatch.setattr(
        provider_materializer, "verify_component_availability_records", drifted
    )
    output = tmp_path / "published"
    before = _tree(tmp_path)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert calls
    assert not output.exists()
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("fault", ["write", "rename", "cleanup"])
def test_write_rename_cleanup_faults_publish_no_target_or_private_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    fixture = _completed_snapshot(tmp_path, monkeypatch)
    output = tmp_path / "published"
    before = _tree(tmp_path)
    calls: list[str] = []
    if fault == "write":
        def fail_materialize(**kwargs: object) -> object:
            calls.append("write")
            path = Path(kwargs["output_dir"])
            path.mkdir(parents=True, exist_ok=True)
            (path / "partial").write_bytes(b"partial")
            raise OSError("injected write fault")

        monkeypatch.setattr(
            provider_materializer, "materialize_provider_bundle", fail_materialize
        )
    elif fault == "rename":
        def fail_replace(source: object, destination: object, *args: object) -> object:
            calls.append("rename")
            raise OSError("injected rename fault")

        monkeypatch.setattr(provider_materializer.os, "replace", fail_replace)
    else:
        def fail_materialize(**kwargs: object) -> object:
            calls.append("downstream")
            path = Path(kwargs["output_dir"])
            path.mkdir(parents=True, exist_ok=True)
            (path / "partial").write_bytes(b"partial")
            raise OSError("injected downstream fault")

        monkeypatch.setattr(
            provider_materializer, "materialize_provider_bundle", fail_materialize
        )
        original_rmtree = provider_materializer.shutil.rmtree
        cleanup_calls: list[Path] = []

        def flaky_rmtree(path: object, *args: object, **kwargs: object) -> object:
            cleanup_calls.append(Path(path))
            if len(cleanup_calls) == 1:
                raise OSError("injected cleanup fault")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(provider_materializer.shutil, "rmtree", flaky_rmtree)
    with pytest.raises((ProviderMaterializationError, ValueError, OSError)):
        _bridge(fixture, output)
    assert calls
    assert not output.exists()
    assert _tree(tmp_path) == before


def test_registered_materializer_cli_has_success_and_failure_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "success").mkdir()
    fixture = _completed_snapshot(tmp_path / "success", monkeypatch)
    output = tmp_path / "success-output"
    command = [
        sys.executable,
        "-m",
        "stockdata.cli",
        "rqgm-provider-materialize-registered",
        "--registration-file",
        str(fixture["registration"]),
        "--database",
        str(fixture["database"]),
        "--output-dir",
        str(output),
    ]
    success = subprocess.run(command, capture_output=True, text=True, check=False)
    assert success.returncode == 0
    assert (output / "bundle.json").is_file()

    incomplete = _completed_snapshot(tmp_path / "incomplete", monkeypatch, completed=False)
    failed_output = tmp_path / "failed-output"
    failed = subprocess.run(
        [
            *command[:4],
            "--registration-file",
            str(incomplete["registration"]),
            "--database",
            str(incomplete["database"]),
            "--output-dir",
            str(failed_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert not failed_output.exists()


def test_cli_rejects_caller_semantic_parameters() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "rqgm-provider-materialize-registered",
                "--registration-file",
                "/tmp/registration.json",
                "--database",
                "/tmp/database.sqlite",
                "--output-dir",
                "/tmp/output",
                "--panel-file",
                "/tmp/panel.json",
            ]
        )


def test_research_replay_bridge_has_only_the_frozen_public_api() -> None:
    parameters = inspect.signature(_research_replay_bridge()).parameters
    assert list(parameters) == ["bundle_file", "policy_request"]
    assert parameters["bundle_file"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["bundle_file"].default is inspect.Parameter.empty
    assert parameters["policy_request"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["policy_request"].default is inspect.Parameter.empty
    assert not {
        "expected_bindings",
        "component_payloads",
        "provider_export",
        "materialization",
        "plan",
        "result",
        "callback",
        "alternate_step",
        "authority",
        "ready",
        "writer",
        "replay",
    } & set(parameters)


def test_research_replay_bridge_calls_fixed_steps_once_in_order_and_returns_exact_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_file, _bundle, request = _research_replay_fixture(tmp_path, monkeypatch)
    bridge = _research_replay_bridge()
    before = _tree(tmp_path)
    calls: list[tuple[str, object]] = []
    resolved_holder: dict[str, object] = {}
    export_holder: dict[str, object] = {}
    materialization_holder: dict[str, object] = {}

    original_resolve = provider_export_module.resolve_trusted_local_research_replay_inputs
    original_export = replay_export_module.build_trusted_local_research_replay_export
    original_materialization = (
        replay_materialization_module.build_trusted_local_research_replay_materialization
    )

    def resolve(bundle_path: object, *, replay_policy_binding: object) -> object:
        calls.append(("resolve", replay_policy_binding))
        resolved = original_resolve(
            bundle_path,
            replay_policy_binding=replay_policy_binding,
        )
        resolved_holder["value"] = resolved
        return resolved

    def build_export(*, expected_bindings: object) -> object:
        calls.append(("export", expected_bindings))
        exported = original_export(expected_bindings=expected_bindings)
        export_holder["value"] = exported
        return exported

    def build_materialization(**kwargs: object) -> object:
        calls.append(("materialization", kwargs))
        materialized = original_materialization(**kwargs)
        materialization_holder["value"] = materialized
        return materialized

    monkeypatch.setattr(
        provider_export_module,
        "resolve_trusted_local_research_replay_inputs",
        resolve,
    )
    monkeypatch.setattr(
        replay_export_module,
        "build_trusted_local_research_replay_export",
        build_export,
    )
    monkeypatch.setattr(
        replay_materialization_module,
        "build_trusted_local_research_replay_materialization",
        build_materialization,
    )
    monkeypatch.setattr(
        provider_export_module,
        "build_trusted_local_research_replay_export",
        build_export,
        raising=False,
    )
    monkeypatch.setattr(
        provider_export_module,
        "build_trusted_local_research_replay_materialization",
        build_materialization,
        raising=False,
    )

    envelope = bridge(bundle_file, policy_request=request)

    assert [name for name, _ in calls] == [
        "resolve",
        "export",
        "materialization",
    ]
    assert len(calls) == 3
    resolved = resolved_holder["value"]
    exported = export_holder["value"]
    materialized = materialization_holder["value"]
    assert isinstance(resolved, dict)
    assert isinstance(exported, dict)
    assert isinstance(materialized, dict)
    assert calls[1][1] is resolved["expected_bindings"]
    materialization_call = calls[2][1]
    assert isinstance(materialization_call, dict)
    assert set(materialization_call) == {
        "provider_export",
        "expected_bindings",
        "component_payloads",
        "shared_cash_policy_body",
        "risk_policy_body",
    }
    assert materialization_call["provider_export"] is exported
    assert materialization_call["expected_bindings"] is resolved["expected_bindings"]
    assert materialization_call["component_payloads"] is resolved["component_payloads"]
    assert materialization_call["shared_cash_policy_body"] == request[
        "shared_cash_policy_body"
    ]
    assert materialization_call["risk_policy_body"] == request["risk_policy_body"]

    assert set(envelope) == {
        "schema_version",
        "provider_export",
        "provider_expected_bindings",
        "provider_materialization",
    }
    assert envelope["schema_version"] == RESEARCH_REPLAY_ENVELOPE_SCHEMA
    assert envelope["provider_export"] == exported
    assert envelope["provider_expected_bindings"] is resolved["expected_bindings"]
    assert envelope["provider_materialization"] == materialized
    assert _tree(tmp_path) == before
    assert capsys.readouterr().out == ""
    assert export_verified_provider_receipt(bundle_file)["ready"] is False


def test_research_replay_bridge_rejects_nonexact_request_and_composed_inputs_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_file, bundle, request = _research_replay_fixture(tmp_path, monkeypatch)
    bridge = _research_replay_bridge()
    before = _tree(tmp_path)
    required = tuple(request)
    forbidden = (
        "expected_bindings",
        "component_payloads",
        "provider_export",
        "materialization",
        "plan",
        "result",
        "callback",
        "alternate_step",
    )
    candidates: list[object] = [None]
    candidates.extend(
        {
            key: value
            for key, value in request.items()
            if key != missing
        }
        for missing in required
    )
    candidates.extend(
        {
            **deepcopy(request),
            key: None,
        }
        for key in required
    )
    candidates.extend(
        {
            **deepcopy(request),
            key: {},
        }
        for key in forbidden
    )

    for candidate in candidates:
        with pytest.raises((TypeError, ValueError)):
            bridge(bundle_file, policy_request=candidate)
        assert _tree(tmp_path) == before
        assert capsys.readouterr().out == ""

    assert request == _research_replay_policy_request(bundle)


def test_research_replay_bridge_rejects_policy_drift_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_file, _, request = _research_replay_fixture(tmp_path, monkeypatch)
    bridge = _research_replay_bridge()
    before = _tree(tmp_path)
    request["risk_policy_body"]["target_weight_max"] = 0.21

    with pytest.raises((TypeError, ValueError)):
        bridge(bundle_file, policy_request=request)

    assert _tree(tmp_path) == before
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("stage", ["resolver", "export", "materialization"])
def test_research_replay_bridge_rejects_intermediate_drift_without_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    bundle_file, _bundle, request = _research_replay_fixture(tmp_path, monkeypatch)
    bridge = _research_replay_bridge()
    before = _tree(tmp_path)

    if stage == "resolver":
        original = provider_export_module.resolve_trusted_local_research_replay_inputs

        def drifted_resolver(
            bundle_path: object, *, replay_policy_binding: object
        ) -> object:
            value = deepcopy(
                original(
                    bundle_path,
                    replay_policy_binding=replay_policy_binding,
                )
            )
            value["expected_bindings"]["panel_reference"]["ordered_cells"][0] = (
                "000002.SZ@2026-09-07"
            )
            return value

        monkeypatch.setattr(
            provider_export_module,
            "resolve_trusted_local_research_replay_inputs",
            drifted_resolver,
        )
    elif stage == "export":
        original = replay_export_module.build_trusted_local_research_replay_export

        def drifted_export(*, expected_bindings: object) -> object:
            value = deepcopy(
                original(expected_bindings=expected_bindings)
            )
            value["scope"] = "PIT_EXECUTION"
            return value

        monkeypatch.setattr(
            replay_export_module,
            "build_trusted_local_research_replay_export",
            drifted_export,
        )
        monkeypatch.setattr(
            provider_export_module,
            "build_trusted_local_research_replay_export",
            drifted_export,
            raising=False,
        )
    else:
        original = (
            replay_materialization_module.build_trusted_local_research_replay_materialization
        )

        def drifted_materialization(**kwargs: object) -> object:
            value = deepcopy(original(**kwargs))
            value["schema_version"] = "drifted/1"
            return value

        monkeypatch.setattr(
            replay_materialization_module,
            "build_trusted_local_research_replay_materialization",
            drifted_materialization,
        )
        monkeypatch.setattr(
            provider_export_module,
            "build_trusted_local_research_replay_materialization",
            drifted_materialization,
            raising=False,
        )

    with pytest.raises((TypeError, ValueError)):
        bridge(bundle_file, policy_request=request)

    assert _tree(tmp_path) == before
    assert capsys.readouterr().out == ""


def test_research_replay_bridge_source_has_one_fixed_chain_and_no_writer_or_callback() -> None:
    source = inspect.getsource(_research_replay_bridge())
    assert source.count("resolve_trusted_local_research_replay_inputs(") == 1
    assert source.count("build_trusted_local_research_replay_export(") == 1
    assert source.count("build_trusted_local_research_replay_materialization(") == 1
    for forbidden in (
        "materialize_provider_bundle(",
        "materialize_registered_provider_bundle(",
        "export_verified_provider_receipt(",
        "write_bytes(",
        "write_text(",
        "callback",
        "subprocess",
        "sqlite3",
    ):
        assert forbidden not in source


def test_research_replay_cli_has_exact_parameters_and_reads_one_canonical_request_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_file, _bundle, request = _research_replay_fixture(tmp_path, monkeypatch)
    policy_file = tmp_path / "replay-policy-request.json"
    policy_file.write_bytes(_canonical(request))
    arguments = [
        "rqgm-provider-research-replay",
        "--bundle-file",
        str(bundle_file),
        "--policy-request-file",
        str(policy_file),
    ]
    assert build_params(arguments) == {
        "kind": "rqgm_provider_research_replay",
        "bundle_file": str(bundle_file),
        "policy_request_file": str(policy_file),
    }
    expected = {
        "schema_version": RESEARCH_REPLAY_ENVELOPE_SCHEMA,
        "provider_export": {},
        "provider_expected_bindings": {},
        "provider_materialization": {},
    }
    calls: list[tuple[object, object]] = []

    def fake_bridge(bundle_path: object, *, policy_request: object) -> object:
        calls.append((bundle_path, policy_request))
        return expected

    monkeypatch.setattr(
        provider_export_module,
        "run_trusted_local_research_replay_bridge",
        fake_bridge,
        raising=False,
    )
    assert main(arguments) == 0
    assert calls == [(str(bundle_file), request)]
    assert json.loads(capsys.readouterr().out) == expected
    assert policy_file.read_bytes() == _canonical(request)

    for forbidden in (
        "--expected-bindings",
        "--component-payload",
        "--provider-export",
        "--materialization",
        "--plan",
        "--result",
        "--callback",
        "--step",
    ):
        with pytest.raises(SystemExit):
            main([*arguments, forbidden, "{}"])


def test_research_replay_bridge_rejects_mapping_split_view_after_canonical_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_file, _, request = _research_replay_fixture(tmp_path, monkeypatch)
    bridge = _research_replay_bridge()
    before = _tree(tmp_path)
    alternate = deepcopy(request)
    alternate["shared_cash_policy_body"]["initial_capital"] = 2_000_000.0
    alternate["replay_policy_binding"]["shared_cash_policy_reference"]["sha256"] = (
        hashlib.sha256(
            _canonical(alternate["shared_cash_policy_body"])
        ).hexdigest()
    )

    class SplitView(dict[str, object]):
        def __init__(
            self, canonical: dict[str, object], live: dict[str, object]
        ) -> None:
            super().__init__(canonical)
            self._canonical = canonical
            self._live = live

        def items(self) -> object:
            return self._canonical.items()

        def get(self, key: str, default: object = None) -> object:
            return self._canonical.get(key, default)

        def __getitem__(self, key: str) -> object:
            return self._live[key]

    split_request = SplitView(request, alternate)
    with pytest.raises((TypeError, ValueError)):
        bridge(bundle_file, policy_request=split_request)

    assert _tree(tmp_path) == before
    assert capsys.readouterr().out == ""


def test_research_replay_cli_rejects_all_scalar_parameter_abbreviations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_bytes(b"{}")
    calls: list[object] = []

    def fake_bridge(bundle_path: object, *, policy_request: object) -> object:
        calls.append((bundle_path, policy_request))
        return {
            "schema_version": RESEARCH_REPLAY_ENVELOPE_SCHEMA,
            "provider_export": {},
            "provider_expected_bindings": {},
            "provider_materialization": {},
        }

    monkeypatch.setattr(
        provider_export_module,
        "run_trusted_local_research_replay_bridge",
        fake_bridge,
        raising=False,
    )
    for full_flag, other_flag in (
        ("--bundle-file", "--policy-request-file"),
        ("--policy-request-file", "--bundle-file"),
    ):
        for length in range(3, len(full_flag)):
            abbreviation = full_flag[:length]
            arguments = [
                "rqgm-provider-research-replay",
                abbreviation,
                "/tmp/value",
                other_flag,
                str(policy_file),
            ]
            with pytest.raises(SystemExit):
                main(arguments)
            assert capsys.readouterr().out == ""

    assert calls == []


def test_research_replay_cli_rejects_duplicate_scalar_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_bytes(b"{}")
    calls: list[object] = []

    def fake_bridge(bundle_path: object, *, policy_request: object) -> object:
        calls.append((bundle_path, policy_request))
        return {
            "schema_version": RESEARCH_REPLAY_ENVELOPE_SCHEMA,
            "provider_export": {},
            "provider_expected_bindings": {},
            "provider_materialization": {},
        }

    monkeypatch.setattr(
        provider_export_module,
        "run_trusted_local_research_replay_bridge",
        fake_bridge,
        raising=False,
    )
    for flag in ("--bundle-file", "--policy-request-file"):
        arguments = [
            "rqgm-provider-research-replay",
            "--bundle-file",
            "/tmp/bundle-a.json",
            "--policy-request-file",
            str(policy_file),
            flag,
            "/tmp/duplicate.json",
        ]
        with pytest.raises(SystemExit):
            main(arguments)
        assert capsys.readouterr().out == ""

    assert calls == []
