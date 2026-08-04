from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from stockdata.component_availability import (
    AVAILABILITY_RECORDS_SCHEMA,
    EVIDENCE_COMPONENTS,
    verify_component_availability_records,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _panel_sha256(panel: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(panel, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _artifact(panel: list[str], receipt_id: str) -> dict[str, object]:
    records = []
    for component in EVIDENCE_COMPONENTS:
        for panel_entry in panel:
            records.append(
                {
                    "component": component,
                    "panel_entry": panel_entry,
                    "record_sha256": _sha(f"{component}:{panel_entry}"),
                    "source_receipt_ids": [receipt_id],
                    "effective_at": f"{panel_entry.split('@')[1]}T00:00:00+08:00",
                    "available_at": f"{panel_entry.split('@')[1]}T09:00:00+08:00",
                    "decision_cutoff_at": f"{panel_entry.split('@')[1]}T09:25:00+08:00",
                }
            )
    return {
        "schema_version": AVAILABILITY_RECORDS_SCHEMA,
        "panel": panel,
        "records": records,
    }


def _verify(artifact, panel, receipts):
    return verify_component_availability_records(
        artifact,
        expected_panel_sha256=_panel_sha256(panel),
        expected_panel_size=len(panel),
        expected_decision_cutoffs={
            panel_entry: f"{panel_entry.split('@')[1]}T09:25:00+08:00"
            for panel_entry in panel
        },
        bound_source_receipt_ids=receipts,
    )


def test_complete_exact_panel_availability_verifies() -> None:
    panel = ["000001.SZ@2026-01-02", "600000.SH@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)

    verified = _verify(artifact, panel, [receipt_id])

    assert verified.panel_size == 2
    assert verified.source_receipt_ids == (receipt_id,)
    assert verified.artifact_sha256 == hashlib.sha256(
        json.dumps(
            artifact,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unordered"])
def test_component_panel_coverage_must_be_complete_unique_and_sorted(mutation) -> None:
    panel = ["000001.SZ@2026-01-02", "600000.SH@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)
    if mutation == "missing":
        artifact["records"].pop()
        message = "do not cover"
    elif mutation == "duplicate":
        artifact["records"].append(deepcopy(artifact["records"][-1]))
        message = "sorted and unique"
    else:
        artifact["records"][0], artifact["records"][1] = (
            artifact["records"][1],
            artifact["records"][0],
        )
        message = "sorted and unique"

    with pytest.raises(ValueError, match=message):
        _verify(artifact, panel, [receipt_id])


def test_unbound_or_modified_source_response_receipt_is_rejected() -> None:
    panel = ["000001.SZ@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)

    with pytest.raises(ValueError, match="invalid or unbound"):
        _verify(artifact, panel, [_sha("different-response")])


def test_post_cutoff_availability_is_rejected() -> None:
    panel = ["000001.SZ@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)
    artifact["records"][0]["available_at"] = "2026-01-02T09:25:01+08:00"

    with pytest.raises(ValueError, match="post-cutoff"):
        _verify(artifact, panel, [receipt_id])


def test_artifact_cannot_move_the_authoritative_cutoff() -> None:
    panel = ["000001.SZ@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)
    artifact["records"][0]["decision_cutoff_at"] = (
        "2026-01-02T10:25:00+08:00"
    )

    with pytest.raises(ValueError, match="authoritative decision cutoff"):
        _verify(artifact, panel, [receipt_id])


def test_effective_time_may_follow_availability_for_preannounced_events() -> None:
    panel = ["000001.SZ@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)
    corporate_action = next(
        record
        for record in artifact["records"]
        if record["component"] == "corporate_actions"
    )
    corporate_action["effective_at"] = "2026-01-10T00:00:00+08:00"

    assert _verify(artifact, panel, [receipt_id]).panel_size == 1


@pytest.mark.parametrize("mutation", ["subset", "superset"])
def test_panel_subset_or_superset_cannot_satisfy_bound_panel(mutation) -> None:
    panel = ["000001.SZ@2026-01-02", "600000.SH@2026-01-02"]
    receipt_id = _sha("source-response")
    artifact = _artifact(panel, receipt_id)
    artifact["panel"] = (
        ["000001.SZ@2026-01-02"]
        if mutation == "subset"
        else [*panel, "600001.SH@2026-01-02"]
    )

    with pytest.raises(ValueError, match="differs from the exact panel"):
        _verify(artifact, panel, [receipt_id])
