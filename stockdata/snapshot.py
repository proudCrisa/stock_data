"""Immutable SQLite snapshot export and verification."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .cache import Cache, _COLS, _SCHEMA, _SCHEMA_VERSION
from .finalization import latest_finalized_date
from .ticker import normalize


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_hash(rows: list[dict]) -> str:
    payload = "\n".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for row in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_rows(
    connection: sqlite3.Connection,
    as_of: str,
    codes: list[str] | None,
    source: str | None,
    adjustment_mode: str,
    adjustment_version: str | None,
) -> list[dict]:
    where = ["date<=?", "is_final=1", "adjustment_mode=?"]
    params: list[object] = [as_of, adjustment_mode]
    if adjustment_version is not None:
        where.append("adjustment_version=?")
        params.append(adjustment_version)
    if source is not None:
        where.append("source=?")
        params.append(source)
    if codes:
        where.append(f"code IN ({','.join('?' for _ in codes)})")
        params.extend(codes)
    connection.row_factory = sqlite3.Row
    query = (
        f"SELECT code,{','.join(_COLS)} FROM daily WHERE {' AND '.join(where)} "
        "ORDER BY code,date"
    )
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def create_snapshot(
    cache: Cache,
    output_root: str | Path,
    as_of: str,
    *,
    codes: list[str] | None = None,
    source: str | None = None,
    adjustment_mode: str = "qfq",
    adjustment_version: str | None = None,
) -> dict:
    """Export finalized rows into a content-addressed, read-only snapshot."""
    if as_of > latest_finalized_date():
        raise ValueError("as_of must not be later than the latest finalized date")
    normalized = sorted({normalize(code) for code in codes}) if codes else None
    rows = _select_rows(
        cache._conn, as_of, normalized, source, adjustment_mode, adjustment_version
    )
    identities = {
        (row["source"], row["adjustment_mode"], row["adjustment_version"])
        for row in rows
    }
    if len(identities) > 1:
        raise ValueError(
            f"refusing snapshot with mixed provenance: {sorted(identities)!r}"
        )
    identity = next(iter(identities)) if identities else None
    source = identity[0] if identity else None
    effective_version = adjustment_version or (identity[2] if identity else None)
    content_sha256 = _content_hash(rows)
    snapshot_id = f"{as_of}-{content_sha256}"
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / snapshot_id
    manifest_path = target / "manifest.json"
    if manifest_path.exists():
        verification = verify_snapshot(target)
        if not verification["valid"]:
            raise ValueError(f"existing snapshot verification failed: {snapshot_id}")
        return json.loads(manifest_path.read_text())
    if target.exists():
        raise ValueError(f"snapshot target already exists without manifest: {snapshot_id}")

    temp_dir = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=root))
    try:
        db_path = temp_dir / "data.sqlite"
        connection = sqlite3.connect(db_path)
        connection.execute(_SCHEMA)
        connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        columns = ("code",) + _COLS
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO daily ({','.join(columns)}) VALUES ({placeholders})",
            [tuple(row[column] for column in columns) for row in rows],
        )
        connection.commit()
        connection.close()

        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of,
            "codes": normalized,
            "source": source,
            "adjustment_mode": adjustment_mode,
            "adjustment_version": effective_version,
            "finalized_only": True,
            "row_count": len(rows),
            "content_sha256": content_sha256,
            "database_sha256": _sha256_file(db_path),
            "schema_version": _SCHEMA_VERSION,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        os.chmod(temp_dir / "data.sqlite", 0o444)
        os.chmod(temp_dir / "manifest.json", 0o444)
        os.replace(temp_dir, target)
        os.chmod(target, 0o555)
        return manifest
    except Exception:
        if temp_dir.exists():
            os.chmod(temp_dir, 0o755)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def verify_snapshot(snapshot_dir: str | Path) -> dict:
    """Verify both SQLite bytes and canonical row content against the manifest."""
    root = Path(snapshot_dir)
    connection = None
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        db_path = root / "data.sqlite"
        database_sha256 = _sha256_file(db_path)
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = _select_rows(
            connection,
            manifest["as_of"],
            manifest.get("codes"),
            manifest.get("source"),
            manifest["adjustment_mode"],
            manifest.get("adjustment_version"),
        )
        total_rows = int(
            connection.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        )
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        integrity_ok = (
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
    except Exception as exc:
        return {
            "snapshot_id": root.name,
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if connection is not None:
            connection.close()

    content_sha256 = _content_hash(rows)
    expected_snapshot_id = f"{manifest['as_of']}-{content_sha256}"
    identities = {
        (row["source"], row["adjustment_mode"], row["adjustment_version"])
        for row in rows
    }
    expected_source = next(iter(identities))[0] if len(identities) == 1 else None
    immutable = all(
        os.stat(path).st_mode & 0o222 == 0
        for path in (root, db_path, root / "manifest.json")
    )
    return {
        "snapshot_id": manifest["snapshot_id"],
        "valid": (
            database_sha256 == manifest["database_sha256"]
            and content_sha256 == manifest["content_sha256"]
            and len(rows) == manifest["row_count"]
            and total_rows == len(rows)
            and schema_version == manifest["schema_version"] == _SCHEMA_VERSION
            and integrity_ok
            and len(identities) <= 1
            and manifest.get("source") == expected_source
            and manifest.get("finalized_only") is True
            and manifest["snapshot_id"] == expected_snapshot_id
            and root.name == expected_snapshot_id
            and immutable
        ),
        "row_count": len(rows),
        "content_sha256": content_sha256,
        "database_sha256": database_sha256,
    }
