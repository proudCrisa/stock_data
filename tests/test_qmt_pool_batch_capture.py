from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockdata import qmt_pool_replay as pool
from stockdata.qmt_pool_batch_capture import capture_qmt_pool_replay_batch
from test_qmt_pool_replay import _latest, _status


SYMBOLS = [f"{600000 + offset:06d}.SH" for offset in range(309)]


def _client(monkeypatch):
    calls = []
    latest = _latest(symbols=SYMBOLS)
    latest["request"]["count"] = 1300

    def get(self, path):
        calls.append(path)
        return _status(symbols=SYMBOLS) if path == "/" else latest

    monkeypatch.setattr(pool.QmtPoolReplayClient, "_get", get)
    return pool.QmtPoolReplayClient(token="offline-fixture"), calls, latest


def test_one_acquisition_keeps_source_request_and_full_receipt(tmp_path, monkeypatch):
    client, calls, _ = _client(monkeypatch)
    original = pool.normalize_qmt_pool_wire
    times = []

    def normalize(status, latest):
        value = original(status, latest)
        times.append(value["available_at"])
        return value

    monkeypatch.setattr(pool, "normalize_qmt_pool_wire", normalize)
    result = capture_qmt_pool_replay_batch(client, SYMBOLS[:154], tmp_path)
    assert result["status"] == "COMPLETE"
    assert calls == ["/", "/latest"]
    assert len(times) == 1
    artifacts = [pool.verify_qmt_pool_replay(json.loads(Path(path).read_bytes())) for path in result["outputs"]]
    assert [len(value["selection"]["symbols"]) for value in artifacts] == [20] * 7 + [14]
    receipt = artifacts[0]["pool_receipt"]
    assert all(value["pool_receipt"] == receipt for value in artifacts)
    assert receipt["available_at"] == times[0]
    assert receipt["source_request"]["symbols"] == SYMBOLS
    assert receipt["source_request"]["count"] == 1300
    assert all(value["decision_authority"] is False for value in artifacts)


@pytest.mark.parametrize("problem", ("short", "unordered", "duplicate", "nonempty"))
def test_invalid_batch_is_rejected_before_acquisition(tmp_path, monkeypatch, problem):
    client, calls, _ = _client(monkeypatch)
    cohort = SYMBOLS[:154]
    if problem == "short":
        cohort.pop()
    elif problem == "unordered":
        cohort.reverse()
    elif problem == "duplicate":
        cohort[-1] = cohort[-2]
    else:
        (tmp_path / "existing.json").write_text("existing")
    with pytest.raises(pool.QmtPoolReplayError):
        capture_qmt_pool_replay_batch(client, cohort, tmp_path)
    assert calls == []


@pytest.mark.parametrize("problem", ("source_count", "generated", "last_partition"))
def test_invalid_source_is_rejected_before_publication(tmp_path, monkeypatch, problem):
    client, _, latest = _client(monkeypatch)
    if problem == "source_count":
        latest["request"]["count"] = 1299
    elif problem == "generated":
        latest["generated"] = "2026-08-30 17:13:44"
    else:
        latest["market"][SYMBOLS[153]]["columns"]["close"][0] = -1
    with pytest.raises(pool.QmtPoolReplayError):
        capture_qmt_pool_replay_batch(client, SYMBOLS[:154], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_interrupted_publication_is_incomplete_and_cannot_resume(tmp_path, monkeypatch):
    client, calls, _ = _client(monkeypatch)
    writer = pool.write_qmt_pool_replay
    count = 0

    def write(root, artifact):
        nonlocal count
        count += 1
        if count == 4:
            raise OSError("offline injected interruption")
        return writer(root, artifact)

    monkeypatch.setattr(pool, "write_qmt_pool_replay", write)
    with pytest.raises(OSError, match="injected"):
        capture_qmt_pool_replay_batch(client, SYMBOLS[:154], tmp_path)
    assert len(list(tmp_path.iterdir())) == 3
    with pytest.raises(pool.QmtPoolReplayError, match="must be empty"):
        capture_qmt_pool_replay_batch(client, SYMBOLS[:154], tmp_path)
    assert calls == ["/", "/latest"]
