from datetime import datetime, timedelta, timezone
import json

from stockdata import local_main_buy_publisher as publisher
from stockdata.liquidity_amount_product import _canonical
from stockdata.main_buy_supplement import verify_main_buy_supplement
from test_main_buy_supplement import make_supplement
from test_provider_authority_publisher import _private_raw


def test_publisher_freezes_after_signing_without_backdating_source_facts(tmp_path, monkeypatch):
    fixture, pin, signer = make_supplement(asof="2026-09-04")
    identity = tmp_path / "identity"
    identity.mkdir()
    (identity / "registry.json").write_bytes(_canonical(fixture["registry"]))
    key = identity / "publisher.key"
    key.write_bytes(_private_raw(signer))
    key.chmod(0o600)
    capture = fixture["liquidity"]["product"]["source_receipts"][0]
    capture_path = tmp_path / "capture.json"
    capture_path.write_bytes(_canonical(capture))
    global_path = tmp_path / "global.json"
    global_path.write_bytes(_canonical(fixture["global_signals"]["snapshot"]))
    references = {component: {k: v for k, v in inputs.items() if k != "authority_envelope"}
                  for component, inputs in fixture["references"].items()}
    global_inputs = {k: v for k, v in fixture["global_signals"].items() if k != "authority_envelope"}
    monkeypatch.setattr(publisher, "prepare_reference_inputs", lambda **kwargs: (references, global_inputs))
    class Clock:
        tick = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz):
            cls.tick += timedelta(microseconds=1)
            return cls.tick
    monkeypatch.setattr(publisher, "datetime", Clock)
    result = publisher.publish_main_buy_supplement(evidence_dir=tmp_path,
        liquidity_capture_file=capture_path, global_snapshot_file=global_path,
        publisher_dir=identity, registry_sha256=pin, provider_manifest_sha256="a" * 64,
        output_dir=tmp_path / "output")
    payload = json.loads((tmp_path / "output" / "supplement.json").read_bytes())
    assert verify_main_buy_supplement(payload, expected_registry_sha256=pin,
        provider_manifest_sha256="a" * 64, asof="2026-09-04", decision_cutoff=result["decision_cutoff"]) == payload
    assert payload["liquidity"]["product"]["source_receipts"][0]["observed_at"] == capture["observed_at"]
    freeze = datetime.fromisoformat(payload["decision_cutoff"])
    inputs = [payload["liquidity"], payload["global_signals"], *payload["references"].values()]
    assert all(datetime.fromisoformat(item["authority_envelope"]["payload"]["available_at"]) < freeze for item in inputs)
