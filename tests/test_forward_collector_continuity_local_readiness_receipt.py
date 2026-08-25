from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHANGE = ROOT / "openspec" / "changes" / "bind-forward-collector-continuity"
RECEIPT = CHANGE / "evidence" / "local-readiness-audit-2026-08-24.json"

TARGET_SCHEMA_COUNTS = {
    "rqgm-forward-panel-registration/4": 0,
    "stockdata-authority-envelope/1": 0,
    "stockdata-forward-collector-continuity-closure/1": 0,
    "stockdata-market-rules/1": 0,
    "stockdata-provider-component-source-receipt/1": 0,
    "stockdata-rqgm-provider-bundle/2": 0,
    "stockdata-trading-calendar/1": 0,
}
BLOCKER_CATEGORIES = {
    "economic_evidence",
    "immutable_judge",
    "production_trust_root_signer",
    "prospective_future_panel",
    "release_authorization",
    "signed_authority",
}
DATABASE_PATHS = {
    "/Users/cdzhangxueli/.stockdata/rqgm-forward-evidence.sqlite",
    "/Users/cdzhangxueli/.stockdata/rqgm-forward-evidence-smoke.sqlite",
    "/Users/cdzhangxueli/.stockdata/rqgm-forward-evidence-tencent-smoke.sqlite",
}
PANEL_SESSIONS = ["2026-08-13", "2026-08-14", "2026-08-17"]
PANEL_SYMBOLS = [
    "000001.SZ",
    "000333.SZ",
    "000725.SZ",
    "000858.SZ",
    "002415.SZ",
    "300750.SZ",
    "600030.SH",
    "600036.SH",
    "600276.SH",
    "600519.SH",
    "601166.SH",
    "601318.SH",
]
READINESS_COMPONENTS = {
    "availability_records",
    "corporate_actions",
    "decision_context",
    "execution_prices",
    "instrument_status",
    "market_rules",
    "signal_prices",
    "trading_calendar",
    "universe",
}
READINESS_BLOCKER_CODES = [
    "complete_component_availability_records_not_bound",
    "corporate_action_publisher_key_not_enrolled",
    "corporate_action_revisions_not_supported",
    "dividend_observation_not_full_corporate_action_ledger",
    "forward_universe_publisher_key_not_enrolled",
    "instrument_status_is_activity_proxy",
    "missing_corporate_action_coverage",
    "missing_decision_context_rows",
    "missing_finalized_context_rows",
    "missing_panel_rows",
    "official_rulebook_bundle_not_enrolled",
    "signed_session_calendar_not_enrolled",
    "signed_trading_calendar_not_enrolled",
]
RQGM_BINDINGS = {
    (
        "/Users/cdzhangxueli/workspaces/super-trader-rqgm/"
        "artifacts/economic-reproduction-readiness.json"
    ): {
        "schema_version": "rqgm-economic-reproduction-readiness/1",
        "status": "DATA_BLOCKED",
        "task": "14.3",
    },
    (
        "/Users/cdzhangxueli/workspaces/super-trader-rqgm/"
        "artifacts/execution-replay-readiness.json"
    ): {
        "schema_version": "rqgm-execution-replay-readiness/1",
        "status": "DATA_BLOCKED",
        "task": "8.6",
    },
    (
        "/Users/cdzhangxueli/workspaces/super-trader-rqgm/"
        "artifacts/independent-p0-p1-review.json"
    ): {
        "schema_version": "rqgm-independent-p0-p1-review/1",
        "task": "14.4",
    },
    (
        "/Users/cdzhangxueli/workspaces/super-trader-rqgm/"
        "artifacts/remaining-task-readiness.json"
    ): {
        "schema_version": "rqgm-remaining-task-readiness/3",
    },
}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_receipt() -> tuple[bytes, dict[str, object]]:
    raw = RECEIPT.read_bytes()
    payload = json.loads(
        raw.decode("ascii"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON value: {value}")
        ),
    )
    assert isinstance(payload, dict)
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False
    ).encode("ascii") + b"\n"
    assert raw == canonical
    return raw, payload


def test_local_readiness_receipt_is_exact_negative_evidence() -> None:
    raw, payload = _load_receipt()

    assert raw.endswith(b"\n")
    assert set(payload) == {
        "audit_environment",
        "authority_credit",
        "blockers",
        "change",
        "checked_at",
        "collector_databases",
        "continuity_gate",
        "decision",
        "evidence_grade_granted",
        "local_inventory",
        "prohibited_claims",
        "production_readiness_recompute",
        "production_registry",
        "rqgm_readiness_bindings",
        "schema_version",
        "target_schema_counts",
        "task",
    }
    assert payload["schema_version"] == "stockdata-forward-collector-local-readiness-audit/1"
    assert payload["change"] == "bind-forward-collector-continuity"
    assert payload["task"] == "5.6"
    assert payload["checked_at"] == "2026-08-24"

    environment = payload["audit_environment"]
    assert environment == {
        "discovery_method": (
            "parsed_schema_and_content over regular JSON files; no filename-based "
            "authority inference"
        ),
        "filesystem_writes": "DENIED",
        "network": "DENIED",
        "private_material_exposed": False,
    }
    assert payload["production_registry"] == {
        "path": str(ROOT / "stockdata" / "enrolled_trust_registry.json"),
        "pin_verified": True,
        "pinned_sha256": (
            "69b94b1d01cb8dd299db799fac657b78ce77a548d35753ab9dca1c9bf94aeec6"
        ),
        "schema_version": "stockdata-enrolled-trust-registry/1",
        "signer_enrollment_count": 0,
        "trust_root_count": 0,
    }
    assert payload["target_schema_counts"] == TARGET_SCHEMA_COUNTS

    assert payload["continuity_gate"] == {
        "can_admit_signed_authority": False,
        "can_change_component_boolean": False,
        "can_make_aggregate_ready": False,
        "can_populate_component": False,
        "can_reject_materialization_or_export": True,
        "can_satisfy_availability": False,
        "role": "NEGATIVE_PROVENANCE_GATE_ONLY",
    }
    blockers = payload["blockers"]
    assert isinstance(blockers, list)
    assert len(blockers) == 6
    assert {blocker["category"] for blocker in blockers} == BLOCKER_CATEGORIES
    for blocker in blockers:
        assert set(blocker) == {"category", "evidence", "status"}
        assert blocker["status"] == "BLOCKED"
        assert isinstance(blocker["evidence"], str) and blocker["evidence"]
    assert payload["decision"] == "DATA_BLOCKED"
    assert payload["authority_credit"] is False
    assert payload["evidence_grade_granted"] is None


def test_local_readiness_receipt_proves_all_collector_files_unchanged() -> None:
    _, payload = _load_receipt()
    databases = payload["collector_databases"]

    assert isinstance(databases, list)
    assert len(databases) == 3
    assert {record["path"] for record in databases} == DATABASE_PATHS
    for record in databases:
        assert set(record) == {
            "database_uuid",
            "fingerprint_unchanged",
            "genesis_present",
            "ledger_path",
            "ledger_present",
            "path",
            "post_audit_fingerprint",
            "pre_audit_fingerprint",
            "registration_v4_eligible",
        }
        assert record["genesis_present"] is False
        assert record["database_uuid"] is None
        assert record["ledger_present"] is False
        assert record["registration_v4_eligible"] is False
        assert record["ledger_path"] == f"{record['path']}.collector-ledger.jsonl"
        assert record["fingerprint_unchanged"] is True
        assert record["pre_audit_fingerprint"] == record["post_audit_fingerprint"]
        assert set(record["pre_audit_fingerprint"]) == {
            "mtime_ns",
            "sha256",
            "size_bytes",
            "st_dev",
            "st_ino",
        }

    inventory = payload["local_inventory"]
    assert set(inventory) == {
        "fingerprint_changes",
        "invalid_empty_json_files",
        "inventory_hash_contract",
        "inventory_sha256_after",
        "inventory_sha256_before",
        "json_files_examined",
        "json_files_parsed",
        "research_calendar_candidate",
        "roots",
    }
    assert inventory["fingerprint_changes"] == []
    assert inventory["inventory_sha256_before"] == inventory["inventory_sha256_after"]


def test_production_readiness_recompute_is_exact_and_read_only() -> None:
    _, payload = _load_receipt()
    recompute = payload["production_readiness_recompute"]

    assert set(recompute) == {
        "checked_parameters",
        "fingerprints",
        "input_database",
        "panel_sha256",
        "panel_size",
        "registration_path",
        "registration_schema",
        "report",
        "sessions",
        "symbols",
    }
    assert recompute["input_database"] == (
        "/Users/cdzhangxueli/.stockdata/rqgm-forward-evidence.sqlite"
    )
    assert recompute["registration_path"] == (
        "/Users/cdzhangxueli/workspaces/super-trader-rqgm/"
        "artifacts/forward-panel-registration-2026-08-12.json"
    )
    assert recompute["registration_schema"] == "rqgm-forward-panel-registration/1"
    assert recompute["sessions"] == PANEL_SESSIONS
    assert recompute["symbols"] == PANEL_SYMBOLS
    assert len(recompute["symbols"]) == 12
    assert recompute["panel_size"] == 36
    assert recompute["panel_sha256"] == (
        "75b63a8022f28444212bbb8b52c938f5aad81fb13454e76475fa9b54562d4e74"
    )
    assert recompute["checked_parameters"] == {
        "execution_adjustment": {
            "mode": "raw",
            "version": "tencent-qt-daily-v1",
        },
        "signal_adjustment": {
            "mode": "raw",
            "version": "tencent-qt-daily-v1",
        },
        "source": "tencent",
    }

    report = recompute["report"]
    assert set(report) == {
        "blocker_codes",
        "component_summaries",
        "ready",
        "schema_version",
    }
    assert report["schema_version"] == "stockdata-full-execution-readiness/1"
    assert report["ready"] is False
    assert report["blocker_codes"] == READINESS_BLOCKER_CODES
    assert report["blocker_codes"] == sorted(report["blocker_codes"])
    assert len(report["blocker_codes"]) == 13

    components = report["component_summaries"]
    assert set(components) == READINESS_COMPONENTS
    for name, component in components.items():
        expected_keys = {"blockers", "ready"}
        if name in {"execution_prices", "signal_prices"}:
            expected_keys.add("selected_rows")
            assert component["selected_rows"] == 12
        assert set(component) == expected_keys
        assert component["ready"] is False
        assert isinstance(component["blockers"], list) and component["blockers"]
        for blocker in component["blockers"]:
            assert set(blocker) == {"code", "count"}
            assert blocker["code"] in READINESS_BLOCKER_CODES
            assert type(blocker["count"]) is int and blocker["count"] > 0

    fingerprints = recompute["fingerprints"]
    assert set(fingerprints) == {"after", "before", "unchanged"}
    assert fingerprints["unchanged"] is True
    assert fingerprints["before"] == fingerprints["after"]
    for snapshot in (fingerprints["before"], fingerprints["after"]):
        assert set(snapshot) == {
            "database",
            "journal_exists",
            "shm_exists",
            "wal_exists",
        }
        assert snapshot["journal_exists"] is False
        assert snapshot["shm_exists"] is False
        assert snapshot["wal_exists"] is False
        assert set(snapshot["database"]) == {"mtime_ns", "sha256", "size_bytes"}


def test_local_readiness_receipt_binds_current_rqgm_artifacts_without_credit() -> None:
    _, payload = _load_receipt()
    bindings = payload["rqgm_readiness_bindings"]

    assert isinstance(bindings, list)
    assert len(bindings) == 4
    assert {binding["path"] for binding in bindings} == set(RQGM_BINDINGS)
    for binding in bindings:
        expected = RQGM_BINDINGS[binding["path"]]
        assert binding == {
            "path": binding["path"],
            "read_only": True,
            "schema_version": expected["schema_version"],
            "sha256": binding["sha256"],
            **({"status": expected["status"]} if "status" in expected else {}),
            **({"task": expected["task"]} if "task" in expected else {}),
        }
        assert len(binding["sha256"]) == 64
        assert all(character in "0123456789abcdef" for character in binding["sha256"])

    execution = next(
        binding
        for binding in bindings
        if binding["task"] == "8.6"
    )
    assert execution["schema_version"] == "rqgm-execution-replay-readiness/1"
    assert execution["status"] == "DATA_BLOCKED"

    assert payload["prohibited_claims"] == [
        "TASK_8_6_COMPLETE",
        "TASK_11_2_COMPLETE",
        "TASK_14_3_COMPLETE",
        "RELEASE_COMPLETE",
        "ALPHA_DEMONSTRATED",
        "PROFITABILITY_DEMONSTRATED",
        "RELEASED_ELITE",
    ]
