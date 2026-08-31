from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import urllib.error
from datetime import datetime, timezone

import pytest

from stockdata import qmt_transport_capture as qmt
from stockdata import qmt_daily_bar_product as qmt_product
from stockdata.qmt_daily_bar_product import (
    QmtDailyBarProductError,
    build_qmt_daily_bar_product,
    load_qmt_daily_bar_product,
    verify_qmt_daily_bar_product,
    write_qmt_daily_bar_product,
)


REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
OBSERVED = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _request(*, adjustment="raw"):
    return qmt.build_qmt_transport_request(
        ["600519.SH"], count=2, adjustment=adjustment, request_id=REQUEST_ID,
    )


def _snapshot(*, request=None, generated_at="2026-08-31T09:00:00+00:00"):
    request = request or _request()
    rows = [
        {"date": "2026-08-27", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 100.0, "amount": 1000.0},
        {"date": "2026-08-28", "open": 10.5, "high": 11.5, "low": 10.0,
         "close": 11.0, "volume": 120.0, "amount": 1320.0},
    ]
    dates = [row["date"] for row in rows]
    return {
        "schema_version": qmt.SCHEMA_VERSION,
        "request_id": request["request_id"],
        "request_sha256": qmt.request_sha256(request),
        "request": request,
        "producer_instance": "qmt-win-01",
        "qmt_build": "qmt-20260831",
        "xtquant_build": "xtquant-1.2.3",
        "generated_at": generated_at,
        "available_at": "2026-08-31T09:00:01+00:00",
        "volume_unit": "share",
        "amount_unit": "cny",
        "market": {
            "600519.SH": {
                "coverage": {
                    "status": "complete_available_history", "requested_count": 2,
                    "returned_count": 2, "start": dates[0], "end": dates[-1],
                },
                "finality": {
                    "status": "source_marked_final", "verification": "unverified",
                    "watermark": dates[-1],
                },
                "errors": [],
                "rows": rows,
                "rows_sha256": qmt._sha256(rows),
            },
        },
    }


def _capture(snapshot=None, request=None, **kwargs):
    return qmt.validate_qmt_transport_snapshot(
        snapshot or _snapshot(request=request), request or _request(),
        observed_at=OBSERVED, **kwargs,
    )


def test_v2_capture_binds_complete_request_and_is_shadow_only():
    capture = _capture()

    assert capture["schema_version"] == "qmt-transport-snapshot/2"
    assert capture["authority_grade"] == "shadow"
    assert capture["decision_eligible"] is False
    assert capture["actions"] == []
    assert capture["decision_authority"] is False
    assert capture["source_authentication"] == "shared_token_unverified"
    assert capture["permitted_uses"] == ["offline_replay", "shadow_compare"]
    assert capture["request_sha256"] == qmt.request_sha256(_request())
    assert capture["symbols"][0]["rows_sha256"] == \
        qmt._sha256(capture["symbols"][0]["rows"])


@pytest.mark.parametrize("mutation, message", [
    (lambda snapshot: snapshot.__setitem__("schema_version", "qmt-shadow-history/1"),
     "v1 snapshot"),
    (lambda snapshot: snapshot.__setitem__("request_id", str(__import__("uuid").uuid4())),
     "request binding"),
    (lambda snapshot: snapshot.__setitem__("request_sha256", "0" * 64),
     "request binding"),
    (lambda snapshot: snapshot["market"]["600519.SH"].__setitem__("errors", ["failed"]),
     "reports errors"),
    (lambda snapshot: snapshot.__setitem__("volume_unit", "lot"), "unit is ambiguous"),
    (lambda snapshot: snapshot.__setitem__("amount_unit", "yuan"), "unit is ambiguous"),
    (lambda snapshot: snapshot["market"]["600519.SH"].__setitem__(
        "finality", {"status": "unknown", "verification": "unverified", "watermark": "2026-08-28"}),
     "coverage or finality"),
])
def test_rejects_v1_cross_request_errors_units_and_finality(mutation, message):
    snapshot = _snapshot()
    mutation(snapshot)
    with pytest.raises(qmt.QmtTransportCaptureError, match=message):
        _capture(snapshot)


@pytest.mark.parametrize("mutation, message", [
    (lambda snapshot: snapshot["market"]["600519.SH"]["rows"].append(
        copy.deepcopy(snapshot["market"]["600519.SH"]["rows"][-1])), "exceeds"),
    (lambda snapshot: snapshot["market"]["600519.SH"]["rows"][0].__setitem__("close", 99.0),
     "OHLCV"),
    (lambda snapshot: snapshot["market"]["600519.SH"]["rows"][1].__setitem__("date", "2026-08-31"),
     "prior-day"),
    (lambda snapshot: snapshot["market"]["600519.SH"].__setitem__("rows_sha256", "0" * 64),
     "rows hash"),
])
def test_rejects_overcount_bad_ohlcv_same_day_row_and_row_hash(mutation, message):
    snapshot = _snapshot()
    mutation(snapshot)
    with pytest.raises(qmt.QmtTransportCaptureError, match=message):
        _capture(snapshot)


def test_accepts_nonempty_available_history_shorter_than_requested_count():
    snapshot = _snapshot()
    market = snapshot["market"]["600519.SH"]
    market["rows"] = market["rows"][:1]
    market["rows_sha256"] = qmt._sha256(market["rows"])
    market["coverage"] = {
        "status": "complete_available_history", "requested_count": 2,
        "returned_count": 1, "start": "2026-08-27", "end": "2026-08-27",
    }
    market["finality"]["watermark"] = "2026-08-27"
    assert _capture(snapshot)["symbols"][0]["coverage"]["returned_count"] == 1


def test_rejects_replay_and_qfq_parameter_drift():
    with pytest.raises(qmt.QmtTransportTimeout, match="replayed"):
        _capture(baseline_generated_at="2026-08-31T09:00:00+00:00")

    request = _request(adjustment="qfq")
    request["qmt_parameter"] = "none"
    with pytest.raises(qmt.QmtTransportCaptureError, match="request contract"):
        _capture(_snapshot(request=request), request)

    qfq_request = _request(adjustment="qfq")
    with pytest.raises(qmt.QmtTransportCaptureError, match="request binding"):
        _capture(_snapshot(), qfq_request)


def test_rejects_future_generation_and_availability_timestamps():
    snapshot = _snapshot(generated_at="2026-08-31T10:00:01+00:00")
    snapshot["available_at"] = "2026-08-31T10:00:02+00:00"
    with pytest.raises(qmt.QmtTransportCaptureError, match="in the future"):
        _capture(snapshot)


def test_content_addressed_snapshot_and_product_are_idempotent(tmp_path):
    capture = _capture()
    snapshot = qmt.write_qmt_transport_snapshot(tmp_path, capture)
    assert qmt.load_qmt_transport_snapshot(snapshot) == capture

    product = build_qmt_daily_bar_product(capture)
    assert product["data_product_id"].startswith("qmt-daily-bars:")
    assert product["version"] != product["content_hash"]
    assert product["schema_id"] == "ohlcv-daily/1"
    assert product["source_receipt_ids"] == [capture["snapshot_sha256"]]
    assert product["pit_mode"] == "current_observation"
    assert product["universe_version"] == "not_bound"
    assert product["trading_calendar_version"] == "not_bound"
    assert product["source_authentication"] == "shared_token_unverified"
    assert product["capture_closure"] == capture
    assert product["corporate_action_version"] == "not_bound"
    assert product["products"][0]["row_projection"] == {
        "capture_symbol": "600519.SH",
        "rows_sha256": capture["symbols"][0]["rows_sha256"],
    }
    output = write_qmt_daily_bar_product(tmp_path, product)
    assert output.name == f"{product['product_sha256']}.json"
    assert json.loads(output.read_text(encoding="ascii")) == product
    assert verify_qmt_daily_bar_product(product) == product

    product["actions"] = ["buy"]
    product["product_sha256"] = qmt._sha256(
        {key: value for key, value in product.items() if key != "product_sha256"}
    )
    with pytest.raises(QmtDailyBarProductError, match="does not match"):
        verify_qmt_daily_bar_product(product)

    product = build_qmt_daily_bar_product(capture)
    product["products"][0]["row_projection"]["rows_sha256"] = "0" * 64
    content = {
        key: value for key, value in product["products"][0].items()
        if key != "content_sha256"
    }
    product["products"][0]["content_sha256"] = qmt._sha256(content)
    product["product_sha256"] = qmt._sha256(
        {key: value for key, value in product.items() if key != "product_sha256"}
    )
    with pytest.raises(QmtDailyBarProductError, match="capture closure"):
        verify_qmt_daily_bar_product(product)

    product = build_qmt_daily_bar_product(capture)
    product["source_id"] = "forged"
    product["product_sha256"] = qmt._sha256(
        {key: value for key, value in product.items() if key != "product_sha256"}
    )
    with pytest.raises(QmtDailyBarProductError, match="does not match"):
        verify_qmt_daily_bar_product(product)

    raw = qmt._canonical(build_qmt_daily_bar_product(capture)).decode("ascii")
    duplicate = tmp_path / "duplicate-product.json"
    duplicate.write_text(raw.replace("{", '{"source_id":"forged",', 1), encoding="ascii")
    with pytest.raises(QmtDailyBarProductError, match="unreadable"):
        load_qmt_daily_bar_product(duplicate)


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.payload[:limit]


def test_client_only_uses_loopback_env_token_and_exact_ack(monkeypatch):
    monkeypatch.setenv("QMT_TRANSPORT_TOKEN", "test-token")
    with pytest.raises(qmt.QmtTransportCaptureError, match="loopback"):
        qmt.QmtTransportCaptureClient(base_url="http://example.com:8000")
    with pytest.raises(qmt.QmtTransportCaptureError, match="loopback"):
        qmt.QmtTransportCaptureClient(base_url="http://localhost:8000")

    client = qmt.QmtTransportCaptureClient(max_response_bytes=1024)
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return _Response(b'{"schema_version":"qmt-transport-snapshot/2"}')

    client._opener = Opener()
    assert client._json("/latest") == {"schema_version": qmt.SCHEMA_VERSION}
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8000/latest"
    assert request.get_header("X-token") == "test-token"
    assert timeout == 3.0

    request = _request()
    with pytest.raises(qmt.QmtTransportCaptureError, match="acknowledgement binding"):
        qmt._verify_ack(
            {"schema_version": qmt.ACK_SCHEMA_VERSION, "ok": True,
             "request_id": request["request_id"], "request_sha256": "0" * 64},
            request, qmt.request_sha256(request),
        )


def test_client_posts_bound_request_then_captures_new_v2_snapshot(monkeypatch):
    client = qmt.QmtTransportCaptureClient(token="test")
    posted = []
    baseline = {
        "schema_version": qmt.SCHEMA_VERSION,
        "generated_at": "2026-08-31T08:00:00+00:00",
    }

    def fake_json(path, *, method="GET", body=None):
        if path == "/latest" and not posted:
            return baseline
        if path == "/request":
            posted.append(body)
            return {
                "schema_version": qmt.ACK_SCHEMA_VERSION,
                "ok": True,
                "request_id": body["request_id"],
                "request_sha256": qmt.request_sha256(body),
            }
        return _snapshot(request=posted[0])

    monkeypatch.setattr(client, "_json", fake_json)
    capture = client.capture(["600519.SH"], count=2, wait_timeout=1)

    assert posted == [capture["request"]]
    assert capture["request_id"] == capture["request"]["request_id"]


def test_client_waits_through_foreign_v2_snapshot(monkeypatch):
    client = qmt.QmtTransportCaptureClient(token="test")
    posted = []
    foreign_request = qmt.build_qmt_transport_request(
        ["000001.SZ"], count=2, adjustment="raw",
        request_id="123e4567-e89b-12d3-a456-426614174001",
    )
    foreign = _snapshot(request=_request())
    foreign["request_id"] = foreign_request["request_id"]
    foreign["request_sha256"] = qmt.request_sha256(foreign_request)
    foreign["request"] = foreign_request
    latest = iter([
        {"schema_version": qmt.SCHEMA_VERSION, "generated_at": "2026-08-31T08:00:00+00:00"},
        foreign,
    ])

    def fake_json(path, *, method="GET", body=None):
        if path == "/request":
            posted.append(body)
            return {
                "schema_version": qmt.ACK_SCHEMA_VERSION, "ok": True,
                "request_id": body["request_id"],
                "request_sha256": qmt.request_sha256(body),
            }
        if not posted:
            return next(latest)
        if len(posted) == 1:
            posted.append("foreign_seen")
            return next(latest)
        return _snapshot(request=posted[0])

    monkeypatch.setattr(client, "_json", fake_json)
    monkeypatch.setattr(qmt.time, "sleep", lambda _seconds: None)
    capture = client.capture(["600519.SH"], count=2, wait_timeout=1)
    assert capture["request"]["symbols"] == ["600519.SH"]


def test_rejects_invalid_transport_limits():
    with pytest.raises(qmt.QmtTransportCaptureError, match="limits"):
        qmt.QmtTransportCaptureClient(token="test", max_response_bytes=1023)
    with pytest.raises(qmt.QmtTransportCaptureError, match="limits"):
        qmt.QmtTransportCaptureClient(token="test", request_timeout=float("inf"))


def test_client_rejects_oversize_duplicate_json_and_redirect(monkeypatch):
    client = qmt.QmtTransportCaptureClient(token="test", max_response_bytes=1024)
    client._opener = type("Opener", (), {
        "open": lambda self, request, timeout: _Response(b"0" * 1025),
    })()
    with pytest.raises(qmt.QmtTransportCaptureError, match="memory limit"):
        client._json("/latest")

    client = qmt.QmtTransportCaptureClient(token="test")
    client._opener = type("Opener", (), {
        "open": lambda self, request, timeout: _Response(b'{"a":1,"a":2}'),
    })()
    with pytest.raises(qmt.QmtTransportCaptureError, match="duplicate JSON key"):
        client._json("/latest")

    client._opener = type("Opener", (), {
        "open": lambda self, request, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(request.full_url, 302, "redirect", {}, io.BytesIO())
        ),
    })()
    with pytest.raises(qmt.QmtTransportCaptureError, match="HTTP 302"):
        client._json("/latest")


def test_writers_reject_protected_and_symlinked_roots(tmp_path, monkeypatch):
    capture = _capture()
    product = build_qmt_daily_bar_product(capture)
    monkeypatch.setattr(qmt, "_PROTECTED_OUTPUT_ROOTS", (tmp_path,))
    with pytest.raises(qmt.QmtTransportCaptureError, match="protected"):
        qmt.write_qmt_transport_snapshot(tmp_path, capture)
    with pytest.raises(QmtDailyBarProductError, match="protected"):
        write_qmt_daily_bar_product(tmp_path, product)

    monkeypatch.setattr(qmt, "_PROTECTED_OUTPUT_ROOTS", ())
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(qmt.QmtTransportCaptureError, match="not a directory"):
        qmt.write_qmt_transport_snapshot(link, capture)
    with pytest.raises(QmtDailyBarProductError, match="not a directory"):
        write_qmt_daily_bar_product(link, product)


def test_writer_rejects_an_existing_hard_link_target(tmp_path):
    capture = _capture()
    raw = qmt._canonical(capture)
    source = tmp_path / "source.json"
    source.write_bytes(raw)
    target = tmp_path / f"{capture['snapshot_sha256']}.json"
    os.link(source, target)
    with pytest.raises(qmt.QmtTransportCaptureError, match="conflicts"):
        qmt.write_qmt_transport_snapshot(tmp_path, capture)


def test_writer_rejects_swap_without_deleting_the_replacement(tmp_path, monkeypatch):
    capture = _capture()
    target = tmp_path / f"{capture['snapshot_sha256']}.json"
    moved = tmp_path / "created-before-swap.json"
    original = qmt._write_all

    def swap_after_create(descriptor, raw, error_type):
        os.rename(target, moved)
        target.write_bytes(b"replacement")
        original(descriptor, raw, error_type)

    monkeypatch.setattr(qmt, "_write_all", swap_after_create)
    with pytest.raises(qmt.QmtTransportCaptureError, match="entry identity"):
        qmt.write_qmt_transport_snapshot(tmp_path, capture)
    assert target.read_bytes() == b"replacement"
    assert moved.exists()


def test_writer_exception_cleanup_never_deletes_a_swapped_replacement(tmp_path, monkeypatch):
    capture = _capture()
    target = tmp_path / f"{capture['snapshot_sha256']}.json"
    moved = tmp_path / "created-before-error.json"

    def swap_then_fail(descriptor, raw, error_type):
        os.rename(target, moved)
        target.write_bytes(b"replacement")
        raise error_type("injected failure")

    monkeypatch.setattr(qmt, "_write_all", swap_then_fail)
    with pytest.raises(qmt.QmtTransportCaptureError, match="injected failure"):
        qmt.write_qmt_transport_snapshot(tmp_path, capture)
    assert target.read_bytes() == b"replacement"
    assert moved.exists()


def test_writer_rechecks_entry_after_file_fsync(tmp_path, monkeypatch):
    capture = _capture()
    target = tmp_path / f"{capture['snapshot_sha256']}.json"
    moved = tmp_path / "created-before-fsync.json"
    original = qmt.os.fsync

    def swap_at_file_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and target.exists():
            os.rename(target, moved)
            target.write_bytes(b"replacement")
        original(descriptor)

    monkeypatch.setattr(qmt.os, "fsync", swap_at_file_fsync)
    with pytest.raises(qmt.QmtTransportCaptureError, match="entry identity"):
        qmt.write_qmt_transport_snapshot(tmp_path, capture)
    assert target.read_bytes() == b"replacement"
    assert moved.exists()


def test_fd_loader_rejects_same_inode_same_size_rewrite(tmp_path, monkeypatch):
    capture = _capture()
    path = qmt.write_qmt_transport_snapshot(tmp_path, capture)
    original = qmt._read_exact

    def rewrite_after_read(descriptor, limit, error_type):
        raw = original(descriptor, limit, error_type)
        changed = bytes([raw[0] ^ 1]) + raw[1:]
        path.write_bytes(changed)
        return raw

    monkeypatch.setattr(qmt, "_read_exact", rewrite_after_read)
    with pytest.raises(qmt.QmtTransportCaptureError, match="identity changed during read"):
        qmt.load_qmt_transport_snapshot(path)


def test_product_id_is_stable_for_same_scope_and_size_cap_is_enforced(monkeypatch):
    capture = _capture()
    other_request = qmt.build_qmt_transport_request(
        ["600519.SH"], count=2, adjustment="raw",
        request_id="123e4567-e89b-12d3-a456-426614174002",
    )
    other_capture = _capture(_snapshot(request=other_request), other_request)
    product = build_qmt_daily_bar_product(capture)
    other = build_qmt_daily_bar_product(other_capture)
    assert product["data_product_id"] == other["data_product_id"]
    assert product["version"] != other["version"]

    monkeypatch.setattr(qmt_product, "MAX_PRODUCT_BYTES", 1)
    with pytest.raises(QmtDailyBarProductError, match="byte cap"):
        build_qmt_daily_bar_product(capture)


def test_product_id_is_independent_of_requested_symbol_order():
    request = qmt.build_qmt_transport_request(
        ["000001.SZ", "600519.SH"], count=2, adjustment="raw",
        request_id="123e4567-e89b-12d3-a456-426614174003",
    )
    reordered = qmt.build_qmt_transport_request(
        ["600519.SH", "000001.SZ"], count=2, adjustment="raw",
        request_id="123e4567-e89b-12d3-a456-426614174004",
    )
    def capture_for(value):
        snapshot = _snapshot(request=value)
        snapshot["market"]["000001.SZ"] = copy.deepcopy(
            snapshot["market"]["600519.SH"]
        )
        return _capture(snapshot, value)

    assert build_qmt_daily_bar_product(capture_for(request))["data_product_id"] == \
        build_qmt_daily_bar_product(capture_for(reordered))["data_product_id"]


def test_product_writer_supports_macos_absolute_temporary_directory():
    capture = _capture()
    with tempfile.TemporaryDirectory() as root:
        output = write_qmt_daily_bar_product(
            root, build_qmt_daily_bar_product(capture)
        )
    assert output.name.endswith(".json")


def test_capture_source_has_no_qmt_download_subscription_or_execution_api():
    source = (qmt.Path(qmt.__file__).read_text(encoding="utf-8")
              + (qmt.Path(qmt.__file__).parents[1] / "scripts" / "capture_qmt_transport.py")
              .read_text(encoding="utf-8")).lower()
    forbidden = (
        "xttrader", "download_history_data", "subscribe_quote", "passorder",
        "order_stock", "cancel_order_stock", "sqlite", "cache(",
    )
    assert all(token not in source for token in forbidden)
