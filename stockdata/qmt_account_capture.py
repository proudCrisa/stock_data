"""QMT 账户/持仓密封捕获:0600 权限 JSON,不进主库、不进日志。

隐私数据按 authority 约束走文件密封:每次捕获生成一个时间戳文件
``~/.stockdata/qmt-account/<UTC>.json``,内容为
``{schema, captured_at, source_generated, account, positions, content_sha256}``,
其中 ``content_sha256`` 是对 ``{source_generated, account, positions}``
规范化 JSON 的哈希,可用 :func:`verify_qmt_account_capture` 离线校验自洽性。
``source_generated`` 是通道快照自身的生成时间——消费方据此可识别陈旧观测。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "stockdata-qmt-account-capture/2"
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


class QmtAccountCaptureError(ValueError):
    """账户段缺失、产物超限或校验失败。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _extract_account_sections(snapshot: dict) -> tuple[dict, list]:
    """从 ``/latest`` 快照提取 ACCOUNT / POSITION 段;缺段即拒绝。"""
    if not isinstance(snapshot, dict):
        raise QmtAccountCaptureError("快照不是 JSON 对象")
    sections = snapshot.get("account", {}).get("sections", {})
    account_rows = sections.get("ACCOUNT") or []
    position_rows = sections.get("POSITION") or []
    if not account_rows:
        raise QmtAccountCaptureError("快照无 ACCOUNT 段(QMT 未登录资金账号?)")
    return account_rows[0], list(position_rows)


def capture_qmt_account(
    snapshot: dict,
    output_root: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """把一次 ``/latest`` 快照的账户段落盘为 0600 密封 JSON,返回路径。

    只写文件;调用方负责日志纪律(路径与哈希可记,内容不可记)。
    """
    account, positions = _extract_account_sections(snapshot)
    now = now or _utc_now()
    content = {"source_generated": snapshot.get("generated"),
               "account": account, "positions": positions}
    payload = {
        "schema": SCHEMA_VERSION,
        "captured_at": now.isoformat(timespec="seconds"),
        **content,
        "content_sha256": _sha256(_canonical(content)),
    }
    blob = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    if len(blob) > _MAX_CAPTURE_BYTES:
        raise QmtAccountCaptureError(f"捕获产物超限: {len(blob)} bytes")

    root = Path(output_root) if output_root else (
        Path.home() / ".stockdata" / "qmt-account")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = now.strftime("%Y%m%dT%H%M%S") + f"-{_sha256(blob)[:12]}.json"
    path = root / name
    # O_EXCL 防覆盖;0600 从创建即生效,不留窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # 短写防御:os.write 允许部分写入,循环到写完;失败则回滚产物
        view = memoryview(blob)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise QmtAccountCaptureError(f"写入停滞: {path}")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return path


def verify_qmt_account_capture(path: str | Path) -> dict:
    """离线校验捕获文件:schema、0600 权限、sha256 自洽。返回 payload。"""
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise QmtAccountCaptureError(
            f"捕获文件权限必须为 0600,当前 {oct(mode)}: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA_VERSION:
        raise QmtAccountCaptureError(f"schema 不符: {payload.get('schema')!r}")
    content = {"source_generated": payload.get("source_generated"),
               "account": payload.get("account"),
               "positions": payload.get("positions")}
    if _sha256(_canonical(content)) != payload.get("content_sha256"):
        raise QmtAccountCaptureError("content_sha256 不自洽,文件可能被篡改")
    return payload
