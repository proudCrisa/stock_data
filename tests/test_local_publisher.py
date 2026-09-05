import json
import stat
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stockdata.authority import load_enrolled_trust_registry, require_enrolled_role_coverage
from stockdata.local_publisher import initialize_local_publisher
from stockdata.provider_authority_publisher import _key_id, _public_key


def test_real_local_identity_verifies_and_keeps_secrets_private(tmp_path):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    directory = tmp_path / "publisher"
    anchor = initialize_local_publisher(directory)
    assert datetime.fromisoformat(anchor["valid_from"]) >= before
    registry = load_enrolled_trust_registry(
        anchor["registry_file"], expected_sha256=anchor["registry_sha256"],
    )
    require_enrolled_role_coverage(
        registry, roles=["liquidity_amounts", "global_signals"],
        valid_from=datetime.fromisoformat(anchor["valid_from"]),
        valid_until=datetime.fromisoformat(anchor["valid_until"]),
    )
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name, identifier in (("root", "trust_root_id"), ("publisher", "publisher_key_id")):
        keyfile = directory / f"{name}.key"
        assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
        key = Ed25519PrivateKey.from_private_bytes(keyfile.read_bytes())
        assert _key_id(_public_key(key)) == anchor[identifier]
    assert json.loads((directory / "trust-anchor.json").read_text()) == anchor
    original_key = (directory / "publisher.key").read_bytes()
    with pytest.raises(FileExistsError):
        initialize_local_publisher(directory)
    assert (directory / "publisher.key").read_bytes() == original_key
    other = initialize_local_publisher(tmp_path / "other")
    assert other["publisher_key_id"] != anchor["publisher_key_id"]


def test_registry_pin_must_be_fixed_outside_the_registry(tmp_path):
    anchor = initialize_local_publisher(tmp_path / "publisher")
    with pytest.raises(ValueError, match="expected pin"):
        load_enrolled_trust_registry(anchor["registry_file"], expected_sha256="f" * 64)
