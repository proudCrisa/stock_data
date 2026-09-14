import base64
from copy import deepcopy
import io
import json
import urllib.error
from datetime import date, timedelta

import pytest

from stockdata import qmt_fulldata_shadow_capture as qmt


SYMBOL = "561980.SH"


def _request():
    return qmt.build_qmt_fulldata_request(symbol=SYMBOL, start="2026-08-01",
                                          end="2026-09-11", count=21,
                                          adjustment="raw")


def _response(request, identifier="job-1"):
    days = [(date(2026, 9, 11) - timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(20, -1, -1)]
    return {"id": identifier, "status": "ok", "type": "history_kline",
            "dividend_type": "none", "data": {SYMBOL: {"dividend_type": "none",
            "index": days, "columns": {"open": [10.0] * 21, "high": [11.0] * 21,
            "low": [9.0] * 21, "close": [10.5] * 21, "volume": [100.0] * 21,
            "amount": [105000.0] * 21}}}}


class _Response:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.raw[:limit]

    def getcode(self):
        return 200


def _client(monkeypatch, values):
    monkeypatch.setenv("QMT_TOKEN", "test-token")
    client = qmt.QmtFulldataShadowCaptureClient()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return _Response(value)

    client._opener = Opener()
    return client, calls


def test_capture_is_bound_shadow_only_and_content_addressed(tmp_path, monkeypatch):
    request = _request()
    response = _response(request, "job-1")
    client, calls = _client(monkeypatch, iter([
        b'{"id":"job-1","extra":"retained"}', json.dumps(response).encode(),
    ]))
    times = iter([qmt.datetime(2026, 9, 11, 9, tzinfo=qmt.timezone.utc),
                  qmt.datetime(2026, 9, 11, 10, tzinfo=qmt.timezone.utc),
                  qmt.datetime(2026, 9, 11, 11, tzinfo=qmt.timezone.utc)])
    monkeypatch.setattr(qmt, "_now", lambda: next(times))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    capture = client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11",
                             count=21, adjustment="raw", wait_timeout=1)

    assert [item.get_method() for item in calls] == ["POST", "GET"]
    assert calls[0].data == (
        b'{"params":{"count":21,"dividend_type":"none","end":"20260911",'
        b'"period":"1d","start":"20260801","symbol":"561980.SH"},'
        b'"type":"history_kline"}'
    )
    assert calls[1].full_url.endswith("/fulldata/job-1")
    assert capture["decision_eligible"] is False and capture["actions"] == []
    assert capture["volume_unit"] == "unknown" and capture["finality"] == "unverified"
    assert capture["submit"]["ack"]["extra"] == "retained"
    assert capture["derived_bound_request"]["request_id"] == "job-1"
    assert capture["terminal"]["response"]["id"] == "job-1"
    assert capture["started_at"] <= capture["submitted_at"] <= capture["completed_at"]
    assert base64.b64decode(capture["terminal"]["response_raw_base64"]) == json.dumps(response).encode()
    path = qmt.write_qmt_fulldata_shadow_capture(tmp_path, capture)
    assert path.name == f"{capture['capture_sha256']}.json"
    assert json.loads(path.read_text()) == capture


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "https://127.0.0.1:8000",
                                      "http://8.8.8.8:8000"])
def test_client_requires_loopback_http_and_explicit_token(monkeypatch, base_url):
    monkeypatch.setenv("QMT_TOKEN", "test")
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="loopback|bare"):
        qmt.QmtFulldataShadowCaptureClient(base_url=base_url)
    monkeypatch.delenv("QMT_TOKEN")
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="QMT_TOKEN"):
        qmt.QmtFulldataShadowCaptureClient()


@pytest.mark.parametrize("mutate, message", [
    (lambda response: response.__setitem__("id", "other"), "binding"),
    (lambda response: response.__setitem__("extra", 1), "binding"),
    (lambda response: response["data"].__setitem__("000001.SZ", {}), "symbol"),
    (lambda response: response["data"][SYMBOL]["columns"].__setitem__("close", [float("nan")] * 21), "non-finite"),
    (lambda response: response["data"][SYMBOL]["index"].__setitem__(0, "20260931"), "dates"),
])
def test_terminal_identity_and_rows_fail_closed(monkeypatch, mutate, message):
    request, response = _request(), _response(_request(), "job-1")
    mutate(response)
    client, _ = _client(monkeypatch, iter([b'{"id":"job-1"}', json.dumps(response).encode()]))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match=message):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)


def test_only_404_is_pending_and_other_http_or_oversize_fails(monkeypatch):
    request, response = _request(), _response(_request(), "job-1")
    client, _ = _client(monkeypatch, iter([
        b'{"id":"job-1"}',
        urllib.error.HTTPError("http://127.0.0.1/fulldata/job-1", 404, "pending", {}, io.BytesIO()),
        json.dumps(response).encode(),
    ]))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    monkeypatch.setattr(qmt.time, "sleep", lambda _seconds: None)
    assert client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                          adjustment="raw", wait_timeout=1)["terminal"]["response"]["id"] == "job-1"

    client, _ = _client(monkeypatch, iter([
        urllib.error.HTTPError("http://127.0.0.1/fulldata", 500, "bad", {}, io.BytesIO()),
    ]))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError,
                       match=r"submit POST /fulldata HTTP 500 generic"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)

    client, _ = _client(monkeypatch, iter([b"x" * (qmt.MAX_RESPONSE_BYTES + 1)]))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="memory limit"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)


def test_rejects_unsafe_ack_duplicate_json_and_writer_conflict(tmp_path, monkeypatch):
    client, _ = _client(monkeypatch, iter([b'{"id":"../escape"}']))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="unsafe"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)

    client, _ = _client(monkeypatch, iter([b'{"id":".."}']))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="unsafe"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)

    client, _ = _client(monkeypatch, iter([b'{"id":"job","id":"other"}']))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="duplicate"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)

    capture = {"capture_sha256": "0" * 64}
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="identity"):
        qmt.write_qmt_fulldata_shadow_capture(tmp_path, capture)


@pytest.mark.parametrize("ack_raw, response", [
    (b'{"id":"job-1","secret":"test-token"}', None),
    (b'{"id":"job-1"}', "test-token"),
])
def test_capture_rejects_token_echo_before_writing(monkeypatch, ack_raw, response):
    request = _request()
    terminal = _response(request, response or "job-1")
    values = [ack_raw] if response is None else [ack_raw, json.dumps(terminal).encode()]
    client, _ = _client(monkeypatch, iter(values))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="token"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)


def test_request_date_count_and_adjustment_contract():
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="ISO"):
        qmt.build_qmt_fulldata_request(symbol=SYMBOL, start="20260801", end="2026-09-11",
                                       count=21, adjustment="raw")
    with pytest.raises(qmt.QmtFulldataShadowCaptureError, match="1..1300"):
        qmt.build_qmt_fulldata_request(symbol=SYMBOL, start="2026-08-01", end="2026-09-11",
                                       count=1301, adjustment="raw")
    assert _request()["params"]["dividend_type"] == "none"


def test_http500_diagnostics_are_phase_bound_redacted_and_never_retried(monkeypatch):
    request = _request()
    business = b'{"status":"failed","error":"history_window_unavailable","id":"job-1"}'
    client, calls = _client(monkeypatch, iter([
        b'{"id":"job-1"}',
        urllib.error.HTTPError("http://127.0.0.1/fulldata/job-1", 500, "ignored",
                               {}, io.BytesIO(business)),
    ]))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError,
                       match=r"poll GET /fulldata/job-1 HTTP 500 terminal_business_error "
                             r"status=failed error=history_window_unavailable id=job-1"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)
    assert [call.get_method() for call in calls] == ["POST", "GET"]

    client, _ = _client(monkeypatch, iter([
        urllib.error.HTTPError("http://127.0.0.1/fulldata", 500, "test-token",
                               {}, io.BytesIO(b'{"status":"failed","error":"test\\u002dtoken","id":"job-1"}')),
    ]))
    with pytest.raises(qmt.QmtFulldataShadowCaptureError,
                       match=r"submit POST /fulldata HTTP 500 generic") as raised:
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)
    assert "test-token" not in str(raised.value)

    client, _ = _client(monkeypatch, iter([
        b'{"id":"job-1"}',
        urllib.error.HTTPError("http://127.0.0.1/fulldata/job-1", 500, "ignored", {},
                               io.BytesIO(b"x" * (qmt.MAX_ERROR_BYTES + 1))),
    ]))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError,
                       match=r"poll GET /fulldata/job-1 HTTP 500 generic") as raised:
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)
    assert "ignored" not in str(raised.value)

    client, _ = _client(monkeypatch, iter([
        b'{"id":"job-1"}',
        urllib.error.HTTPError("http://127.0.0.1/fulldata/job-1", 500, "ignored", {},
                               io.BytesIO(b'{"status":"failed","error":"x","id":"other"}')),
    ]))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError,
                       match=r"poll GET /fulldata/job-1 HTTP 500 generic"):
        client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                       adjustment="raw", wait_timeout=1)


@pytest.mark.parametrize("mutate", [
    lambda capture: capture["submit"].__setitem__("request_raw_base64", base64.b64encode(b"{}").decode()),
    lambda capture: capture["submit"].__setitem__("request_sha256", "0" * 64),
    lambda capture: capture["submit"]["ack"].__setitem__("id", "other"),
    lambda capture: capture.__setitem__("authority_grade", "formal"),
    lambda capture: capture.__setitem__("started_at", "2026-09-11T09:00:00"),
    lambda capture: capture.__setitem__("completed_at", "2026-09-11T08:00:00+00:00"),
])
def test_writer_revalidates_wire_identity_and_time(tmp_path, monkeypatch, mutate):
    request, response = _request(), _response(_request(), "job-1")
    client, _ = _client(monkeypatch, iter([b'{"id":"job-1"}', json.dumps(response).encode()]))
    times = iter([qmt.datetime(2026, 9, 11, 9, tzinfo=qmt.timezone.utc),
                  qmt.datetime(2026, 9, 11, 10, tzinfo=qmt.timezone.utc),
                  qmt.datetime(2026, 9, 11, 11, tzinfo=qmt.timezone.utc)])
    monkeypatch.setattr(qmt, "_now", lambda: next(times))
    monkeypatch.setattr(qmt, "build_qmt_fulldata_request", lambda **_kwargs: request)
    capture = client.capture(symbol=SYMBOL, start="2026-08-01", end="2026-09-11", count=21,
                             adjustment="raw", wait_timeout=1)
    mutate(capture)
    with pytest.raises(qmt.QmtFulldataShadowCaptureError):
        qmt.write_qmt_fulldata_shadow_capture(tmp_path, capture)
