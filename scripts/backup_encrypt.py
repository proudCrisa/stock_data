#!/usr/bin/env python3
"""AES-256-GCM 加密本地数据源并打包为 .zip。

密钥安全约定：
- 密钥仅经 getpass 交互式读取，不回显；
- 不作命令行参数、不入环境变量、不写磁盘、不进任何日志；
- 派生用 PBKDF2-HMAC-SHA256（200k 迭代）+ 随机 salt；GCM 提供完整性认证。

用法：
    python backup_encrypt.py            # 加密 ~/.stockdata/cache.sqlite
    python backup_encrypt.py <文件...>  # 加密指定文件
产物：当前目录下 stockdata-backup-<n>files.zip
     内含每个源文件的 .enc（salt|nonce|ciphertext|tag）+ MANIFEST.txt（明文参数，无密钥）。
"""

from __future__ import annotations

import getpass
import os
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

MAGIC = b"SDBK1\x00"  # 文件头魔数 + 版本，解密时校验
KDF_ITERATIONS = 200_000
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32  # AES-256


def _derive_key(password: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, iterations=KDF_ITERATIONS
    )
    return kdf.derive(password)


def _read_password() -> bytes:
    p1 = getpass.getpass("设置加密密钥: ")
    if not p1:
        sys.exit("密钥为空，已取消。")
    p2 = getpass.getpass("再次输入确认: ")
    if p1 != p2:
        sys.exit("两次输入不一致，已取消。")
    return p1.encode("utf-8")


def _encrypt_blob(plaintext: bytes, password: bytes) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)  # ct 尾部含 16B GCM tag
    return MAGIC + salt + nonce + ct


def main(argv: list[str]) -> int:
    if argv:
        sources = [Path(a).expanduser() for a in argv]
    else:
        sources = [Path.home() / ".stockdata" / "cache.sqlite"]

    missing = [s for s in sources if not s.is_file()]
    if missing:
        sys.exit("找不到文件: " + ", ".join(str(m) for m in missing))

    print("将加密以下文件：")
    for s in sources:
        print(f"  - {s}  ({s.stat().st_size:,} bytes)")

    password = _read_password()

    out_zip = Path.cwd() / f"stockdata-backup-{len(sources)}files.zip"
    if out_zip.exists():
        sys.exit(f"输出已存在，拒绝覆盖: {out_zip}")
    manifest = [
        "stockdata 加密备份清单（明文，不含密钥）",
        "算法: AES-256-GCM | KDF: PBKDF2-HMAC-SHA256",
        f"迭代: {KDF_ITERATIONS} | salt: {SALT_LEN}B | nonce: {NONCE_LEN}B",
        f"文件头: {MAGIC!r}  布局: MAGIC|salt|nonce|ciphertext(+16B tag)",
        "解密: python backup_decrypt.py <name>.enc",
        "-" * 40,
    ]
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in sources:
            blob = _encrypt_blob(s.read_bytes(), password)
            arcname = s.name + ".enc"
            zf.writestr(arcname, blob)
            manifest.append(f"{arcname}  <=  {s}  ({s.stat().st_size:,} bytes 明文)")
        zf.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")

    del password
    print(f"\n完成：{out_zip}  ({out_zip.stat().st_size:,} bytes)")
    print("提示：解密用同目录 backup_decrypt.py。密钥未被存储，请自行牢记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
