from __future__ import annotations

import importlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


MODULE = importlib.import_module("stockdata.collector_continuity")


def _api(name: str):
    return getattr(MODULE, name)


def _error():
    return _api("CollectorContinuityError")


def _identity(path: Path):
    opened = _api("open_existing_regular_file")(path)
    try:
        return opened.identity
    finally:
        opened.close()


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "collector-ledger.jsonl"
    path.write_bytes(b"")
    return path


def _child_script(action: str) -> str:
    return f"""
import importlib
import os
import sys

module = importlib.import_module("stockdata.collector_continuity")
try:
    action = {action!r}
    acquire = getattr(module, "acquire_collector_phase_lease")
except AttributeError:
    sys.stdout.write("missing")
    raise SystemExit(2)

if action == "compete":
    try:
        lease = acquire(sys.argv[1])
    except Exception:
        sys.stdout.write("rejected")
        raise SystemExit(0)
    lease.close()
    sys.stdout.write("acquired")
    raise SystemExit(1)

if action == "close":
    os.close(int(sys.argv[1]))
    sys.stdout.write("closed")
    raise SystemExit(0)

sys.stdout.write("unknown")
raise SystemExit(2)
"""


def test_lease_lifecycle_identity_reentry_owner_and_close(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    lease_type = _api("CollectorPhaseLease")
    acquire = _api("acquire_collector_phase_lease")

    lease = acquire(ledger)
    try:
        assert isinstance(lease, lease_type)
        assert lease.verify() == _identity(ledger)

        lease.__enter__()
        try:
            with pytest.raises(_error()):
                lease.__enter__()
        finally:
            lease.__exit__(None, None, None)

        original_owner_pid = lease.owner_pid
        lease.owner_pid = original_owner_pid + 1
        with pytest.raises(_error()):
            lease.verify()
        lease.owner_pid = original_owner_pid
    finally:
        lease.close()
        lease.close()

    with acquire(ledger) as reacquired:
        assert reacquired.verify() == _identity(ledger)


def test_second_independent_acquire_is_nonblocking_and_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")

    with acquire(ledger):
        with pytest.raises(_error()):
            acquire(ledger)


def test_subprocess_competition_rejects_without_ledger_mutation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")

    with acquire(ledger):
        before = ledger.read_bytes()
        result = subprocess.run(
            [sys.executable, "-c", _child_script("compete"), str(ledger)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "rejected"
        assert ledger.read_bytes() == before


def test_handoff_fd_inheritance_and_context_lifetime(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    handoff_type = _api("CollectorChildLeaseHandoff")

    with acquire(ledger) as lease:
        base_fd = lease.ledger.file_fd
        assert os.get_inheritable(base_fd) is False
        with lease.child_handoff() as handoff:
            assert isinstance(handoff, handoff_type)
            assert handoff.pass_fds == (handoff.fd,)
            assert os.get_inheritable(handoff.fd) is True
            lease.verify()
        with pytest.raises(OSError):
            os.fstat(handoff.fd)
        assert lease.verify() == _identity(ledger)


def test_locked_primitive_accepts_only_a_currently_locked_matching_fd(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    verify = _api("verify_locked_collector_lease")
    identity = _identity(ledger)

    with acquire(ledger) as lease:
        with lease.child_handoff() as handoff:
            assert verify(
                handoff.fd,
                expected_ledger_identity=identity,
            ) == identity

            for invalid_fd in (False, -1, 0, 1, 2, 999_999):
                with pytest.raises(_error()):
                    verify(invalid_fd, expected_ledger_identity=identity)

            wrong_path = tmp_path / "other-ledger.jsonl"
            wrong_path.write_bytes(b"")
            wrong_fd = os.open(wrong_path, os.O_RDONLY)
            try:
                with pytest.raises(_error()):
                    verify(wrong_fd, expected_ledger_identity=identity)
            finally:
                os.close(wrong_fd)

            independent_fd = os.open(ledger, os.O_RDONLY)
            try:
                with pytest.raises(_error()):
                    verify(independent_fd, expected_ledger_identity=identity)
            finally:
                os.close(independent_fd)

            wrong_identity = type(identity)(
                canonical_path=identity.canonical_path,
                parent_st_dev=identity.parent_st_dev,
                parent_st_ino=identity.parent_st_ino,
                file_st_dev=identity.file_st_dev,
                file_st_ino=identity.file_st_ino + 1,
            )
            with pytest.raises(_error()):
                verify(handoff.fd, expected_ledger_identity=wrong_identity)


def test_nofollow_and_identity_replacement_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    verify = _api("verify_locked_collector_lease")

    target = tmp_path / "target.jsonl"
    target.write_bytes(b"")
    symlink = tmp_path / "symlink.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(_error()):
        acquire(symlink)

    with acquire(ledger) as lease:
        identity = lease.verify()
        with lease.child_handoff() as handoff:
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_bytes(b"")
            os.replace(replacement, ledger)
            with pytest.raises(_error()):
                lease.verify()
            with pytest.raises(_error()):
                verify(handoff.fd, expected_ledger_identity=identity)


def test_handoff_keeps_lock_after_base_close_until_handoff_closes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")

    lease = acquire(ledger)
    handoff_context = lease.child_handoff()
    handoff_context.__enter__()
    try:
        lease.close()
        with pytest.raises(_error()):
            acquire(ledger)
    finally:
        handoff_context.__exit__(None, None, None)

    with acquire(ledger):
        pass


def test_shell_false_child_with_pass_fds_can_verify_handoff(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    identity = _identity(ledger)
    script = """
import importlib
import json
import sys

module = importlib.import_module("stockdata.collector_continuity")
verify = getattr(module, "verify_locked_collector_lease")
identity_type = getattr(module, "PhysicalFileIdentity")
identity = identity_type.from_dict(json.loads(sys.argv[2]))
verify(int(sys.argv[1]), expected_ledger_identity=identity)
print("verified")
"""

    with acquire(ledger) as lease:
        with lease.child_handoff() as handoff:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(handoff.fd),
                    json.dumps(identity.to_dict()),
                ],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                close_fds=True,
                pass_fds=handoff.pass_fds,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == "verified\n"


def test_child_close_does_not_unlock_parent_lease(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")

    with acquire(ledger) as lease:
        with lease.child_handoff() as handoff:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _child_script("close"),
                    str(handoff.fd),
                ],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                close_fds=True,
                pass_fds=handoff.pass_fds,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == "closed"
            with pytest.raises(_error()):
                acquire(ledger)


def test_locked_primitive_accepts_independently_locked_fd_without_active_attempt_authority(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    identity = _identity(ledger)
    verify = _api("verify_locked_collector_lease")
    independent_fd = os.open(ledger, os.O_RDONLY)
    try:
        fcntl.flock(independent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert verify(independent_fd, expected_ledger_identity=identity) == identity
        # This primitive proves only current flock plus physical identity;
        # active-attempt authority remains the separate task 2.5 contract.
    finally:
        os.close(independent_fd)


def test_lease_close_failure_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    original_close = os.close
    calls = 0

    with acquire(ledger) as lease:
        def fail_once(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected close failure")
            original_close(descriptor)

        monkeypatch.setattr(MODULE.os, "close", fail_once)
        with pytest.raises(_error(), match="cannot be closed"):
            lease.close()
        assert lease._closed is False
        monkeypatch.setattr(MODULE.os, "close", original_close)
        lease.close()
        assert lease._closed is True


def test_handoff_close_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    original_close = os.close

    with acquire(ledger) as lease:
        handoff_context = lease.child_handoff()
        handoff = handoff_context.__enter__()

        def fail_handoff(descriptor: int) -> None:
            if descriptor == handoff.fd:
                raise OSError("injected handoff close failure")
            original_close(descriptor)

        monkeypatch.setattr(MODULE.os, "close", fail_handoff)
        with pytest.raises(_error(), match="handoff cannot be closed"):
            handoff_context.__exit__(None, None, None)
        monkeypatch.setattr(MODULE.os, "close", original_close)
        os.fstat(handoff.fd)
        with pytest.raises(_error()):
            acquire(ledger)
        original_close(handoff.fd)


def test_handoff_construction_failure_cleans_duplicate_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    original_set_inheritable = MODULE.os.set_inheritable
    before = ledger.read_bytes()

    def fail_set_inheritable(descriptor: int, inheritable: bool) -> None:
        del descriptor, inheritable
        raise OSError("injected set_inheritable failure")

    with acquire(ledger) as lease:
        monkeypatch.setattr(MODULE.os, "set_inheritable", fail_set_inheritable)
        with pytest.raises(_error(), match="handoff cannot be created"):
            lease.child_handoff()
        assert lease.verify() == _identity(ledger)
        assert ledger.read_bytes() == before
    monkeypatch.setattr(MODULE.os, "set_inheritable", original_set_inheritable)

    independent_fd = os.open(ledger, os.O_RDONLY)
    try:
        fcntl.flock(independent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_fd)
    assert ledger.read_bytes() == before


def test_handoff_construction_cleanup_failure_is_honest_and_keeps_fd_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    acquire = _api("acquire_collector_phase_lease")
    original_close = MODULE.os.close
    original_set_inheritable = MODULE.os.set_inheritable
    duplicate_fd: int | None = None
    before = ledger.read_bytes()

    def fail_set_inheritable(descriptor: int, inheritable: bool) -> None:
        nonlocal duplicate_fd
        del inheritable
        duplicate_fd = descriptor
        raise OSError("injected set_inheritable failure")

    def fail_duplicate_cleanup(descriptor: int) -> None:
        if descriptor == duplicate_fd:
            raise OSError("injected duplicate cleanup failure")
        original_close(descriptor)

    lease = acquire(ledger)
    try:
        monkeypatch.setattr(MODULE.os, "set_inheritable", fail_set_inheritable)
        monkeypatch.setattr(MODULE.os, "close", fail_duplicate_cleanup)
        with pytest.raises(_error()) as exc_info:
            lease.child_handoff()
        message = str(exc_info.value)
        assert "injected set_inheritable failure" in message
        assert "duplicate cleanup failed: injected duplicate cleanup failure" in message
        assert duplicate_fd is not None
        os.fstat(duplicate_fd)
        independent_fd = os.open(ledger, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(independent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(independent_fd)
        assert ledger.read_bytes() == before
    finally:
        monkeypatch.setattr(MODULE.os, "set_inheritable", original_set_inheritable)
        monkeypatch.setattr(MODULE.os, "close", original_close)
        if duplicate_fd is not None:
            original_close(duplicate_fd)
        lease.close()

    independent_fd = os.open(ledger, os.O_RDONLY)
    try:
        fcntl.flock(independent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_fd)
    assert ledger.read_bytes() == before
