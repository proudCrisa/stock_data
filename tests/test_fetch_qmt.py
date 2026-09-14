"""hermetic 测试:QMT 通道客户端与前复权日线同步(假传输,不打真实通道)。"""
from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import stockdata.fetch_qmt as fetch_qmt
from stockdata.cache import Cache
from stockdata.fetch_qmt import (
    ADJ_MODE,
    ADJ_VERSION,
    SOURCE,
    QmtChannelClient,
    QmtChannelError,
    _urllib_transport,
    load_qmt_token,
    parse_history_records,
    sync_qmt_daily,
)


def _payload(symbol: str, rows: list[tuple[str, dict]]) -> dict:
    """按通道真实线形(列式字典)构造 history_kline 返回。"""
    keys = ["open", "high", "low", "close", "volume", "suspendFlag"]
    columns = {k: [r.get(k) for _, r in rows] for k in keys}
    if all(v is None for v in columns["suspendFlag"]):
        columns.pop("suspendFlag")  # 通道无此字段时的形态
    return {
        "id": "req-1",
        "status": "ok",
        "data": {symbol: {"index": [d for d, _ in rows], "columns": columns}},
    }


def _bar(o=10.0, h=11.0, l=9.5, c=10.5, v=1000.0, **extra):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, **extra}


def _status(alive: bool = True, age: float = 30.0) -> dict:
    """满足 assert_ready 健康门的通道状态。"""
    return {"server": "QmtExport/2.0", "latest_exists": alive,
            "latest_age_sec": age}


def _client(routes: dict, calls: list | None = None):
    """routes: {(path, method): dict 或 callable(body)->dict}"""
    def transport(path, method, body):
        if calls is not None:
            calls.append((path, method))
        route = routes[(path, method)]
        return route(body) if callable(route) else route
    return QmtChannelClient(transport=transport, sleep=lambda _: None)


class TestLoadToken:
    def test_env_wins(self, tmp_path):
        assert load_qmt_token(env={"QMT_TOKEN": "abc"}) == "abc"

    def test_file_0600(self, tmp_path):
        f = tmp_path / "qmt-token"
        f.write_text("tok\n")
        os.chmod(f, 0o600)
        assert load_qmt_token(env={}, token_file=f) == "tok"

    def test_file_bad_perms_rejected(self, tmp_path):
        f = tmp_path / "qmt-token"
        f.write_text("tok")
        os.chmod(f, 0o644)
        with pytest.raises(QmtChannelError, match="0600"):
            load_qmt_token(env={}, token_file=f)

    def test_missing_everywhere(self, tmp_path):
        with pytest.raises(QmtChannelError, match="凭据"):
            load_qmt_token(env={}, token_file=tmp_path / "nope")


class TestClient:
    def test_is_alive(self):
        client = _client({("/", "GET"): {"latest_exists": True}})
        assert client.is_alive() is True

    def test_history_front_happy_path(self):
        calls = []
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        routes = {
            ("/fulldata", "POST"): lambda body: {"id": "req-1"},
            ("/fulldata/req-1", "GET"): payload,
        }
        client = _client(routes, calls)
        result = client.history_front("600519.SH")
        assert result["status"] == "ok"
        # 只触碰 /fulldata,绝无 /request(池替换)路径
        assert all("/request" not in p for p, _ in calls)

    def test_history_front_submit_without_id(self):
        client = _client({("/fulldata", "POST"): {"error": "busy"}})
        with pytest.raises(QmtChannelError, match="请求ID"):
            client.history_front("600519.SH")

    def test_history_front_error_status(self):
        routes = {
            ("/fulldata", "POST"): {"id": "r"},
            ("/fulldata/r", "GET"): {"id": "r", "status": "error",
                                     "error": "no local data"},
        }
        client = _client(routes)
        with pytest.raises(QmtChannelError, match="no local data"):
            client.history_front("600519.SH")

    def test_history_front_poll_404_then_ok(self):
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        polls = {"n": 0}

        def poll():
            polls["n"] += 1
            if polls["n"] == 1:
                raise QmtChannelError("HTTP 404: /fulldata/req-1", status=404)
            return payload

        routes = {("/fulldata", "POST"): {"id": "req-1"},
                  ("/fulldata/req-1", "GET"): lambda body: poll()}
        client = _client(routes)
        assert client.history_front("600519.SH")["status"] == "ok"
        assert polls["n"] == 2

    def test_history_front_ok_with_none_data_keeps_polling(self):
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        polls = {"n": 0}

        def poll():
            polls["n"] += 1
            if polls["n"] == 1:
                return {"id": "req-1", "status": "ok", "data": None}
            return payload

        routes = {("/fulldata", "POST"): {"id": "req-1"},
                  ("/fulldata/req-1", "GET"): lambda body: poll()}
        client = _client(routes)
        assert client.history_front("600519.SH")["status"] == "ok"
        assert polls["n"] == 2

    def test_history_front_timeout(self):
        routes = {
            ("/fulldata", "POST"): {"id": "r"},
            ("/fulldata/r", "GET"): {"pending": True},
        }
        client = _client(routes)
        with pytest.raises(QmtChannelError, match="超时"):
            client.history_front("600519.SH", timeout=0.01)

    def test_history_front_non404_poll_error_fails_fast(self):
        """401/500/连接断等错误立即抛出,不当 pending 干等满超时。"""
        polls = {"n": 0}

        def poll():
            polls["n"] += 1
            raise QmtChannelError("HTTP 401: /fulldata/r", status=401)

        routes = {("/fulldata", "POST"): {"id": "r"},
                  ("/fulldata/r", "GET"): lambda body: poll()}
        client = _client(routes)
        with pytest.raises(QmtChannelError, match="401"):
            client.history_front("600519.SH", timeout=60)
        assert polls["n"] == 1  # 未重试


class _RedirectTarget(BaseHTTPRequestHandler):
    seen_token = None

    def do_GET(self):
        _RedirectTarget.seen_token = self.headers.get("X-Token")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class TestTransportHardening:
    def _serve(self, handler_cls):
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_non_loopback_base_rejected(self):
        with pytest.raises(QmtChannelError, match="loopback"):
            _urllib_transport("http://evil.example.com", "tok")

    def test_redirect_not_followed_token_not_leaked(self):
        target = self._serve(_RedirectTarget)
        target_port = target.server_address[1]

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location",
                                 f"http://127.0.0.1:{target_port}/stolen")
                self.end_headers()

            def log_message(self, *args):
                pass

        redirector = self._serve(Redirector)
        port = redirector.server_address[1]
        try:
            transport = _urllib_transport(f"http://127.0.0.1:{port}", "sekrit")
            with pytest.raises(QmtChannelError) as exc_info:
                transport("/", "GET", None)
            assert exc_info.value.status == 302
            assert _RedirectTarget.seen_token is None  # token 未随重定向外泄
        finally:
            redirector.shutdown()
            target.shutdown()

    def test_oversized_response_rejected(self, monkeypatch):
        class BigBody(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "64")
                self.end_headers()
                self.wfile.write(b"x" * 64)

            def log_message(self, *args):
                pass

        server = self._serve(BigBody)
        monkeypatch.setattr(fetch_qmt, "_MAX_RESPONSE_BYTES", 16)
        try:
            transport = _urllib_transport(
                f"http://127.0.0.1:{server.server_address[1]}", "tok")
            with pytest.raises(QmtChannelError, match="上限"):
                transport("/", "GET", None)
        finally:
            server.shutdown()


class TestParse:
    def test_valid_rows_sorted(self):
        payload = _payload("X", [("2026-09-11", _bar(c=11.0)),
                                 ("2026-09-10", _bar())])
        bars, suspended, invalid, _np = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-10", "2026-09-11"]
        assert suspended == set() and invalid == []

    def test_compact_and_epoch_dates(self):
        epoch_ms = 1789689600000  # 2026-09-18T08:00+08
        payload = _payload("X", [("20260910", _bar()), (epoch_ms, _bar())])
        bars, _, invalid, _np = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-10", "2026-09-18"]
        assert invalid == []

    def test_epoch_converted_through_shanghai_not_utc(self):
        """上海子夜 epoch(2026-09-18 00:00+08 = 09-17 16:00Z)必须落 09-18。"""
        shanghai_midnight_ms = 1789660800000
        payload = _payload("X", [(shanghai_midnight_ms, _bar())])
        bars, _, invalid, _np = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-18"]
        assert invalid == []

    def test_suspend_flag_and_empty_volume(self):
        payload = _payload("X", [
            ("2026-09-10", _bar()),
            ("2026-09-11", _bar(v=None)),
            ("2026-09-14", _bar(suspendFlag=True)),
            ("2026-09-15", _bar(v=0.0)),
        ])
        bars, suspended, invalid, _np = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-10"]
        assert suspended == {"2026-09-11", "2026-09-14", "2026-09-15"}

    @pytest.mark.parametrize("row", [
        _bar(c=0.0),                    # 非正价
        _bar(h=9.0, l=10.0),            # high < low
        _bar(o=12.0, h=11.0),           # open > high
        _bar(v=-1.0),                   # 负量
        _bar(c=float("nan")),           # 非有限
    ])
    def test_invalid_rows_rejected(self, row):
        payload = _payload("X", [("2026-09-10", row)])
        bars, suspended, invalid, _np = parse_history_records(payload, "X")
        assert bars == [] and not suspended and len(invalid) == 1

    def test_out_of_range_epoch_is_invalid_row_not_batch_abort(self):
        payload = _payload("X", [(10**30, _bar()), ("2026-09-10", _bar())])
        bars, _, invalid, _np = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-10"]
        assert len(invalid) == 1 and "非法" in invalid[0]

    def test_nonpositive_qfq_deep_history_skipped_but_explained(self):
        """前复权深历史非正价:不入库(Cache 正价不变量),但作为已观测证据。"""
        payload = _payload("X", [("2021-05-19", _bar(o=-3.5, h=-3.2, l=-3.6, c=-3.2)),
                                 ("2026-09-10", _bar())])
        bars, _, invalid, nonpositive = parse_history_records(payload, "X")
        assert [b["date"] for b in bars] == ["2026-09-10"]
        assert invalid == []
        assert nonpositive == {"2021-05-19"}

    def test_beyond_cutoff_rows_skipped_silently(self):
        """晚于定稿截断的行整行跳过(盘中演化 bar 不入库)。"""
        payload = _payload("X", [("2026-09-10", _bar()),
                                 ("2026-09-11", _bar(c=0.0)),  # 超 cutoff:不校验
                                 ("2026-09-14", _bar())])
        bars, _, invalid, _np = parse_history_records(
            payload, "X", cutoff="2026-09-10")
        assert [b["date"] for b in bars] == ["2026-09-10"]
        assert invalid == []

    def test_protocol_violations(self):
        with pytest.raises(QmtChannelError, match="无数据"):
            parse_history_records({"status": "ok", "data": {}}, "X")
        with pytest.raises(QmtChannelError, match="index/columns"):
            parse_history_records({"data": {"X": {"index": []}}}, "X")
        with pytest.raises(QmtChannelError, match="长度不齐"):
            parse_history_records(
                {"data": {"X": {"index": ["2026-09-10"], "columns": {
                    "open": [], "high": [], "low": [], "close": [],
                    "volume": []}}}}, "X")
        with pytest.raises(QmtChannelError, match="缺字段"):
            parse_history_records(
                {"data": {"X": {"index": ["2026-09-10"], "columns": {
                    "open": [1.0]}}}}, "X")


def _seed_calendar(cache: Cache, days: list[str]):
    """用他源(baostock)行充当日历证据。"""
    bars = [{"date": d, "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 10.0} for d in days]
    cache.upsert("000001.SH", bars, source="baostock", adjustment_mode="raw",
                 adjustment_version="baostock-adjustflag-3")


class TestSync:
    def _sync_client(self, payload_by_symbol: dict, alive=True):
        routes = {("/", "GET"): _status(alive=alive)}

        def submit(body):
            return {"id": body["params"]["symbol"]}

        def poll(path_payload=None):
            return None

        def transport(path, method, body):
            if (path, method) == ("/", "GET"):
                return routes[("/", "GET")]
            if (path, method) == ("/fulldata", "POST"):
                return submit(body)
            symbol = path.rsplit("/", 1)[-1]
            return payload_by_symbol[symbol]

        return QmtChannelClient(transport=transport, sleep=lambda _: None)

    def test_dead_channel_fails_closed(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        client = self._sync_client({}, alive=False)
        with pytest.raises(QmtChannelError, match="尚无 latest"):
            sync_qmt_daily(cache, client, ["600519.SH"])
        cache.close()

    def test_stale_snapshot_fails_fast(self, tmp_path):
        """导出进程活着但策略停跳(快照 >900s):整批快速失败,不进标的循环。"""
        cache = Cache(tmp_path / "t.sqlite")
        calls = []

        def transport(path, method, body):
            calls.append(path)
            if path == "/":
                return _status(age=1800.0)
            raise AssertionError("不应进入 fulldata 阶段")

        client = QmtChannelClient(transport=transport, sleep=lambda _: None)
        with pytest.raises(QmtChannelError, match="停跳"):
            sync_qmt_daily(cache, client, ["600519.SH"])
        assert calls == ["/"]
        cache.close()

    def test_missing_age_field_fails_closed(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")

        def transport(path, method, body):
            return {"server": "QmtExport/2.0", "latest_exists": True}

        client = QmtChannelClient(transport=transport, sleep=lambda _: None)
        with pytest.raises(QmtChannelError, match="latest_age_sec"):
            sync_qmt_daily(cache, client, ["600519.SH"])
        cache.close()

    def test_happy_path_identity_isolated(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10", "2026-09-11"]
        _seed_calendar(cache, days)
        payload = _payload("600519.SH", [(d, _bar()) for d in days])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["sh600519"])
        assert result["rows"] == 2 and result["codes_ok"] == ["600519.SH"]
        rows = {tuple(r) for r in cache._conn.execute(
            "SELECT source, adjustment_mode, adjustment_version FROM daily"
            " WHERE code='600519.SH'")}
        assert rows == {(SOURCE, ADJ_MODE, ADJ_VERSION)}
        # 他源行未被触碰
        assert cache._conn.execute(
            "SELECT COUNT(*) FROM daily WHERE source='baostock'").fetchone()[0] == 2
        # coverage 已记录
        assert cache._conn.execute(
            "SELECT COUNT(*) FROM sync_coverage WHERE source=?",
            (SOURCE,)).fetchone()[0] == 1
        cache.close()

    def test_idempotent_upsert(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10"]
        _seed_calendar(cache, days)
        payload = _payload("600519.SH", [(d, _bar()) for d in days])
        client = self._sync_client({"600519.SH": payload})
        sync_qmt_daily(cache, client, ["600519.SH"])
        sync_qmt_daily(cache, client, ["600519.SH"])
        assert cache._conn.execute(
            "SELECT COUNT(*) FROM daily WHERE source='qmt'").fetchone()[0] == 1
        cache.close()

    def test_per_symbol_failure_does_not_block_batch(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10"]
        _seed_calendar(cache, days)
        ok = _payload("600519.SH", [(days[0], _bar())])

        def transport(path, method, body):
            if (path, method) == ("/", "GET"):
                return _status()
            if (path, method) == ("/fulldata", "POST"):
                return {"id": body["params"]["symbol"]}
            symbol = path.rsplit("/", 1)[-1]
            if symbol == "000001.SH":
                return {"id": symbol, "status": "error", "error": "no local data"}
            return ok

        client = QmtChannelClient(transport=transport, sleep=lambda _: None)
        result = sync_qmt_daily(cache, client, ["000001.SH", "600519.SH"])
        assert result["codes_ok"] == ["600519.SH"]
        assert "000001.SH" in result["errors"]
        cache.close()

    def test_coverage_hole_blocks_declaration(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-10", "2026-09-11", "2026-09-14"])
        # QMT 返回区间首尾,中间交易日 09-11 无 bar 也无停牌证据
        payload = _payload("600519.SH", [("2026-09-10", _bar()),
                                         ("2026-09-14", _bar())])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert "600519.SH" in result["coverage_holes"]
        assert cache._conn.execute(
            "SELECT COUNT(*) FROM sync_coverage WHERE source=?",
            (SOURCE,)).fetchone()[0] == 0
        cache.close()

    def test_empty_calendar_never_declares_coverage(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert result["coverage_holes"]["600519.SH"] == ["trading calendar empty"]
        cache.close()

    def test_suspension_explains_gap(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10", "2026-09-11"]
        _seed_calendar(cache, days)
        payload = _payload("600519.SH", [
            ("2026-09-10", _bar()),
            ("2026-09-11", _bar(suspendFlag=True)),
        ])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert result["coverage_holes"] == {}
        assert result["rows"] == 1
        cache.close()

    def test_full_refresh_deletes_rows_beyond_channel_reach(self, tmp_path):
        """通道返回范围之外的更早日行被删除:库内序列保持单一因子版本。"""
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-08", "2026-09-09", "2026-09-10"]
        _seed_calendar(cache, days)
        # 首次:通道返回 09-08 起三天
        payload1 = _payload("600519.SH", [(d, _bar()) for d in days])
        client = self._sync_client({"600519.SH": payload1})
        r1 = sync_qmt_daily(cache, client, ["600519.SH"])
        assert r1["rows"] == 3
        # 第二次:通道只返回后两天(窗口滑动/除权重述)
        payload2 = _payload("600519.SH", [
            ("2026-09-09", _bar(o=19.5, h=21.5, l=19.0, c=20.0)),
            ("2026-09-10", _bar(o=20.5, h=22.0, l=20.0, c=21.0))])
        client = self._sync_client({"600519.SH": payload2})
        r2 = sync_qmt_daily(cache, client, ["600519.SH"])
        assert r2["rows"] == 2
        stored = {r[0]: r[1] for r in cache._conn.execute(
            "SELECT date, close FROM daily WHERE source='qmt'")}
        assert stored == {"2026-09-09": 20.0, "2026-09-10": 21.0}  # 09-08 已删
        cache.close()

    def test_stale_positive_row_deleted_when_day_turns_nonpositive(self, tmp_path):
        """除权重述后某日转为非正价:旧因子版本的残留正价行被删除。"""
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-08", "2026-09-09", "2026-09-10"])
        # 旧因子版本:三天都是正价且已入库
        old = _payload("600519.SH", [(d, _bar()) for d in
                                     ["2026-09-08", "2026-09-09", "2026-09-10"]])
        sync_qmt_daily(cache, self._sync_client({"600519.SH": old}),
                       ["600519.SH"])
        # 新因子版本:09-08 重述为非正价
        new = _payload("600519.SH", [
            ("2026-09-08", _bar(o=-0.5, h=-0.2, l=-0.6, c=-0.3)),
            ("2026-09-09", _bar()),
            ("2026-09-10", _bar()),
        ])
        result = sync_qmt_daily(cache, self._sync_client({"600519.SH": new}),
                                ["600519.SH"])
        stored = sorted(r[0] for r in cache._conn.execute(
            "SELECT date FROM daily WHERE source='qmt'"))
        assert stored == ["2026-09-09", "2026-09-10"]
        assert result["nonpositive"] == {"600519.SH": 1}
        cache.close()

    def test_disjoint_coverage_not_merged(self, tmp_path):
        """既有覆盖与本次区间夹缝含交易日:拒绝 MIN/MAX 合并式声明。"""
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-08", "2026-09-09", "2026-09-10"])
        # 既有覆盖止于 09-08(模拟早期运行)
        cache.record_sync_coverage("600519.SH", SOURCE, ADJ_MODE, ADJ_VERSION,
                                   "2026-09-08", "2026-09-08")
        # 本次只验证 [09-10, 09-10](宕机超窗后),09-09 无人检查
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert "600519.SH" in result["coverage_holes"]
        row = cache._conn.execute(
            "SELECT start_date, end_date FROM sync_coverage WHERE code='600519.SH'"
            " AND source='qmt'").fetchone()
        assert tuple(row) == ("2026-09-08", "2026-09-08")  # 未被合并夸大
        cache.close()

    def test_adjacent_coverage_merges(self, tmp_path):
        """夹缝无交易日(相邻区间):正常合并声明。"""
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-08", "2026-09-10"])  # 09-09 非交易日
        cache.record_sync_coverage("600519.SH", SOURCE, ADJ_MODE, ADJ_VERSION,
                                   "2026-09-08", "2026-09-08")
        payload = _payload("600519.SH", [("2026-09-10", _bar())])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert result["coverage_holes"] == {}
        row = cache._conn.execute(
            "SELECT start_date, end_date FROM sync_coverage WHERE code='600519.SH'"
            " AND source='qmt'").fetchone()
        assert tuple(row) == ("2026-09-08", "2026-09-10")
        cache.close()

    def test_unfinished_current_session_bar_excluded(self, tmp_path, monkeypatch):
        """盘中运行时,未收盘的当日 bar 不得以 is_final=True 入库。"""
        monkeypatch.setattr(fetch_qmt, "latest_finalized_date",
                            lambda *a, **k: "2026-09-10")
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10", "2026-09-11"]
        _seed_calendar(cache, days)
        payload = _payload("600519.SH", [(d, _bar()) for d in days])
        client = self._sync_client({"600519.SH": payload})
        result = sync_qmt_daily(cache, client, ["600519.SH"])
        assert result["rows"] == 1  # 09-11(未收盘)被截断
        stored = [r[0] for r in cache._conn.execute(
            "SELECT date FROM daily WHERE source='qmt'")]
        assert stored == ["2026-09-10"]
        assert all(r[0] for r in cache._conn.execute(
            "SELECT is_final FROM daily WHERE source='qmt'"))
        cache.close()


def _snapshot_client(front: dict | None, dividend_type: str = "none",
                     calls: list | None = None):
    """/latest 快照传输:front={symbol: rec};dividend_type='front' 时改读 market。"""
    snap = {"generated": "2026-09-14T15:00:00", "dividend_type": dividend_type}
    if front is not None:
        if dividend_type == "front":
            snap["market"] = front
        else:
            snap["market_adj"] = {"front": front}

    def transport(path, method, body, max_bytes=None):
        if calls is not None:
            calls.append(path)
        if path == "/":
            return _status()
        if path == "/latest":
            return snap
        raise AssertionError(f"unexpected path {path}")

    return QmtChannelClient(transport=transport, sleep=lambda _: None)


class TestSnapshotSync:
    def test_happy_path_one_call_covers_pool(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        days = ["2026-09-10", "2026-09-11"]
        _seed_calendar(cache, days)
        rec = {"index": days, "columns": {
            "open": [10.0, 10.0], "high": [11.0, 11.0], "low": [9.5, 9.5],
            "close": [10.5, 10.5], "volume": [1000.0, 1000.0]}}
        calls = []
        client = _snapshot_client({"600519.SH": rec}, calls=calls)
        result = fetch_qmt.sync_qmt_daily_from_snapshot(
            cache, client, ["600519.SH"])
        assert result["rows"] == 2 and result["codes_ok"] == ["600519.SH"]
        assert calls == ["/", "/latest"]  # 健康门 + 单次快照,无 fulldata
        cache.close()

    def test_codes_outside_pool_recorded_not_errors(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-10"])
        rec = {"index": ["2026-09-10"], "columns": {
            "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
            "volume": [10.0]}}
        client = _snapshot_client({"600519.SH": rec})
        result = fetch_qmt.sync_qmt_daily_from_snapshot(
            cache, client, ["600519.SH", "000300.SH"])
        assert result["codes_ok"] == ["600519.SH"]
        assert result["not_in_pool"] == ["000300.SH"]
        assert result["errors"] == {}
        cache.close()

    def test_dividend_type_front_reads_market(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        _seed_calendar(cache, ["2026-09-10"])
        rec = {"index": ["2026-09-10"], "columns": {
            "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
            "volume": [10.0]}}
        client = _snapshot_client({"600519.SH": rec}, dividend_type="front")
        result = fetch_qmt.sync_qmt_daily_from_snapshot(
            cache, client, ["600519.SH"])
        assert result["rows"] == 1
        cache.close()

    def test_snapshot_without_front_data_rejected(self, tmp_path):
        cache = Cache(tmp_path / "t.sqlite")
        client = _snapshot_client(None)  # 快照无 front 复权段
        with pytest.raises(QmtChannelError, match="front"):
            fetch_qmt.sync_qmt_daily_from_snapshot(
                cache, client, ["600519.SH"])
        cache.close()
