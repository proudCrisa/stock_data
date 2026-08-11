import json
import os

import pytest

from stockdata.cache import Cache
from stockdata.snapshot import create_snapshot, verify_snapshot


def _bar(day, close, *, is_final=True):
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "is_final": is_final,
    }


def test_snapshot_is_finalized_hashed_and_idempotent(tmp_path):
    cache = Cache(tmp_path / "source.sqlite")
    cache.upsert("600519.SH", [
        _bar("2024-01-02", 1.0),
        _bar("2024-01-03", 1.1),
        _bar("2024-01-04", 1.2, is_final=False),
    ])

    root = tmp_path / "snapshots"
    first = create_snapshot(
        cache, root, "2024-01-04", codes=["600519.SH"],
        adjustment_version="baostock-adjustflag-2",
    )
    second = create_snapshot(
        cache, root, "2024-01-04", codes=["600519.SH"],
        adjustment_version="baostock-adjustflag-2",
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["snapshot_id"] == (
        f"2024-01-04-{first['content_sha256']}"
    )
    assert first["row_count"] == 2
    snapshot_dir = root / first["snapshot_id"]
    manifest = json.loads((snapshot_dir / "manifest.json").read_text())
    assert len(manifest["content_sha256"]) == 64
    assert len(manifest["database_sha256"]) == 64
    assert os.stat(snapshot_dir / "data.sqlite").st_mode & 0o222 == 0
    assert os.stat(snapshot_dir / "manifest.json").st_mode & 0o222 == 0
    assert os.stat(snapshot_dir).st_mode & 0o222 == 0
    assert verify_snapshot(snapshot_dir)["valid"] is True
    assert len([path for path in root.iterdir() if path.is_dir()]) == 1


def test_existing_corrupt_snapshot_is_not_silently_reused(tmp_path):
    cache = Cache(tmp_path / "source.sqlite")
    cache.upsert("600519.SH", [_bar("2024-01-02", 1.0)])
    root = tmp_path / "snapshots"
    manifest = create_snapshot(
        cache, root, "2024-01-02", codes=["600519.SH"],
        adjustment_version="baostock-adjustflag-2",
    )
    snapshot_dir = root / manifest["snapshot_id"]
    database = snapshot_dir / "data.sqlite"
    os.chmod(snapshot_dir, 0o755)
    os.chmod(database, 0o644)
    database.write_bytes(b"corrupt")

    try:
        create_snapshot(
            cache, root, "2024-01-02", codes=["600519.SH"],
            adjustment_version="baostock-adjustflag-2",
        )
    except ValueError as exc:
        assert "verification failed" in str(exc)
    else:
        raise AssertionError("corrupt snapshot must not be reused")


def test_snapshot_can_select_one_source_variant(tmp_path):
    cache = Cache(tmp_path / "source.sqlite")
    cache.upsert(
        "600519.SH", [_bar("2024-01-02", 1.0)], source="source-a",
        adjustment_mode="qfq", adjustment_version="qfq-v1",
    )
    cache.upsert(
        "600519.SH", [_bar("2024-01-02", 9.0)], source="source-b",
        adjustment_mode="qfq", adjustment_version="qfq-v1",
    )
    with pytest.raises(ValueError, match="mixed provenance"):
        create_snapshot(
            cache, tmp_path / "snapshots", "2024-01-02",
            adjustment_version="qfq-v1",
        )
    selected = create_snapshot(
        cache, tmp_path / "snapshots", "2024-01-02", source="source-a",
        adjustment_version="qfq-v1",
    )
    assert selected["source"] == "source-a"


def test_snapshot_rejects_as_of_after_latest_finalized_date(
    tmp_path, monkeypatch
):
    cache = Cache(tmp_path / "future.sqlite")
    monkeypatch.setattr(
        "stockdata.snapshot.latest_finalized_date", lambda: "2024-01-04"
    )

    with pytest.raises(ValueError, match="latest finalized date"):
        create_snapshot(cache, tmp_path / "snapshots", "2024-01-05")


def test_verify_rejects_manifest_snapshot_id_tampering(tmp_path):
    cache = Cache(tmp_path / "source.sqlite")
    cache.upsert("600519.SH", [_bar("2024-01-02", 1.0)])
    root = tmp_path / "snapshots"
    manifest = create_snapshot(cache, root, "2024-01-02")
    assert manifest["adjustment_version"] == "baostock-adjustflag-2"
    snapshot_dir = root / manifest["snapshot_id"]
    manifest_path = snapshot_dir / "manifest.json"
    os.chmod(snapshot_dir, 0o755)
    os.chmod(manifest_path, 0o644)
    payload = json.loads(manifest_path.read_text())
    payload["snapshot_id"] = "forged"
    manifest_path.write_text(json.dumps(payload))

    assert verify_snapshot(snapshot_dir)["valid"] is False
