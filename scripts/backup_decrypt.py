#!/usr/bin/env python3
"""解密 backup_encrypt.py 产生的 .enc 文件。

密钥仅经 getpass 读取，同样不落盘、不进日志。

用法：
    unzip stockdata-backup-*.zip          # 先解出 .enc 文件
    python backup_decrypt.py cache.sqlite.enc [输出路径]
默认输出为去掉 .enc 后缀的文件名。GCM 认证失败（密钥错或数据被篡改）会报错并拒绝写出。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

MAGIC = b"SDBK1\x00"
KDF_ITERATIONS = 200_000
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32


def _derive_key(password: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, iterations=KDF_ITERATIONS
    )
    return kdf.derive(password)


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit("用法: python backup_decrypt.py <name>.enc [输出路径]")
    src = Path(argv[0]).expanduser()
    if not src.is_file():
        sys.exit(f"找不到文件: {src}")

    blob = src.read_bytes()
    head = len(MAGIC)
    if blob[:head] != MAGIC:
        sys.exit("文件头不匹配，可能不是本工具产生的备份。")
    salt = blob[head : head + SALT_LEN]
    nonce = blob[head + SALT_LEN : head + SALT_LEN + NONCE_LEN]
    ct = blob[head + SALT_LEN + NONCE_LEN :]

    password = getpass.getpass("输入解密密钥: ").encode("utf-8")
    key = _derive_key(password, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        sys.exit("解密失败：密钥错误或数据被篡改（GCM 认证不通过）。")
    del password, key

    if len(argv) > 1:
        out = Path(argv[1]).expanduser()
    else:
        out = (
            src.with_suffix("")
            if src.suffix == ".enc"
            else src.with_name(src.name + ".dec")
        )
    if out.exists():
        sys.exit(f"输出已存在，拒绝覆盖: {out}")
    out.write_bytes(plaintext)
    print(f"完成：{out}  ({len(plaintext):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
