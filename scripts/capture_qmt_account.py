"""捕获一次 QMT 账户/持仓密封快照(0600 JSON,落入 ~/.stockdata/qmt-account/)。

stdout 只输出文件路径与内容哈希,绝不输出账户明细。
退出码:0 = 捕获成功;3 = 通道不可达/凭据缺失/账户段缺失。

用法:
    .venv/bin/python scripts/capture_qmt_account.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockdata.fetch_qmt import (  # noqa: E402
    QmtChannelClient,
    QmtChannelError,
    load_qmt_token,
)
from stockdata.qmt_account_capture import (  # noqa: E402
    QmtAccountCaptureError,
    capture_qmt_account,
    verify_qmt_account_capture,
)


def main() -> int:
    try:
        client = QmtChannelClient(token=load_qmt_token())
        snapshot = client._transport("/latest", "GET", None)
        path = capture_qmt_account(snapshot)
        payload = verify_qmt_account_capture(path)
    except (QmtChannelError, QmtAccountCaptureError) as exc:
        print(f"ERROR: {exc}")
        return 3
    print(f"captured: {path}")
    print(f"sha256:   {payload['content_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
