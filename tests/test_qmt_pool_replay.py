from __future__ import annotations

import io
import json
import os
import urllib.error
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockdata import qmt_pool_replay as pool


CAPTURED = "2026-08-31 17:13:44"
OBSERVED = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _symbols() -> list[str]:
    return ["600519.SH", "000001.SZ"]


def _record(*, rows: int = 3) -> dict:
    capture_day = date(2026, 8, 31)
    dates = [
        (capture_day - timedelta(days=rows - offset - 1)).strftime("%Y%m%d")
        for offset in range(rows)
    ]
    length = rows
    return {
        "index": dates,
        "columns": {
            "open": [10.0 + offset for offset in range(length)],
            "high": [11.0 + offset for offset in range(length)],
            "low": [9.0 + offset for offset in range(length)],
            "close": [10.5 + offset for offset in range(length)],
            "volume": [100.0 + offset for offset in range(length)],
            "amount": [1000.0 + offset for offset in range(length)],
        },
    }


def _status(*, symbols: list[str] | None = None, generated: str = CAPTURED) -> dict:
    return {
        "account_sections": {}, "auth_required": True, "errors": [], "export_dir": "not-bound",
        "generated": generated, "latest_age_sec": 0.0, "latest_exists": True,
        "latest_mtime": generated, "python": "3.10", "server": "QmtExport/2.0",
        "symbols": _symbols() if symbols is None else symbols, "uptime_sec": 1.0,
    }


def _latest(*, symbols: list[str] | None = None, generated: str = CAPTURED, count: int = 3) -> dict:
    members = _symbols() if symbols is None else symbols
    return {
        "account": {}, "account_id": "not-bound", "errors": [], "generated": generated,
        "market": {symbol: _record(rows=count) for symbol in members},
        "request": {"count": count, "fields": list(pool.FIELDS), "period": "1d", "symbols": members},
    }


def _normalized() -> dict:
    return pool.normalize_qmt_pool_wire(_status(), _latest(), observed_at=OBSERVED)


def _reseal(artifact: dict) -> None:
    unsigned = {key: value for key, value in artifact.items() if key != "replay_sha256"}
    artifact["replay_sha256"] = pool._sha256(unsigned)


def test_normalizes_actual_qmtexport_wire_shape_and_drops_capture_day():
    normalized = _normalized()
    artifact = pool.seal_qmt_pool_replay(normalized, ["600519.SH"])

    assert normalized["normalized_generated_at"] == "2026-08-31T17:13:44+08:00"
    assert normalized["available_at"] == "2026-08-31T10:00:00+00:00"
    assert artifact["schema_version"] == "qmt-pool-sealed-replay/1"
    assert artifact["authority_grade"] == "shadow"
    assert artifact["decision_eligible"] is False
    assert artifact["decision_authority"] is False
    assert artifact["actions"] == []
    assert artifact["permitted_uses"] == ["offline_replay"]
    assert artifact["source_authentication"] == "shared_token_unverified"
    assert artifact["pool_receipt"]["service"] == "QmtExport/2.0"
    assert artifact["pool_receipt"]["symbol_count"] == 2
    assert artifact["pool_receipt"]["source_request"] == {
        "count": 3, "fields": list(pool.FIELDS), "period": "1d", "symbols": ["000001.SZ", "600519.SH"],
    }
    product = artifact["products"][0]
    assert product["source_row_count"] == 3
    assert product["accepted_row_count"] == 2
    assert product["dropped_capture_day_count"] == 1
    assert [row["date"] for row in product["rows"]] == ["2026-08-29", "2026-08-30"]
    assert pool.verify_qmt_pool_replay(artifact) == artifact


def test_1300_source_rows_seal_1299_prior_day_rows():
    latest = _latest(count=1300)
    artifact = pool.seal_qmt_pool_replay(
        pool.normalize_qmt_pool_wire(_status(), latest, observed_at=OBSERVED), ["600519.SH"]
    )

    product = artifact["products"][0]
    assert product["source_row_count"] == 1300
    assert product["accepted_row_count"] == 1299
    assert product["dropped_capture_day_count"] == 1


@pytest.mark.parametrize("mutate, message", [
    (lambda status, latest: status.__setitem__("server", "other"), "status contract"),
    (lambda status, latest: status.__setitem__("errors", ["error"]), "status contract"),
    (lambda status, latest: status.__setitem__("auth_required", False), "status contract"),
    (lambda status, latest: latest.__setitem__("errors", ["error"]), "latest contract"),
    (lambda status, latest: latest.__setitem__("generated", "2026-08-31 17:13:45"), "watermarks"),
    (lambda status, latest: latest["request"].__setitem__("fields", ["close"]), "source request"),
    (lambda status, latest: latest["market"].pop("000001.SZ"), "membership"),
])
def test_normalizer_rejects_wire_contract_drift(mutate, message):
    status, latest = _status(), _latest()
    mutate(status, latest)
    with pytest.raises(pool.QmtPoolReplayError, match=message):
        pool.normalize_qmt_pool_wire(status, latest, observed_at=OBSERVED)


@pytest.mark.parametrize("mutate, message", [
    (lambda latest: latest["market"]["600519.SH"]["index"].__setitem__(2, "20260901"), "source dates"),
    (lambda latest: latest["market"]["600519.SH"]["index"].__setitem__(1, "20260829"), "source dates"),
    (lambda latest: latest["market"]["600519.SH"]["columns"]["close"].pop(), "columns"),
    (lambda latest: latest["market"]["600519.SH"]["columns"]["close"].__setitem__(0, float("nan")), "finite"),
    (lambda latest: latest["market"]["600519.SH"]["columns"]["low"].__setitem__(0, 11.0), "OHLCV"),
])
def test_validates_all_source_rows_before_capture_day_filter(mutate, message):
    latest = _latest()
    mutate(latest)
    with pytest.raises(pool.QmtPoolReplayError, match=message):
        pool.seal_qmt_pool_replay(
            pool.normalize_qmt_pool_wire(_status(), latest, observed_at=OBSERVED), ["600519.SH"]
        )


class _Response:
    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum: int) -> bytes:
        return self.raw[:maximum]


def test_client_uses_only_read_only_paths_token_and_byte_cap(monkeypatch):
    client = pool.QmtPoolReplayClient(token="test")
    payloads = iter([_status(), _latest()])
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return _Response(json.dumps(next(payloads)).encode())

    client._opener = Opener()
    artifact = client.capture(["600519.SH"])
    assert artifact["products"][0]["symbol"] == "600519.SH"
    assert [request.full_url.rsplit(":8000", 1)[1] for request, _ in calls] == ["/", "/latest"]
    assert all(timeout == pool.HTTP_TIMEOUT_SECONDS == 15.0 for _, timeout in calls)
    assert all(request.get_method() == "GET" and request.get_header("X-token") == "test" for request, _ in calls)

    monkeypatch.setattr(pool, "MAX_RESPONSE_BYTES", 8)
    client._opener = type("Opener", (), {"open": lambda self, request, timeout: _Response(b"123456789")})()
    with pytest.raises(pool.QmtPoolReplayError, match="memory cap"):
        client._get("/")

    client._opener = type("Opener", (), {
        "open": lambda self, request, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(request.full_url, 302, "redirect", {}, io.BytesIO())
        ),
    })()
    with pytest.raises(pool.QmtPoolReplayError, match="HTTP 302"):
        client._get("/")


def test_content_addressed_writer_is_idempotent_and_rejects_protected_root(tmp_path):
    artifact = pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"])
    first = pool.write_qmt_pool_replay(tmp_path, artifact)
    assert pool.write_qmt_pool_replay(tmp_path, artifact) == first
    with pytest.raises(pool.QmtPoolReplayError, match="protected"):
        pool.write_qmt_pool_replay(Path(pool.__file__).resolve().parents[1], artifact)
    with pytest.raises(pool.QmtPoolReplayError, match="protected"):
        pool._safe_output_root("/")


def test_writer_rejects_output_root_replaced_before_dirfd_open(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    output_root.mkdir()
    moved = tmp_path / "output-before-swap"
    original = pool.os.open
    swapped = False

    def swap_on_open(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == "/":
            swapped = True
            os.rename(output_root, moved)
            output_root.mkdir()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pool.os, "open", swap_on_open)
    with pytest.raises(pool.QmtPoolReplayError, match="root identity changed"):
        pool._safe_output_root(output_root)
    assert moved.is_dir()
    assert output_root.is_dir()


def test_writer_rejects_target_swap_without_deleting_replacement(tmp_path, monkeypatch):
    artifact = pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"])
    target = tmp_path / f"{artifact['replay_sha256']}.json"
    moved = tmp_path / "created-before-swap.json"
    original = pool.os.write
    swapped = False

    def swap_on_write(descriptor, raw):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(target, moved)
            target.write_bytes(b"replacement")
        return original(descriptor, raw)

    monkeypatch.setattr(pool.os, "write", swap_on_write)
    with pytest.raises(pool.QmtPoolReplayError, match="identity changed"):
        pool.write_qmt_pool_replay(tmp_path, artifact)
    assert target.read_bytes() == b"replacement"
    assert moved.exists()


def test_writer_rejects_existing_file_swap_during_safe_read(tmp_path, monkeypatch):
    artifact = pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"])
    target = pool.write_qmt_pool_replay(tmp_path, artifact)
    moved = tmp_path / "existing-before-swap.json"
    original = pool.os.read
    swapped = False

    def swap_on_read(descriptor, maximum):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(target, moved)
            target.write_bytes(pool._canonical(artifact))
        return original(descriptor, maximum)

    monkeypatch.setattr(pool.os, "read", swap_on_read)
    with pytest.raises(pool.QmtPoolReplayError, match="changed during read"):
        pool.write_qmt_pool_replay(tmp_path, artifact)
    assert target.read_bytes() == pool._canonical(artifact)
    assert moved.exists()


def test_verifier_rejects_resealed_multiple_capture_day_count():
    artifact = deepcopy(pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"]))
    product = artifact["products"][0]
    product["source_row_count"] = product["accepted_row_count"] + 2
    product["dropped_capture_day_count"] = 2
    content = {key: value for key, value in product.items() if key != "content_sha256"}
    product["content_sha256"] = pool._sha256(content)
    _reseal(artifact)

    with pytest.raises(pool.QmtPoolReplayError, match="counts"):
        pool.verify_qmt_pool_replay(artifact)


def test_verifier_rejects_resealed_unauthenticated_receipt():
    artifact = deepcopy(pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"]))
    artifact["pool_receipt"]["auth_required"] = False
    _reseal(artifact)

    with pytest.raises(pool.QmtPoolReplayError, match="receipt"):
        pool.verify_qmt_pool_replay(artifact)


@pytest.mark.parametrize("field, value", [
    ("source_generated", "2030-01-01 00:00:00"),
    ("available_at", "2030-01-01T00:00:00+00:00"),
])
def test_verifier_rejects_resealed_future_receipt_timestamps(field, value):
    artifact = deepcopy(pool.seal_qmt_pool_replay(_normalized(), ["600519.SH"]))
    artifact["pool_receipt"][field] = value
    if field == "source_generated":
        artifact["pool_receipt"]["normalized_generated_at"] = "2030-01-01T00:00:00+08:00"
        artifact["generated_at"] = "2030-01-01T00:00:00+08:00"
    else:
        artifact["available_at"] = value
    _reseal(artifact)

    with pytest.raises(pool.QmtPoolReplayError, match="future"):
        pool.verify_qmt_pool_replay(artifact)


def test_source_and_tests_do_not_name_mutating_or_unapproved_pool_paths():
    source = (
        Path(pool.__file__).read_text(encoding="utf-8")
        + (Path(pool.__file__).parents[1] / "scripts" / "capture_qmt_pool_replay.py").read_text(encoding="utf-8")
        + Path(__file__).read_text(encoding="utf-8")
    ).lower()
    forbidden = ("p" + "ost", "/" + "request", "/" + "fulldata")
    assert all(value not in source for value in forbidden)
