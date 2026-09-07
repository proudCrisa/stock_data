from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta

import pytest

from stockdata import candidate_admission_capture as capture

ASOF = "2026-08-28"
OBSERVED = "2026-08-28T14:00:00+08:00"


def _record(code: str, _asof: str) -> dict:
    return {
        "code": code,
        "finance_rows": [{
            "stat_date": "2026-03-31",
            "publish_date": "2026-04-30",
            "metric_methods": {
                "net_profit_ytd": "provider_netProfit",
                "revenue_ytd": "provider_MBRevenue",
                "ocf_ytd": "derived_net_profit_mul_CFOToNP",
                "roe_ytd_pct": "provider_roeAvg_ratio_mul_100",
            },
        }],
        "industry_rows": [{"update_date": "2026-08-24", "industry": "J66"}],
        "corporate_action_rows": [],
    }


def _signal_history(code: str = "sh600000") -> dict:
    start = "2026-05-01"
    days = [(date.fromisoformat(ASOF) - timedelta(days=offset)).isoformat()
            for offset in range(24, -1, -1)]
    identity = {
        "schema_version": capture.SIGNAL_ADJUSTMENT_SCHEMA,
        "price_role": "signal", "source": capture.TENCENT_HISTORY_SOURCE,
        "adjustment_mode": "qfq",
        "adjustment_version": f"{capture.TENCENT_HISTORY_SOURCE}-qfq",
    }
    rows = [{
        "date": day, "open": 10.0, "close": 10.1, "high": 10.2,
        "low": 9.9, "volume": 1000.0,
        "source": capture.TENCENT_HISTORY_SOURCE, "adjustment_mode": "qfq",
        "adjustment_version": f"{capture.TENCENT_HISTORY_SOURCE}-qfq",
        "retrieved_at": OBSERVED, "is_final": True,
    } for day in days]
    receipt = {
        "source": capture.TENCENT_HISTORY_SOURCE, "observed_at": OBSERVED,
        "request": {"code": code, "start": start, "end": ASOF,
                    "adjustment_mode": "qfq"},
        "response": {"pages": [], "bar_count": len(rows),
                     "coverage_start": days[0], "coverage_end": ASOF},
    }
    return {
        "identity": identity, "identity_sha256": capture._sha256(identity),
        "requested_start": start, "requested_end": ASOF, "watermark": ASOF,
        "rows_sha256": capture._sha256(rows),
        "receipt_sha256": capture._sha256(receipt),
        "rows": rows, "receipt": receipt,
    }


def _capture() -> dict:
    return capture.build_capture(
        ["sh600000"], asof=ASOF, capture_one=_record, observed_at=OBSERVED
    )


def _set_now(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    fixed = datetime.fromisoformat(value)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(capture, "datetime", FixedDateTime)


def test_capture_is_bounded_sealed_and_explicitly_non_authoritative():
    artifact = _capture()

    assert capture.verify_capture(artifact) == artifact
    assert artifact["authority"] == {
        "status": "observed_provider_response",
        "execution_grade": False,
        "decision_authority": False,
        "publisher_authenticated": False,
        "historical_replay_grade": False,
        "revision_complete": False,
    }
    assert artifact["records"][0]["observed_at"] == OBSERVED
    assert artifact["source_receipt"]["requested_codes_sha256"]
    assert artifact["source_receipt"]["query_contract"]["corporate_actions"][
        "coverage_days"] == 90


def test_capture_closes_partial_coverage_with_a_blocker():
    def source(code: str, asof: str) -> dict:
        if code == "sz000001":
            raise RuntimeError("provider unavailable")
        return _record(code, asof)

    artifact = capture.build_capture(
        ["sh600000", "sz000001"], asof=ASOF, capture_one=source,
        observed_at=OBSERVED,
    )

    assert [row["code"] for row in artifact["records"]] == ["sh600000"]
    assert artifact["blockers"][0]["code"] == "sz000001"
    capture.verify_capture(artifact)


def test_capture_rejects_unbounded_duplicate_and_cross_day_requests():
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="unique"):
        capture.build_capture(
            ["sh600000", "sh600000"], asof=ASOF, capture_one=_record,
            observed_at=OBSERVED,
        )
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="1..20"):
        capture.build_capture(
            [f"sh60{index:04d}" for index in range(21)], asof=ASOF,
            capture_one=_record, observed_at=OBSERVED,
        )
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="observed on asof"):
        capture.build_capture(
            ["sh600000"], asof=ASOF, capture_one=_record,
            observed_at="2026-08-27T14:00:00+08:00",
        )


def test_publish_is_content_addressed_and_tampering_is_rejected(tmp_path):
    artifact = _capture()
    path = capture.publish_capture(artifact, tmp_path)

    assert artifact["artifact_sha256"] in path.name
    assert capture.load_capture(path) == artifact
    tampered = copy.deepcopy(artifact)
    tampered["records"][0]["industry_rows"][0]["industry"] = "J67"
    path.write_text(
        json.dumps(tampered, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="identity"):
        capture.load_capture(path)


def test_source_receipt_scope_is_bound_even_after_resealing():
    artifact = _capture()
    artifact["source_receipt"]["requested_codes_sha256"] = "0" * 64
    artifact["artifact_sha256"] = capture._sha256({
        key: value for key, value in artifact.items() if key != "artifact_sha256"
    })

    with pytest.raises(capture.CandidateAdmissionCaptureError, match="scope"):
        capture.verify_capture(artifact)


def test_metric_normalization_method_is_required_even_after_resealing():
    artifact = _capture()
    artifact["records"][0]["finance_rows"][0]["metric_methods"].pop("revenue_ytd")
    artifact["artifact_sha256"] = capture._sha256({
        key: value for key, value in artifact.items() if key != "artifact_sha256"
    })

    with pytest.raises(capture.CandidateAdmissionCaptureError, match="normalization"):
        capture.verify_capture(artifact)


def test_corporate_action_requires_valid_qfq_signal_history():
    def source(code: str, asof: str) -> dict:
        record = _record(code, asof)
        record["corporate_action_rows"] = [{"event_date": "2026-07-16"}]
        record["signal_price_history"] = _signal_history(code)
        return record

    artifact = capture.build_capture(
        ["sh600000"], asof=ASOF, capture_one=source, observed_at=OBSERVED
    )
    assert capture.verify_capture(artifact) == artifact

    malformed = copy.deepcopy(artifact)
    malformed["records"][0]["signal_price_history"]["watermark"] = "2026-08-27"
    malformed["artifact_sha256"] = capture._sha256({
        key: value for key, value in malformed.items() if key != "artifact_sha256"
    })
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="signal"):
        capture.verify_capture(malformed)

    negative = copy.deepcopy(artifact)
    history = negative["records"][0]["signal_price_history"]
    history["rows"][0]["close"] = -1
    history["rows_sha256"] = capture._sha256(history["rows"])
    negative["artifact_sha256"] = capture._sha256({
        key: value for key, value in negative.items() if key != "artifact_sha256"
    })
    with pytest.raises(capture.CandidateAdmissionCaptureError, match="row"):
        capture.verify_capture(negative)


@pytest.mark.parametrize("current_time", ["14:59:59", "15:00:00", "15:00:59"])
def test_same_day_qfq_signal_history_rejects_through_1500_minute(
    monkeypatch: pytest.MonkeyPatch, current_time: str,
):
    _set_now(monkeypatch, f"{ASOF}T{current_time}+08:00")
    monkeypatch.setattr(
        capture,
        "fetch_tencent_history",
        lambda *_args, **_kwargs: pytest.fail("unfinalized history was fetched"),
    )

    with pytest.raises(
        capture.CandidateAdmissionCaptureError, match="finalized admission day"
    ):
        capture._signal_history("sh600000", ASOF)


def test_same_day_qfq_signal_history_allows_at_1501(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_now(monkeypatch, f"{ASOF}T15:01:00+08:00")
    expected = _signal_history()

    class CapturedHistory(list):
        capture_receipt = expected["receipt"]

    def fetch(code: str, start: str, end: str, *, adjustment_mode: str):
        assert (code, end, adjustment_mode) == ("sh600000", ASOF, "qfq")
        assert start == (date.fromisoformat(ASOF) - timedelta(days=180)).isoformat()
        return CapturedHistory(expected["rows"])

    monkeypatch.setattr(capture, "fetch_tencent_history", fetch)

    history = capture._signal_history("sh600000", ASOF)

    assert history["watermark"] == ASOF
    assert history["rows"][-1]["date"] == ASOF
