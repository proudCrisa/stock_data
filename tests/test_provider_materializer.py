from __future__ import annotations

import json
import hashlib
import os
import signal
import sqlite3
from pathlib import Path

import pytest

import stockdata.collector_continuity as continuity
import stockdata.provider_export as provider_export
import stockdata.provider_materializer as provider_materializer
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
from stockdata.rqgm_provider_contract import DATABASE_SCHEMA, REQUIRED_COMPONENTS
from test_collector_attempt_protocol import _prepared


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


def _private_export_api():
    path_api = getattr(provider_export, "_export_verified_provider_receipt", None)
    if callable(path_api):
        return path_api
    core_api = getattr(provider_export, "_verify_provider_bundle", None)
    if callable(core_api):
        def verify(path):
            raw = Path(path).read_bytes()
            return core_api(json.loads(raw.decode("ascii")), raw)

        return verify
    pytest.fail("materializer lacks a private unpublished bundle verifier")


def _materializer_private_export_api():
    for name in ("_export_verified_provider_receipt", "_verify_provider_bundle"):
        value = getattr(provider_materializer, name, None)
        if callable(value):
            return name, value
    pytest.fail("materializer does not use a private unpublished verifier")


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "cache.sqlite"
    with sqlite3.connect(database):
        pass
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


def _collector_inputs(tmp_path, monkeypatch):
    from test_collector_phase_orchestration import _append_completed_attempt

    collector = tmp_path / "collector"
    collector.mkdir(parents=True)
    prepared = _prepared(collector, monkeypatch)
    with continuity.acquire_collector_phase_lease(prepared["ledger"]) as lease:
        for spec in prepared["schedule"]:
            _append_completed_attempt(lease, spec)
    staging = tmp_path / "snapshot-staging"
    staging.mkdir()
    inputs = _inputs(tmp_path / "inputs")
    registration = json.loads(prepared["registration"].read_text(encoding="ascii"))
    panel = sorted(
        f"{symbol}@{session}"
        for symbol in registration["symbols"]
        for session in registration["sessions"]
    )
    inputs["panel_file"].write_bytes(_json(panel))
    inputs.update(
        database_file=prepared["database"],
        registration_file=prepared["registration"],
        snapshot_staging_directory=staging,
    )
    return inputs, prepared, staging


def test_materializer_creates_verified_fail_closed_content_closure(
    tmp_path, monkeypatch
) -> None:
    inputs, _, _ = _collector_inputs(tmp_path, monkeypatch)
    result = materialize_provider_bundle(output_dir=tmp_path / "closure", **inputs)

    assert result["receipt"]["ready"] is False
    assert (tmp_path / "closure" / "companion_snapshot.json").is_file()
    exported = export_verified_provider_receipt(result["bundle_file"])
    assert exported["contract"] == result["receipt"]["contract"]
    assert all(
        item["code"] == "provider_component_authority_not_attested"
        for item in exported["readiness_report"]["blockers"]
        if item["code"] == "provider_component_authority_not_attested"
    )


def test_materializer_rejects_component_drift_after_binding(
    tmp_path, monkeypatch
) -> None:
    inputs, _, _ = _collector_inputs(tmp_path, monkeypatch)
    result = materialize_provider_bundle(output_dir=tmp_path / "closure", **inputs)
    bundle = json.loads(Path(result["bundle_file"]).read_text(encoding="ascii"))
    database_path = Path(bundle["database"]["path"])
    database_path.chmod(0o600)
    database_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="database artifact content has drifted"):
        export_verified_provider_receipt(result["bundle_file"])


def test_materializer_rejects_research_receipts_and_missing_components(
    tmp_path, monkeypatch
) -> None:
    inputs, _, _ = _collector_inputs(tmp_path / "research-case", monkeypatch)
    receipt = inputs["source_receipt_files"][0]
    receipt.write_bytes(_json({"schema_version": "stockdata-research-calendar/1"}))
    with pytest.raises(ProviderMaterializationError, match="research-only"):
        materialize_provider_bundle(output_dir=tmp_path / "research", **inputs)

    inputs, _, _ = _collector_inputs(tmp_path / "missing-case", monkeypatch)
    inputs["component_files"].pop("market_rules")
    with pytest.raises(ProviderMaterializationError, match="every required component"):
        materialize_provider_bundle(output_dir=tmp_path / "missing", **inputs)


def test_materializer_contract_identity_is_reproducible(tmp_path, monkeypatch) -> None:
    inputs, _, _ = _collector_inputs(tmp_path, monkeypatch)
    first = materialize_provider_bundle(output_dir=tmp_path / "first", **inputs)
    second = materialize_provider_bundle(output_dir=tmp_path / "second", **inputs)

    assert (
        first["receipt"]["contract"]["contract_sha256"]
        == second["receipt"]["contract"]["contract_sha256"]
    )


def test_materializer_cli_writes_a_verified_blocked_bundle(
    tmp_path, monkeypatch, capsys
) -> None:
    inputs, _, _ = _collector_inputs(tmp_path, monkeypatch)
    command = [
        "rqgm-provider-materialize",
        "--output-dir", str(tmp_path / "closure"),
        "--database", str(inputs["database_file"]),
        "--registration-file", str(inputs["registration_file"]),
        "--snapshot-staging-directory", str(inputs["snapshot_staging_directory"]),
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


def test_noncollector_materialization_does_not_enter_collector_snapshot_path(
    tmp_path, monkeypatch
) -> None:
    snapshot_api = continuity.create_registered_collector_materialization_snapshot
    snapshot_calls: list[object] = []

    def snapshot(*args, **kwargs):
        snapshot_calls.append((args, kwargs))
        return snapshot_api(*args, **kwargs)

    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        snapshot,
        raising=False,
    )

    output = tmp_path / "closure"
    inputs = _inputs(tmp_path / "inputs")
    inputs.update(
        registration_file=tmp_path / "registration.json",
        snapshot_staging_directory=tmp_path / "snapshots",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        provider_materializer,
        "_read_regular",
        lambda *args, **kwargs: calls.append("read") or pytest.fail(
            "noncollector input reached provider reads"
        ),
    )

    with pytest.raises(
        (ProviderMaterializationError, continuity.CollectorContinuityError),
        match=r"collector|genesis|registration",
    ):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert snapshot_calls == []
    assert calls == []
    assert not output.exists()


@pytest.mark.parametrize("schema", ["rqgm-forward-panel-registration/1", "rqgm-forward-panel-registration/3"])
def test_legacy_registration_rejects_before_provider_reads_or_output(
    tmp_path, monkeypatch, schema
) -> None:
    inputs, prepared, _ = _collector_inputs(tmp_path, monkeypatch)
    registration = json.loads(prepared["registration"].read_text(encoding="ascii"))
    registration["schema_version"] = schema
    prepared["registration"].write_bytes(_json(registration))
    output = tmp_path / "bundle"
    calls: list[str] = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"legacy registration reached {name}")

        return fail

    monkeypatch.setattr(
        provider_materializer, "check_full_execution_readiness", forbidden("readiness")
    )
    monkeypatch.setattr(
        provider_materializer, "export_verified_provider_receipt", forbidden("export")
    )

    with pytest.raises((ProviderMaterializationError, continuity.CollectorContinuityError)):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert calls == []
    assert not output.exists()


def test_existing_output_rejects_before_snapshot_or_any_write(tmp_path, monkeypatch) -> None:
    inputs, _, staging = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"existing")
    before = (output.stat().st_ino, sentinel.read_bytes(), list(staging.iterdir()))
    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        lambda *args, **kwargs: pytest.fail("output collision reached snapshot"),
        raising=False,
    )

    with pytest.raises(ProviderMaterializationError, match=r"exist|collision"):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert (output.stat().st_ino, sentinel.read_bytes(), list(staging.iterdir())) == before


def test_collector_genesis_without_ledger_rejects_before_output_readiness_or_export(
    tmp_path, monkeypatch
) -> None:
    collector_dir = tmp_path / "collector"
    collector_dir.mkdir()
    prepared = _prepared(collector_dir, monkeypatch)
    with sqlite3.connect(prepared["database"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM forward_collector_genesis"
        ).fetchone() == (1,)
    prepared["ledger"].unlink()
    inputs = _inputs(tmp_path / "inputs")
    inputs["database_file"] = prepared["database"]
    inputs["registration_file"] = prepared["registration"]
    inputs["snapshot_staging_directory"] = tmp_path / "snapshots"
    output = tmp_path / "bundle"
    calls: list[str] = []

    def forbidden(name):
        def fail(*args, **kwargs):
            del args, kwargs
            calls.append(name)
            raise AssertionError(f"missing collector ledger reached {name}")

        return fail

    monkeypatch.setattr(provider_materializer, "_read_regular", forbidden("read"))
    monkeypatch.setattr(
        provider_materializer,
        "check_full_execution_readiness",
        forbidden("readiness"),
    )
    monkeypatch.setattr(
        provider_materializer,
        "export_verified_provider_receipt",
        forbidden("export"),
    )

    with pytest.raises(
        ProviderMaterializationError,
        match=r"collector.*ledger|ledger.*missing|cannot be opened safely",
    ):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert calls == []
    assert not output.exists()


def test_locked_collector_genesis_without_ledger_never_falls_through_to_materialization(
    tmp_path, monkeypatch
) -> None:
    collector_dir = tmp_path / "collector"
    collector_dir.mkdir()
    prepared = _prepared(collector_dir, monkeypatch)
    with sqlite3.connect(prepared["database"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM forward_collector_genesis"
        ).fetchone() == (1,)
    retained_ledger = prepared["ledger"].with_name("retained-ledger.jsonl")
    prepared["ledger"].rename(retained_ledger)
    inputs = _inputs(tmp_path / "inputs")
    inputs["database_file"] = prepared["database"]
    inputs["registration_file"] = prepared["registration"]
    inputs["snapshot_staging_directory"] = tmp_path / "snapshots"
    output = tmp_path / "bundle"
    calls: list[str] = []

    def forbidden(name):
        def fail(*args, **kwargs):
            del args, kwargs
            calls.append(name)
            raise AssertionError(f"locked missing-ledger collector reached {name}")

        return fail

    monkeypatch.setattr(provider_materializer, "_read_regular", forbidden("read"))
    monkeypatch.setattr(
        provider_materializer,
        "check_full_execution_readiness",
        forbidden("readiness"),
    )
    monkeypatch.setattr(
        provider_materializer,
        "export_verified_provider_receipt",
        forbidden("export"),
    )
    monkeypatch.setattr(
        provider_materializer, "_write_artifact", forbidden("write")
    )
    original_mkdir = provider_materializer.Path.mkdir

    def guarded_mkdir(path, *args, **kwargs):
        if path == output:
            calls.append("mkdir")
            raise AssertionError("locked missing-ledger collector created destination")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(provider_materializer.Path, "mkdir", guarded_mkdir)

    ready_reader, ready_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_reader)
        os.close(release_writer)
        connection = None
        try:
            connection = sqlite3.connect(
                prepared["database"], isolation_level=None, timeout=0
            )
            connection.execute("BEGIN EXCLUSIVE")
            os.write(ready_writer, b"L")
            if os.read(release_reader, 1) != b"R":
                os._exit(92)
            connection.rollback()
            connection.close()
            connection = None
            os._exit(0)
        except BaseException:
            os._exit(91)
        finally:
            if connection is not None:
                connection.close()

    os.close(ready_writer)
    os.close(release_reader)
    status = None
    try:
        ready = os.read(ready_reader, 1)
        os.close(ready_reader)
        assert ready == b"L"
        with pytest.raises(
            ProviderMaterializationError,
            match=r"collector.*ledger|ledger.*missing|cannot.*determin|cannot be opened safely",
        ):
            materialize_provider_bundle(output_dir=output, **inputs)
    finally:
        try:
            os.write(release_writer, b"R")
        except BrokenPipeError:
            pass
        os.close(release_writer)
        _, status = os.waitpid(child, 0)

    assert status is not None and os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert calls == []
    assert not output.exists()


def test_complete_collector_materializes_bundle_v2_from_one_snapshot_authority(
    tmp_path, monkeypatch
) -> None:
    inputs, prepared, staging = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    snapshot_api = getattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        None,
    )
    if not callable(snapshot_api):
        pytest.fail("materializer does not expose the task 4.1 snapshot authority")
    snapshot_calls: list[dict[str, object]] = []

    def snapshot_once(*args, **kwargs):
        result = snapshot_api(*args, **kwargs)
        snapshot_calls.append(result)
        return result

    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        snapshot_once,
    )
    original_read = provider_materializer._read_regular
    read_paths: list[Path] = []

    def read_regular(path, field):
        read_paths.append(Path(path).resolve())
        return original_read(path, field)

    monkeypatch.setattr(provider_materializer, "_read_regular", read_regular)
    original_readiness = provider_materializer.check_full_execution_readiness
    readiness_databases: list[Path] = []

    def check_readiness(database, **kwargs):
        readiness_databases.append(Path(database).resolve())
        return original_readiness(database, **kwargs)

    monkeypatch.setattr(
        provider_materializer, "check_full_execution_readiness", check_readiness
    )

    result = materialize_provider_bundle(output_dir=output, **inputs)
    bundle_file = Path(result["bundle_file"])
    bundle = json.loads(bundle_file.read_text(encoding="ascii"))

    assert len(snapshot_calls) == 1
    snapshot = snapshot_calls[0]
    snapshot_paths = {
        Path(snapshot[name]["path"]).resolve()
        for name in ("database", "registration", "ledger", "continuity_closure")
    }
    snapshot_database = Path(snapshot["database"]["path"]).resolve()
    assert prepared["database"].resolve() not in read_paths
    assert readiness_databases == [snapshot_database]
    assert Path(snapshot["staging_directory"]).parent == staging
    assert set(bundle) == BUNDLE_FIELDS
    assert bundle["schema_version"] == BUNDLE_SCHEMA
    expected = {
        "registration": (
            "stock-data-forward-panel-registration",
            "rqgm-forward-panel-registration/4",
        ),
        "ledger_snapshot": (
            "stock-data-forward-collector-ledger-snapshot",
            "stockdata-forward-collector-ledger-snapshot/1",
        ),
        "continuity_closure": (
            "stock-data-collector-continuity-closure",
            continuity.CLOSURE_SCHEMA,
        ),
    }
    locator_paths = []
    locator_references = []
    for field, (kind, schema) in expected.items():
        locator = bundle[field]
        assert set(locator) == {"reference", "path"}
        assert locator["reference"]["kind"] == kind
        assert locator["reference"]["schema_version"] == schema
        raw = Path(locator["path"]).read_bytes()
        assert locator["reference"]["identifier"] == hashlib.sha256(raw).hexdigest()
        locator_paths.append(Path(locator["path"]).resolve())
        locator_references.append(tuple(sorted(locator["reference"].items())))
    assert len(set(locator_paths)) == len(locator_paths)
    assert len(set(locator_references)) == len(locator_references)
    assert snapshot_paths.issuperset(locator_paths) or all(
        path.is_relative_to(output) for path in locator_paths
    )
    assert result["receipt"]["schema_version"] == "stockdata-rqgm-provider-export/1"
    assert result["receipt"]["ready"] is False
    assert not (
        {"registration", "ledger_snapshot", "continuity_closure"}
        & set(result["receipt"])
    )


def test_bundle_manifest_is_last_atomic_commit_after_internal_export(
    tmp_path, monkeypatch
) -> None:
    inputs, _, _ = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    export_name, original_export = _materializer_private_export_api()
    export_paths: list[Path] = []
    original_fsync = provider_materializer.os.fsync
    fsynced: list[tuple[int, int]] = []

    def fsync(descriptor):
        status = os.fstat(descriptor)
        fsynced.append((status.st_dev, status.st_ino))
        return original_fsync(descriptor)

    monkeypatch.setattr(provider_materializer.os, "fsync", fsync)

    def export_before_commit(*args, **kwargs):
        candidates = list(output.glob(".bundle-*.json"))
        assert len(candidates) == 1
        candidate = candidates[0]
        export_paths.append(candidate)
        assert candidate.parent == output
        assert candidate.name.startswith(".")
        assert candidate.name != "bundle.json"
        assert not (output / "bundle.json").exists()
        if export_name == "_export_verified_provider_receipt":
            assert args == (candidate,)
        else:
            assert candidate.read_bytes() == args[1]
        return original_export(*args, **kwargs)

    monkeypatch.setattr(
        provider_materializer,
        export_name,
        export_before_commit,
    )
    original_replace = provider_materializer.os.replace
    commits: list[tuple[Path, Path]] = []

    def replace(source, destination, *args, **kwargs):
        target = Path(destination)
        if target == output / "bundle.json":
            assert export_paths == [Path(source)]
            commits.append((Path(source), target))
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(provider_materializer.os, "replace", replace)

    result = materialize_provider_bundle(output_dir=output, **inputs)

    assert commits == [(export_paths[0], output / "bundle.json")]
    assert Path(result["bundle_file"]) == output / "bundle.json"
    assert not export_paths[0].exists()
    bundle_status = (output.joinpath("bundle.json").stat().st_dev, output.joinpath("bundle.json").stat().st_ino)
    output_status = (output.stat().st_dev, output.stat().st_ino)
    assert bundle_status in fsynced
    assert output_status in fsynced


def test_export_failure_removes_output_without_adopting_snapshot_staging(
    tmp_path, monkeypatch
) -> None:
    inputs, _, staging = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    export_name, _ = _materializer_private_export_api()
    monkeypatch.setattr(
        provider_materializer,
        export_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("injected export")),
    )

    with pytest.raises(ValueError, match="injected export"):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert not output.exists()
    snapshots = list(staging.iterdir())
    assert len(snapshots) == 1
    assert snapshots[0].name.startswith(".collector-snapshot-")
    assert snapshots[0].is_dir()


def test_sigkill_after_private_export_never_publishes_hidden_manifest(
    tmp_path, monkeypatch, capsys
) -> None:
    private_export = _private_export_api()
    inputs, prepared, _ = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    child = os.fork()
    if child == 0:
        original_replace = provider_materializer.os.replace

        def crash_before_publish(source, destination, *args, **kwargs):
            if Path(destination) == output / "bundle.json":
                os.kill(os.getpid(), signal.SIGKILL)
            return original_replace(source, destination, *args, **kwargs)

        provider_materializer.os.replace = crash_before_publish
        try:
            materialize_provider_bundle(output_dir=output, **inputs)
        except BaseException:
            os._exit(91)
        os._exit(92)

    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert not (output / "bundle.json").exists()
    hidden = list(output.glob(".bundle-*.json"))
    assert len(hidden) == 1
    receipt = private_export(hidden[0])
    assert receipt["schema_version"] == "stockdata-rqgm-provider-export/1"

    with pytest.raises(ValueError, match=r"bundle\.json|published|basename"):
        export_verified_provider_receipt(hidden[0])
    with pytest.raises(ValueError, match=r"bundle\.json|published|basename"):
        main(["rqgm-provider-export", "--bundle-file", str(hidden[0])])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert prepared["ledger"].is_file()
    with continuity.acquire_collector_phase_lease(prepared["ledger"]):
        pass


def test_snapshot_authority_hardlink_aliases_reject_before_downstream_or_output(
    tmp_path, monkeypatch
) -> None:
    inputs, _, staging_parent = _collector_inputs(tmp_path, monkeypatch)
    staging = staging_parent / (".collector-snapshot-" + "a" * 64)
    staging.mkdir(mode=0o700)
    raws = {
        "database": b"snapshot-database",
        "registration": _json(
            {"schema_version": "rqgm-forward-panel-registration/4"}
        ),
        "ledger": b"snapshot-ledger\n",
        "continuity_closure": _json(
            {
                "schema_version": (
                    "stockdata-forward-collector-continuity-closure/1"
                )
            }
        ),
    }
    identifiers = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in raws.items()
    }
    paths = {name: staging / identifier for name, identifier in identifiers.items()}
    paths["database"].write_bytes(raws["database"])
    for name in ("registration", "ledger", "continuity_closure"):
        os.link(paths["database"], paths[name])
    identities = {
        (path.stat().st_dev, path.stat().st_ino) for path in paths.values()
    }
    assert len(identities) == 1
    snapshot = {
        "staging_directory": str(staging),
        "database": {
            "path": str(paths["database"]),
            "reference": {
                "kind": "stock-data-database",
                "identifier": identifiers["database"],
                "schema_version": DATABASE_SCHEMA,
            },
        },
        "registration": {
            "path": str(paths["registration"]),
            "sha256": identifiers["registration"],
        },
        "ledger": {
            "path": str(paths["ledger"]),
            "sha256": identifiers["ledger"],
        },
        "continuity_closure": {
            "path": str(paths["continuity_closure"]),
            "reference": {
                "kind": "stock-data-collector-continuity-closure",
                "identifier": identifiers["continuity_closure"],
                "schema_version": (
                    "stockdata-forward-collector-continuity-closure/1"
                ),
            },
        },
    }
    monkeypatch.setattr(
        provider_materializer,
        "create_registered_collector_materialization_snapshot",
        lambda *args, **kwargs: snapshot,
    )

    def read_snapshot(path, field, *, expected_sha256):
        del field
        name = next(
            name for name, identifier in identifiers.items() if identifier == expected_sha256
        )
        status = Path(path).stat()
        return raws[name], (status.st_dev, status.st_ino)

    monkeypatch.setattr(
        provider_materializer, "_read_snapshot_artifact", read_snapshot
    )
    downstream: list[str] = []
    monkeypatch.setattr(
        provider_materializer,
        "_canonical_panel",
        lambda *args, **kwargs: downstream.append("panel")
        or pytest.fail("snapshot alias reached downstream reads"),
    )
    output = tmp_path / "bundle"

    with pytest.raises(ProviderMaterializationError, match=r"alias|identity|inode"):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert downstream == []
    assert not output.exists()


def test_materializer_semantic_failure_follows_candidate_readiness_and_cleans_hidden_bundle(
    tmp_path, monkeypatch
) -> None:
    inputs, _, staging = _collector_inputs(tmp_path, monkeypatch)
    output = tmp_path / "bundle"
    calls: list[str] = []

    def reject_semantic(*args, **kwargs):
        del args, kwargs
        calls.append("semantic")
        raise continuity.CollectorContinuityError("injected snapshot semantic failure")

    monkeypatch.setattr(
        provider_export,
        "verify_registered_collector_materialization_snapshot",
        reject_semantic,
        raising=False,
    )
    original_materializer_readiness = (
        provider_materializer.check_full_execution_readiness
    )
    monkeypatch.setattr(
        provider_materializer,
        "check_full_execution_readiness",
        lambda *args, **kwargs: calls.append("materializer-readiness")
        or original_materializer_readiness(*args, **kwargs),
    )
    monkeypatch.setattr(
        provider_export,
        "verify_bound_readiness",
        lambda *args, **kwargs: calls.append("readiness")
        or pytest.fail("semantic failure reached export readiness"),
    )

    with pytest.raises(
        (ValueError, ProviderMaterializationError, continuity.CollectorContinuityError),
        match="semantic|continuity",
    ):
        materialize_provider_bundle(output_dir=output, **inputs)

    assert calls == ["materializer-readiness", "semantic"]
    assert not output.exists()
    assert len(list(staging.iterdir())) == 1
