"""One pool acquisition, one observation time, eight fixed research-only seals."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from stockdata import qmt_pool_replay as pool


SCHEMA_VERSION = "qmt-pool-batch-capture-result/1"
PARTITIONS = (20, 20, 20, 20, 20, 20, 20, 14)


def capture_qmt_pool_replay_batch(
    client: pool.QmtPoolReplayClient, symbols: Sequence[str], output_root: str | Path
) -> dict:
    cohort = pool._symbols(list(symbols), maximum=154, field="cohort", ordered=True)
    if len(cohort) != sum(PARTITIONS):
        raise pool.QmtPoolReplayError("batch requires exactly 154 ordered symbols")
    root_fd, root, identity = pool._safe_output_root(output_root)
    try:
        if os.listdir(root_fd):
            raise pool.QmtPoolReplayError("batch output root must be empty")
        status = client._get("/")
        latest = client._get("/latest")
        normalized = pool.normalize_qmt_pool_wire(status, latest)
        request = normalized["source_request"]
        if len(request["symbols"]) != 309 or request["count"] != 1300:
            raise pool.QmtPoolReplayError("batch requires the 309-symbol, count-1300 source request")
        artifacts = []
        offset = 0
        for count in PARTITIONS:
            artifact = pool.seal_qmt_pool_replay(normalized, cohort[offset:offset + count])
            artifacts.append(pool.verify_qmt_pool_replay(artifact))
            offset += count
        receipt = pool._canonical(artifacts[0]["pool_receipt"])
        if any(pool._canonical(item["pool_receipt"]) != receipt for item in artifacts):
            raise pool.QmtPoolReplayError("batch pool receipts differ")
        if os.listdir(root_fd):
            raise pool.QmtPoolReplayError("batch output root changed before publication")
        # A failed/crashed attempt may leave partial files. It never reports COMPLETE,
        # and the consumer rejects every inventory other than the full eight seals.
        outputs = [pool.write_qmt_pool_replay(root, item) for item in artifacts]
        check_fd, _, current_identity = pool._safe_output_root(root)
        os.close(check_fd)
        if current_identity != identity or sorted(os.listdir(root_fd)) != sorted(path.name for path in outputs):
            raise pool.QmtPoolReplayError("batch output inventory or directory identity changed")
        for path, artifact in zip(outputs, artifacts):
            raw = pool._canonical(artifact)
            if pool._read_regular_file(root_fd, path.name, len(raw)) != raw:
                raise pool.QmtPoolReplayError("published batch content changed")
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "COMPLETE",
            "decision_eligible": False,
            "decision_authority": False,
            "actions": [],
            "outputs": [str(path) for path in outputs],
            "pool_receipt_sha256": pool._sha256(artifacts[0]["pool_receipt"]),
        }
    finally:
        os.close(root_fd)
