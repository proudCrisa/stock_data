from __future__ import annotations

import base64
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from fund_flow_fixture import (
    _calendar_inputs,
    _publish,
    make_signed_fund_flow_fixture,
)
from stockdata.authority import (
    AUTHORITY_COMPONENT_ROLES,
    SUPPORTED_AUTHORITY_COMPONENT_ROLES,
)
from stockdata.formal_fund_flow import (
    build_fund_flow_authority_inputs,
    verify_formal_fund_flow,
)


SYMBOLS = (
    "159980.SZ",
    "159992.SZ",
    "512480.SH",
    "512800.SH",
    "512880.SH",
)


def test_fund_flow_is_optional_for_existing_registration_role_coverage():
    assert "fund_flow" not in AUTHORITY_COMPONENT_ROLES
    assert SUPPORTED_AUTHORITY_COMPONENT_ROLES == AUTHORITY_COMPONENT_ROLES | {
        "fund_flow"
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value))


def _verify(fixture, payload=None, **overrides):
    return verify_formal_fund_flow(
        fixture["payload"] if payload is None else payload,
        expected_registry_sha256=overrides.get(
            "expected_registry_sha256", fixture["expected_registry_sha256"]
        ),
        provider_manifest_sha256=overrides.get(
            "provider_manifest_sha256", fixture["provider_manifest_sha256"]
        ),
        asof=overrides.get("asof", fixture["asof"]),
        decision_cutoff=overrides.get("decision_cutoff", fixture["decision_cutoff"]),
        expected_symbols=overrides.get("expected_symbols", fixture["expected_symbols"]),
    )


def test_real_signed_fixture_returns_only_raw_semantics_and_bindings(
    tmp_path, monkeypatch
):
    values = {
        symbol: [0, -1, *(float(index) for index in range(2, 80))] for symbol in SYMBOLS
    }
    fixture = make_signed_fund_flow_fixture(
        tmp_path,
        monkeypatch,
        symbols=SYMBOLS,
        session_count=80,
        values_by_symbol=values,
    )

    verified = fixture["verified"]
    assert fixture["external_io_attempts"] == []
    assert verified.symbols == SYMBOLS
    assert len(verified.sessions) == 80
    assert set(verified.rows_by_symbol) == set(SYMBOLS)
    assert [row.main_net_flow for row in verified.rows_by_symbol[SYMBOLS[0]][:3]] == [
        0.0,
        -1.0,
        2.0,
    ]
    assert not hasattr(verified, "main_net_flow_20_by_symbol")
    assert (
        verified.bindings.provider_manifest_sha256
        == fixture["provider_manifest_sha256"]
    )
    assert len(verified.bindings.source_receipt_ids) == len(SYMBOLS)


def test_exact_minimum_signed_window_is_accepted(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    assert len(_verify(fixture).sessions) == 39


def test_utc_microsecond_z_cutoff_is_preserved_across_signed_context(
    tmp_path, monkeypatch
):
    cutoff = "2026-08-31T08:10:00.000000Z"
    fixture = make_signed_fund_flow_fixture(
        tmp_path,
        monkeypatch,
        symbols=SYMBOLS,
        session_count=39,
        decision_cutoff=cutoff,
    )
    verified = _verify(fixture)
    assert verified.bindings.decision_cutoff == cutoff
    assert {
        record["payload"]["decision_cutoff"]
        for record in fixture["payload"]["fund_flow"]["artifact"]["records"]
    } == {cutoff}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_session",
        "stale_session",
        "future_source_availability",
        "wrong_source",
        "wrong_source_code",
        "wrong_unit",
        "bool_value",
        "string_value",
        "nan_value",
        "huge_int_value",
        "artifact_tamper",
        "receipt_tamper",
        "signature_tamper",
        "signed_context_tamper",
    ],
)
def test_verifier_rejects_incomplete_or_drifted_closure(
    tmp_path, monkeypatch, mutation
):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    payload = deepcopy(fixture["payload"])
    source = payload["fund_flow"]["source_evidence"][SYMBOLS[0]]
    artifact = payload["fund_flow"]["artifact"]
    if mutation == "missing_session":
        source["response"]["rows"].pop()
    elif mutation == "stale_session":
        source["response"]["rows"][-1]["date"] = source["response"]["rows"][-2]["date"]
    elif mutation == "future_source_availability":
        source["observed_at"] = fixture["decision_cutoff"]
    elif mutation == "wrong_source":
        source["source"] = "other"
    elif mutation == "wrong_source_code":
        source["request"]["code"] = "sh512800"
    elif mutation == "wrong_unit":
        source["response"]["unit"] = "RMB"
    elif mutation == "bool_value":
        source["response"]["rows"][0]["MainNetFlow"] = False
    elif mutation == "string_value":
        source["response"]["rows"][0]["MainNetFlow"] = "1.0"
    elif mutation == "nan_value":
        source["response"]["rows"][0]["MainNetFlow"] = math.nan
    elif mutation == "huge_int_value":
        source["response"]["rows"][0]["MainNetFlow"] = 10**1000
    elif mutation == "artifact_tamper":
        artifact["records"][0]["payload"]["main_net_flow"] += 1
    elif mutation == "receipt_tamper":
        receipt = next(iter(payload["fund_flow"]["source_receipts"].values()))
        receipt["response_sha256"] = "f" * 64
    elif mutation == "signature_tamper":
        payload["fund_flow"]["authority_envelope"]["signature_base64"] = "A" * 88
    else:
        artifact["records"][0]["payload"]["provider_manifest_sha256"] = "b" * 64

    with pytest.raises(ValueError):
        _verify(fixture, payload)


def test_builder_rejects_short_window_and_future_observation(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    captures = list(fixture["payload"]["fund_flow"]["source_evidence"].values())
    future = deepcopy(captures)
    future[0]["observed_at"] = fixture["decision_cutoff"]
    kwargs = {
        "expected_symbols": SYMBOLS,
        "calendar_authority": fixture["calendar_authority"],
        "provider_manifest_sha256": fixture["provider_manifest_sha256"],
        "asof": fixture["asof"],
        "decision_cutoff": fixture["decision_cutoff"],
    }
    with pytest.raises(ValueError, match="outside finality window"):
        build_fund_flow_authority_inputs(future, **kwargs)

    with pytest.raises(ValueError, match="at least 39"):
        make_signed_fund_flow_fixture(
            tmp_path / "short", monkeypatch, symbols=SYMBOLS, session_count=38
        )


def test_validly_signed_calendar_gap_is_rejected_semantically(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    calendar_inputs = _calendar_inputs(
        SYMBOLS,
        fixture["verified"].sessions,
        observed_at=f"{fixture['asof']}T15:00:00+08:00",
        gap_after=10,
    )
    published = _publish(
        tmp_path,
        monkeypatch,
        name="calendar-gap",
        component="trading_calendar",
        inputs=calendar_inputs,
        registry=fixture["payload"]["registry"],
        registry_sha256=fixture["expected_registry_sha256"],
        signer=Ed25519PrivateKey.from_private_bytes(bytes([12]) * 32),
        effective_at=f"{fixture['asof']}T15:00:00+08:00",
        available_at=f"{fixture['asof']}T15:00:00+08:00",
        decision_cutoff=fixture["decision_cutoff"],
    )
    payload = deepcopy(fixture["payload"])
    payload["calendar"] = {
        **calendar_inputs,
        "authority_envelope": published.envelope,
    }

    with pytest.raises(ValueError, match="calendar session chain has a gap"):
        _verify(fixture, payload)


def test_verifier_rejects_external_identity_and_wrong_signed_role(
    tmp_path, monkeypatch
):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    with pytest.raises(ValueError):
        _verify(fixture, expected_registry_sha256="f" * 64)
    with pytest.raises(ValueError):
        _verify(fixture, decision_cutoff="2026-08-31T16:11:00+08:00")
    with pytest.raises(ValueError):
        _verify(fixture, expected_symbols=SYMBOLS[:-1])

    changed_manifest = deepcopy(fixture["payload"])
    changed_manifest["provider_manifest_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="signed source closure drifted"):
        _verify(
            fixture,
            changed_manifest,
            provider_manifest_sha256="b" * 64,
        )

    changed_cutoff = deepcopy(fixture["payload"])
    changed_cutoff["decision_cutoff"] = "2026-08-31T16:11:00+08:00"
    with pytest.raises(ValueError, match="signed source closure drifted"):
        _verify(
            fixture,
            changed_cutoff,
            decision_cutoff="2026-08-31T16:11:00+08:00",
        )

    changed_asof = deepcopy(fixture["payload"])
    changed_asof["asof"] = "2026-08-28"
    with pytest.raises(ValueError):
        _verify(fixture, changed_asof, asof="2026-08-28")

    payload = deepcopy(fixture["payload"])
    envelope = payload["fund_flow"]["authority_envelope"]
    envelope["payload"]["component_role"] = "trading_calendar"
    signer = Ed25519PrivateKey.from_private_bytes(bytes([12]) * 32)
    envelope["signature_base64"] = base64.b64encode(
        signer.sign(_canonical(envelope["payload"]))
    ).decode("ascii")
    with pytest.raises(ValueError, match="wrong component role"):
        _verify(fixture, payload)


def test_verifier_rejects_validly_signed_future_envelope(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path, monkeypatch, symbols=SYMBOLS, session_count=39
    )
    payload = deepcopy(fixture["payload"])
    envelope = payload["fund_flow"]["authority_envelope"]
    envelope["payload"]["available_at"] = fixture["decision_cutoff"]
    signer = Ed25519PrivateKey.from_private_bytes(bytes([12]) * 32)
    envelope["signature_base64"] = base64.b64encode(
        signer.sign(_canonical(envelope["payload"]))
    ).decode("ascii")
    with pytest.raises(ValueError, match="post-cutoff"):
        _verify(fixture, payload)


def _formal_fund_flow_cli(tmp_path: Path, fixture: dict[str, object]) -> list[str]:
    payload = fixture["payload"]
    assert isinstance(payload, dict)
    captures = [
        payload["fund_flow"]["source_evidence"][symbol]
        for symbol in fixture["expected_symbols"]
    ]
    registry = tmp_path / "registry.json"
    calendar = tmp_path / "calendar.json"
    capture_file = tmp_path / "captures.json"
    output = tmp_path / "formal-fund-flow.json"
    _write_json(registry, payload["registry"])
    _write_json(calendar, payload["calendar"])
    _write_json(capture_file, captures)

    command = [
        sys.executable,
        "-m",
        "stockdata.formal_fund_flow",
        "--captures",
        str(capture_file),
        "--calendar",
        str(calendar),
        "--registry",
        str(registry),
        "--expected-registry-sha256",
        fixture["expected_registry_sha256"],
        "--provider-manifest-sha256",
        fixture["provider_manifest_sha256"],
        "--asof",
        fixture["asof"],
        "--decision-cutoff",
        fixture["decision_cutoff"],
        "--signer-private-key-env",
        "STOCKDATA_TEST_FUND_FLOW_KEY",
        "--effective-at",
        f"{fixture['asof']}T15:00:00+08:00",
        "--available-at",
        f"{fixture['asof']}T15:00:00+08:00",
        "--output",
        str(output),
    ]
    for symbol in fixture["expected_symbols"]:
        command.extend(["--symbol", symbol])
    return command


def _offline_cli_env(
    tmp_path: Path,
    signer_key: str,
    *,
    fail_replace: Path | None = None,
) -> tuple[dict[str, str], Path, Path]:
    guard_dir = tmp_path / "offline-guard"
    guard_dir.mkdir()
    ledger = tmp_path / "external-io-attempts.log"
    marker = tmp_path / "offline-guard-loaded"
    ledger.write_text("", encoding="ascii")
    (guard_dir / "sitecustomize.py").write_text(
        """import os
import socket
import subprocess
from pathlib import Path

ledger = Path(os.environ["STOCKDATA_TEST_EXTERNAL_IO_LEDGER"])
Path(os.environ["STOCKDATA_TEST_OFFLINE_GUARD_LOADED"]).write_text(
    "loaded\\n", encoding="ascii"
)

def blocked(name):
    def deny(*args, **kwargs):
        with ledger.open("a", encoding="ascii") as handle:
            handle.write(name + "\\n")
        raise AssertionError(f"formal fund-flow CLI attempted external I/O: {name}")
    return deny

socket.getaddrinfo = blocked("socket.getaddrinfo")
socket.socket.__init__ = blocked("socket.socket.__init__")
socket.create_connection = blocked("socket.create_connection")
subprocess.Popen.__init__ = blocked("subprocess.Popen.__init__")
subprocess.run = blocked("subprocess.run")
os.system = blocked("os.system")
if hasattr(os, "posix_spawn"):
    os.posix_spawn = blocked("os.posix_spawn")
if hasattr(os, "posix_spawnp"):
    os.posix_spawnp = blocked("os.posix_spawnp")

fail_replace = os.environ.get("STOCKDATA_TEST_FAIL_REPLACE")
if fail_replace:
    real_replace = os.replace
    def replace(source, destination, *args, **kwargs):
        if str(destination) == fail_replace:
            raise OSError("forced final output replace failure")
        return real_replace(source, destination, *args, **kwargs)
    os.replace = replace
""",
        encoding="ascii",
    )
    env = os.environ.copy()
    env["STOCKDATA_TEST_FUND_FLOW_KEY"] = signer_key
    env["STOCKDATA_TEST_EXTERNAL_IO_LEDGER"] = str(ledger)
    env["STOCKDATA_TEST_OFFLINE_GUARD_LOADED"] = str(marker)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(guard_dir), env.get("PYTHONPATH")) if value
    )
    if fail_replace is not None:
        env["STOCKDATA_TEST_FAIL_REPLACE"] = str(fail_replace)
    return env, ledger, marker


def _run_offline_cli(
    command: list[str],
    *,
    tmp_path: Path,
    signer_key: str,
    fail_replace: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env, ledger, marker = _offline_cli_env(
        tmp_path, signer_key, fail_replace=fail_replace
    )
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert marker.read_text(encoding="ascii") == "loaded\n"
    assert ledger.read_text(encoding="ascii") == ""
    return completed


def test_module_cli_publishes_and_verifies_offline_fixture(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path / "fixture", monkeypatch, symbols=SYMBOLS, session_count=39
    )
    signer_key = os.environ["STOCKDATA_TEST_FUND_FLOW_KEY"]
    monkeypatch.undo()

    command = _formal_fund_flow_cli(tmp_path, fixture)
    completed = _run_offline_cli(
        command, tmp_path=tmp_path, signer_key=signer_key
    )

    assert completed.returncode == 0
    summary = json.loads(completed.stdout)
    output = Path(summary["output"])
    assert summary["sessions"] == 39
    verified = _verify(fixture, json.loads(output.read_text()))
    assert verified.symbols == SYMBOLS
    assert verified.bindings.registry_sha256 == fixture["expected_registry_sha256"]


def test_module_cli_fails_closed_on_drifted_capture(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path / "fixture", monkeypatch, symbols=SYMBOLS, session_count=39
    )
    signer_key = os.environ["STOCKDATA_TEST_FUND_FLOW_KEY"]
    monkeypatch.undo()

    command = _formal_fund_flow_cli(tmp_path, fixture)
    payload = fixture["payload"]
    assert isinstance(payload, dict)
    captures = [
        deepcopy(payload["fund_flow"]["source_evidence"][symbol])
        for symbol in fixture["expected_symbols"]
    ]
    captures[0]["source"] = "other"
    _write_json(tmp_path / "captures.json", captures)
    completed = _run_offline_cli(
        command, tmp_path=tmp_path, signer_key=signer_key
    )

    assert completed.returncode == 2
    assert "fund-flow capture source identity differs" in completed.stderr
    assert not (tmp_path / "formal-fund-flow.json").exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("registry", "registry"),
        ("calendar", "signature"),
        ("cutoff", "decision_cutoff"),
    ],
)
def test_module_cli_rejects_invalid_authority_wiring(
    tmp_path, monkeypatch, case, message
):
    fixture = make_signed_fund_flow_fixture(
        tmp_path / "fixture", monkeypatch, symbols=SYMBOLS, session_count=39
    )
    signer_key = os.environ["STOCKDATA_TEST_FUND_FLOW_KEY"]
    monkeypatch.undo()
    command = _formal_fund_flow_cli(tmp_path, fixture)

    if case == "registry":
        command[command.index("--expected-registry-sha256") + 1] = "0" * 64
    elif case == "calendar":
        calendar_path = tmp_path / "calendar.json"
        calendar = json.loads(calendar_path.read_text(encoding="ascii"))
        signature = calendar["authority_envelope"]["signature_base64"]
        calendar["authority_envelope"]["signature_base64"] = (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        _write_json(calendar_path, calendar)
    else:
        command[command.index("--decision-cutoff") + 1] = "2026-08-31T16:10:00"

    completed = _run_offline_cli(
        command, tmp_path=tmp_path, signer_key=signer_key
    )
    assert completed.returncode == 2
    assert message in completed.stderr
    assert not (tmp_path / "formal-fund-flow.json").exists()


def test_module_cli_replace_failure_preserves_existing_file(tmp_path, monkeypatch):
    fixture = make_signed_fund_flow_fixture(
        tmp_path / "fixture", monkeypatch, symbols=SYMBOLS, session_count=39
    )
    signer_key = os.environ["STOCKDATA_TEST_FUND_FLOW_KEY"]
    monkeypatch.undo()
    command = _formal_fund_flow_cli(tmp_path, fixture)
    output = tmp_path / "formal-fund-flow.json"
    output.write_text("existing\n", encoding="ascii")

    completed = _run_offline_cli(
        command,
        tmp_path=tmp_path,
        signer_key=signer_key,
        fail_replace=output,
    )
    assert completed.returncode == 1
    assert "forced final output replace failure" in completed.stderr
    assert output.read_text(encoding="ascii") == "existing\n"
    assert not list(tmp_path.glob(".formal-fund-flow.json.*.tmp"))
