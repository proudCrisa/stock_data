from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
import zipfile

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_decrypt = _load_script("backup_decrypt")
backup_encrypt = _load_script("backup_encrypt")


def test_encrypted_backup_round_trip_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"execution-grade-evidence")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup_encrypt, "_read_password", lambda: b"test-password")

    assert backup_encrypt.main([str(source)]) == 0
    archive = tmp_path / "stockdata-backup-1files.zip"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extract("source.sqlite.enc", tmp_path)

    output = tmp_path / "restored.sqlite"
    monkeypatch.setattr(backup_decrypt.getpass, "getpass", lambda _: "test-password")
    assert backup_decrypt.main([str(tmp_path / "source.sqlite.enc"), str(output)]) == 0
    assert output.read_bytes() == source.read_bytes()

    with pytest.raises(SystemExit, match="拒绝覆盖"):
        backup_encrypt.main([str(source)])


def test_wrong_password_does_not_write_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted = tmp_path / "source.enc"
    encrypted.write_bytes(backup_encrypt._encrypt_blob(b"secret", b"correct"))
    output = tmp_path / "output"
    monkeypatch.setattr(backup_decrypt.getpass, "getpass", lambda _: "wrong")

    with pytest.raises(SystemExit, match="解密失败"):
        backup_decrypt.main([str(encrypted), str(output)])
    assert not output.exists()


def test_read_password_from_fd_for_unattended_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"fd-password\n")
    os.close(write_fd)
    monkeypatch.setenv("STOCKDATA_BACKUP_PASSWORD_FD", str(read_fd))
    try:
        assert backup_encrypt._read_password() == b"fd-password"
    finally:
        os.close(read_fd)


def test_read_password_from_fd_rejects_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    monkeypatch.setenv("STOCKDATA_BACKUP_PASSWORD_FD", str(read_fd))
    try:
        with pytest.raises(SystemExit, match="密钥为空"):
            backup_encrypt._read_password()
    finally:
        os.close(read_fd)
