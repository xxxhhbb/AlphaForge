from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.research500_harness import (
    RepresentativePreflight,
    RepresentativeResearchHarness,
    load_catalog,
    select_representatives,
)
from veriquant.research_toolbox.contracts import canonical_hash


ROOT = Path(__file__).resolve().parents[4]
CATALOG = ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"


def test_minimum_suite_covers_25_families_and_20_dimensions() -> None:
    catalog = load_catalog(CATALOG)
    selected = select_representatives(catalog)
    assert len(selected) == 25
    assert {item.family_id for item in selected} == set(catalog.families)
    assert {item.test_id for item in selected} == set(catalog.dimensions)
    assert len({item.question_id for item in selected}) == 25


def test_plan_is_stable_and_partitioned_into_five_batches(tmp_path: Path) -> None:
    first = RepresentativeResearchHarness().initialize(catalog_path=CATALOG, output_dir=tmp_path / "a")
    second = RepresentativeResearchHarness().initialize(catalog_path=CATALOG, output_dir=tmp_path / "b")
    assert first == second
    assert (tmp_path / "a" / "representative_plan.json").read_bytes() == (tmp_path / "b" / "representative_plan.json").read_bytes()
    assert {row["batch_id"] for row in first["rows"]} == {"B01", "B02", "B03", "B04", "B05"}
    assert all(sum(row["batch_id"] == batch for row in first["rows"]) == 5 for batch in {row["batch_id"] for row in first["rows"]})


def test_non_real_or_empty_receipt_fails_closed(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path)
    receipt = tmp_path / "bad.json"
    receipt.write_text(json.dumps({"schema_version": "fixture", "status": "PASSED"}), encoding="utf-8")
    with pytest.raises(ValueError, match="real E2E"):
        harness.record_receipt(output_dir=tmp_path, question_id=plan["rows"][0]["question_id"], receipt_path=receipt)


def test_passed_receipt_is_idempotent_and_content_bound(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path)
    report = tmp_path / "report.md"
    report.write_text("# 客户研究报告\n\n真实机器结论。\n", encoding="utf-8")
    receipt_payload = {
        "schema_version": "alphaforge.first-real-e2e-canary-receipt/v1",
        "status": "PASSED",
        "canary_passed": True,
        "failure_code": None,
        "question_hash": plan["rows"][0]["question_hash"],
        "output_kind": plan["rows"][0]["required_output_kind"],
        "evidence_authority": "OFFICIAL_EVIDENCE",
        "source_hash": "sha256:" + "1" * 64,
        "review_hash": "sha256:" + "2" * 64,
        "runner_receipt_hash": "sha256:" + "3" * 64,
        "evidence_result_hash": "sha256:" + "4" * 64,
        "official_evidence_hash": "sha256:" + "5" * 64,
        "report_hash": "sha256:" + "6" * 64,
        "report_path": str(report.resolve()),
    }
    receipt_payload["artifact_hash"] = canonical_hash(receipt_payload)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    question_id = plan["rows"][0]["question_id"]
    first = harness.record_receipt(output_dir=tmp_path, question_id=question_id, receipt_path=receipt)
    second = harness.record_receipt(output_dir=tmp_path, question_id=question_id, receipt_path=receipt)
    assert first == second
    progress = json.loads((tmp_path / "representative_progress.json").read_text(encoding="utf-8"))
    assert progress["passed_count"] == 1
    assert progress["status"] == "IN_PROGRESS"


def test_auxiliary_standard_table_receipt_must_not_claim_official_evidence(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path)
    row = next(item for item in plan["rows"] if item["required_output_kind"] == "RegimeComparisonTable")
    report = tmp_path / "auxiliary-report.md"
    report.write_text("# 客户研究报告\n\n研究级状态比较。\n", encoding="utf-8")
    payload = {
        "schema_version": "alphaforge.first-real-e2e-canary-receipt/v1",
        "status": "PASSED", "canary_passed": True, "failure_code": None,
        "question_hash": row["question_hash"], "output_kind": row["required_output_kind"],
        "evidence_authority": "AUXILIARY_ONLY",
        "source_hash": "sha256:" + "1" * 64,
        "review_hash": "sha256:" + "2" * 64,
        "runner_receipt_hash": "sha256:" + "3" * 64,
        "evidence_result_hash": "sha256:" + "4" * 64,
        "official_evidence_hash": None,
        "report_hash": "sha256:" + "6" * 64,
        "report_path": str(report.resolve()),
    }
    payload["artifact_hash"] = canonical_hash(payload)
    receipt = tmp_path / "auxiliary-receipt.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    committed = harness.record_receipt(output_dir=tmp_path, question_id=row["question_id"], receipt_path=receipt)
    assert next(item for item in committed["rows"] if item["question_id"] == row["question_id"])["status"] == "PASSED"
    payload["official_evidence_hash"] = "sha256:" + "5" * 64
    payload["artifact_hash"] = canonical_hash(payload)
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    harness.initialize(catalog_path=CATALOG, output_dir=tmp_path / "fresh")
    with pytest.raises(ValueError, match="cannot claim official"):
        harness.record_receipt(output_dir=tmp_path / "fresh", question_id=row["question_id"], receipt_path=receipt)


def test_preflight_checks_projected_fields_not_only_source_receipts(tmp_path: Path) -> None:
    plan = RepresentativeResearchHarness().initialize(catalog_path=CATALOG, output_dir=tmp_path / "plan")
    frozen = {
        "snapshot": {
            "fields": [
                "open", "high", "low", "close", "volume", "amount", "adj_factor",
                "market_cap", "turnover_rate", "pe_ttm", "pb",
            ]
        }
    }
    frozen["snapshot"]["coverage_by_field"] = {field: 1.0 for field in frozen["snapshot"]["fields"]}
    frozen_path = tmp_path / "frozen.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    official_ids = {
        item for row in plan["rows"] for item in row["required_data"]
    } | {"daily_basic", "registered_evidence", "registered_factor_summary", "stock_basic", "trade_cal"}
    official_registry = {
        "adj_factor", "balancesheet", "cashflow", "daily", "daily_basic", "express",
        "fina_indicator", "forecast", "income", "index_daily", "moneyflow", "shibor",
        "stock_basic", "trade_cal",
    }
    gateway_ids = sorted(official_ids & official_registry)
    coverage = {
        "capability_report": {"status": "READY", "artifact_hash": "sha256:" + "a" * 64},
        "gateway_manifest": {
            "status": "READY",
            "artifact_hash": "sha256:" + "b" * 64,
            "receipts": [
                {
                    "source_id": item, "status": "SUCCESS", "authority": "OFFICIAL_INPUT",
                    "content_hash": canonical_hash({"source": item}),
                }
                for item in gateway_ids
            ],
        },
        "auxiliary_receipts": [
            {
                "source_id": item, "status": "SUCCESS", "authority": "AUXILIARY_RESEARCH_EVIDENCE",
                "content_hash": canonical_hash({"auxiliary": item}),
            }
            for item in ("documents", "dragon_tiger", "northbound", "research_reports", "stock_news")
        ],
        "registered_context_receipts": [
            {"data_id": item, "status": "READY", "content_hash": canonical_hash({"registered": item})}
            for item in ("portfolio_returns", "registered_evidence", "registered_factor_summary")
        ],
    }
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    result = RepresentativePreflight().run(
        plan_path=tmp_path / "plan" / "representative_plan.json",
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight.json",
    )
    by_id = {row["question_id"]: row for row in result["rows"]}
    assert by_id["Q001"]["status"] == "READY"
    assert by_id["Q085"]["status"] == "BLOCKED"
    assert by_id["Q085"]["missing_feature_fields"] == ["industry"]
    assert "PROJECTED_FIELD_MISSING" in by_id["Q085"]["failure_codes"]
    assert by_id["Q106"]["status"] == "BLOCKED"
    assert "RUNTIME_SURFACE_MISSING" in by_id["Q106"]["failure_codes"]
    assert by_id["Q400"]["status"] == "BLOCKED"
    assert "RUNTIME_SURFACE_MISSING" in by_id["Q400"]["failure_codes"]
    assert json.loads((tmp_path / "preflight.json").read_text(encoding="utf-8")) == result


def test_preflight_accepts_frozen_panel_semantic_field_aliases(tmp_path: Path) -> None:
    plan = RepresentativeResearchHarness().initialize(catalog_path=CATALOG, output_dir=tmp_path / "plan")
    frozen = {
        "snapshot": {
            "fields": [
                "open", "high", "low", "close", "volume", "amount", "adj_factor",
                "market_cap", "turnover_rate", "pe_ttm", "pb", "index_pct_change",
                "buy_extra_large_amount", "buy_large_amount", "net_moneyflow_amount",
                "sell_extra_large_amount", "sell_large_amount", "shibor_3m",
                "asset_turnover", "gross_margin", "operating_cashflow", "net_income",
                "net_income_yoy", "revenue_yoy", "current_ratio", "debt_to_assets",
                "roe", "roa", "revenue",
            ]
        }
    }
    frozen["snapshot"]["coverage_by_field"] = {field: 1.0 for field in frozen["snapshot"]["fields"]}
    frozen_path = tmp_path / "frozen-alias.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    official_ids = {
        item for row in plan["rows"] for item in row["required_data"]
    } | {"daily_basic", "stock_basic", "trade_cal"}
    official_registry = {
        "adj_factor", "balancesheet", "cashflow", "daily", "daily_basic", "express",
        "fina_indicator", "forecast", "income", "index_daily", "moneyflow", "shibor",
        "stock_basic", "trade_cal",
    }
    coverage = {
        "capability_report": {"status": "READY", "artifact_hash": "sha256:" + "a" * 64},
        "gateway_manifest": {
            "status": "READY", "artifact_hash": "sha256:" + "b" * 64,
            "receipts": [
                {"source_id": item, "status": "SUCCESS", "authority": "OFFICIAL_INPUT", "content_hash": canonical_hash({"source": item})}
                for item in sorted(official_ids & official_registry)
            ],
        },
        "registered_context_receipts": [
            {"data_id": item, "status": "READY", "content_hash": canonical_hash({"registered": item})}
            for item in ("portfolio_returns", "registered_evidence", "registered_factor_summary")
        ],
    }
    coverage_path = tmp_path / "coverage-alias.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    result = RepresentativePreflight().run(
        plan_path=tmp_path / "plan" / "representative_plan.json",
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight-alias.json",
    )
    by_id = {row["question_id"]: row for row in result["rows"]}
    assert "benchmark_return" not in by_id["Q127"]["missing_feature_fields"]
    assert by_id["Q379"]["missing_feature_fields"] == []
    assert by_id["Q422"]["missing_feature_fields"] == []
    assert by_id["Q253"]["missing_feature_fields"] == []
    assert by_id["Q274"]["missing_feature_fields"] == []
    assert by_id["Q295"]["missing_feature_fields"] == []
    assert by_id["Q316"]["missing_feature_fields"] == []
    frozen["snapshot"]["coverage_by_field"]["index_pct_change"] = 0.0
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    zero = RepresentativePreflight().run(
        plan_path=tmp_path / "plan" / "representative_plan.json",
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight-zero.json",
    )
    zero_by_id = {row["question_id"]: row for row in zero["rows"]}
    assert "benchmark_return" in zero_by_id["Q127"]["missing_feature_fields"]


def test_exhausted_blocked_case_can_be_replaced_without_losing_25x20_cover(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path)
    old = plan["rows"][0]
    replacement = next(
        item for item in load_catalog(CATALOG).questions
        if item.family_id == old["family_id"] and item.test_id == "T02"
    )
    blocked = {
        "status": "BLOCKED", "canary_passed": False,
        "question_hash": old["question_hash"], "output_kind": old["required_output_kind"],
    }
    blocked["artifact_hash"] = canonical_hash(blocked)
    receipt = tmp_path / "blocked.json"
    receipt.write_text(json.dumps(blocked), encoding="utf-8")
    harness.record_failure(
        output_dir=tmp_path, question_id=old["question_id"], receipt_path=receipt,
        attempt_count=3, failure_code="PROVIDER_SCHEMA_REJECTED",
    )
    replaced = harness.replace_blocked_representative(
        catalog_path=CATALOG, output_dir=tmp_path,
        blocked_question_id=old["question_id"], replacement_question_id=replacement.question_id,
        reason_code="EXHAUSTED_SCHEMA_ATTEMPTS",
    )
    assert len(replaced["rows"]) == 25
    assert len({row["family_id"] for row in replaced["rows"]}) == 25
    assert len({row["test_id"] for row in replaced["rows"]}) == 20
    assert replaced["superseded_rows"][0]["question_id"] == old["question_id"]
    assert replacement.question_id in {row["question_id"] for row in replaced["rows"]}


def test_content_addressed_preflight_blocker_avoids_wasting_real_trials(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path)
    old = next(row for row in plan["rows"] if row["question_id"] == "Q085")
    preflight = {
        "schema_version": "alphaforge.research500-representative-preflight/v1",
        "plan_hash": plan["artifact_hash"],
        "rows": [{
            "question_id": old["question_id"],
            "status": "BLOCKED",
            "failure_codes": ["PROJECTED_FIELD_MISSING"],
        }],
    }
    preflight["artifact_hash"] = canonical_hash(preflight)
    preflight_path = tmp_path / "preflight.json"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")
    replaced = harness.replace_preflight_blocked_representative(
        catalog_path=CATALOG,
        output_dir=tmp_path,
        blocked_question_id="Q085",
        replacement_question_id="Q098",
        preflight_path=preflight_path,
    )
    assert len(replaced["rows"]) == 25
    assert len({row["family_id"] for row in replaced["rows"]}) == 25
    assert len({row["test_id"] for row in replaced["rows"]}) == 20
    replacement = next(row for row in replaced["rows"] if row["question_id"] == "Q098")
    assert replacement["attempt_count"] == 0
    assert replaced["superseded_rows"][0]["preflight_hash"].startswith("sha256:")

    preflight_path.write_text(preflight_path.read_text(encoding="utf-8").replace("PROJECTED", "TAMPERED"), encoding="utf-8")
    fresh = tmp_path / "fresh"
    harness.initialize(catalog_path=CATALOG, output_dir=fresh)
    with pytest.raises(ValueError, match="content-addressed"):
        harness.replace_preflight_blocked_representative(
            catalog_path=CATALOG,
            output_dir=fresh,
            blocked_question_id="Q085",
            replacement_question_id="Q098",
            preflight_path=preflight_path,
        )


def test_auxiliary_event_proxy_can_run_but_scoretable_stays_fail_closed(tmp_path: Path) -> None:
    plan = RepresentativeResearchHarness().initialize(catalog_path=CATALOG, output_dir=tmp_path / "plan")
    frozen = {
        "snapshot": {
            "fields": ["close", "volume", "earnings_yield", "net_income_yoy", "revenue_yoy"],
            "coverage_by_field": {
                "close": 1.0,
                "volume": 1.0,
                "earnings_yield": 0.2,
                "net_income_yoy": 0.2,
                "revenue_yoy": 0.2,
            },
        }
    }
    frozen_path = tmp_path / "frozen-proxy.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    coverage = {
        "capability_report": {"status": "READY", "artifact_hash": "sha256:" + "a" * 64},
        "gateway_manifest": {"status": "READY", "artifact_hash": "sha256:" + "b" * 64, "receipts": []},
        "auxiliary_receipts": [],
        "registered_context_receipts": [
            {"data_id": name, "status": "READY", "content_hash": canonical_hash({"registered": name})}
            for name in ("registered_evidence", "registered_factor_summary")
        ],
        "external_approval_needs": [],
    }
    coverage_path = tmp_path / "coverage-proxy.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    result = RepresentativePreflight().run(
        plan_path=tmp_path / "plan" / "representative_plan.json",
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight-proxy.json",
        available_runtime_surfaces=("frozen_panel", "registered_context"),
    )
    by_id = {row["question_id"]: row for row in result["rows"]}
    assert by_id["Q337"]["status"] == "READY"
    assert by_id["Q337"]["research_input_mode"] == "AUXILIARY_PROXY"
    assert by_id["Q337"]["proxy_disclosure_required"] is True
    assert by_id["Q358"]["status"] == "BLOCKED"
    assert "OFFICIAL_SOURCE_REQUIRED" in by_id["Q358"]["failure_codes"]


def test_industry_is_required_by_neutralization_dimension_not_low_volatility_family(tmp_path: Path) -> None:
    harness = RepresentativeResearchHarness()
    plan = harness.initialize(catalog_path=CATALOG, output_dir=tmp_path / "plan")
    preflight = {
        "schema_version": "alphaforge.research500-representative-preflight/v1",
        "plan_hash": plan["artifact_hash"],
        "rows": [{
            "question_id": "Q085",
            "status": "BLOCKED",
            "failure_codes": ["PROJECTED_FIELD_MISSING"],
        }],
    }
    preflight["artifact_hash"] = canonical_hash(preflight)
    preflight_path = tmp_path / "blocked-industry.json"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")
    harness.replace_preflight_blocked_representative(
        catalog_path=CATALOG,
        output_dir=tmp_path / "plan",
        blocked_question_id="Q085",
        replacement_question_id="Q098",
        preflight_path=preflight_path,
    )
    frozen = {
        "snapshot": {
            "fields": ["close", "volume", "index_pct_change"],
            "coverage_by_field": {"close": 1.0, "volume": 1.0, "index_pct_change": 1.0},
        }
    }
    frozen_path = tmp_path / "frozen-low-vol.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    selected = json.loads((tmp_path / "plan" / "representative_plan.json").read_text(encoding="utf-8"))
    source_ids = sorted({source for row in selected["rows"] for source in row["required_data"]})
    coverage = {
        "capability_report": {"status": "READY", "artifact_hash": "sha256:" + "a" * 64},
        "gateway_manifest": {
            "status": "READY",
            "artifact_hash": "sha256:" + "b" * 64,
            "receipts": [
                {"source_id": source, "status": "SUCCESS", "authority": "OFFICIAL_INPUT", "content_hash": canonical_hash({"source": source})}
                for source in source_ids
            ],
        },
        "auxiliary_receipts": [],
        "registered_context_receipts": [],
        "external_approval_needs": [],
    }
    coverage_path = tmp_path / "coverage-low-vol.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    result = RepresentativePreflight().run(
        plan_path=tmp_path / "plan" / "representative_plan.json",
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight-low-vol.json",
    )
    q098 = next(row for row in result["rows"] if row["question_id"] == "Q098")
    q485 = next(row for row in result["rows"] if row["question_id"] == "Q485")
    assert "industry" not in q098["required_feature_fields"]
    assert q098["missing_feature_fields"] == []
    assert "industry" in q485["required_feature_fields"]


def test_official_scoretable_allows_registered_context_as_read_only_comparator(tmp_path: Path) -> None:
    plan = {
        "artifact_hash": canonical_hash({"plan": "registered-context-comparator"}),
        "rows": [{
            "question_id": "Q485",
            "question_hash": canonical_hash({"question": "industry-neutral composite"}),
            "family_id": "F25",
            "test_id": "T05",
            "required_data": ["portfolio_returns", "registered_evidence", "registered_factor_summary"],
            "required_output_kind": "ScoreTable",
        }],
    }
    frozen = {
        "snapshot": {"fields": ["industry"], "coverage_by_field": {"industry": 0.96}}
    }
    coverage = {
        "capability_report": {"status": "READY", "artifact_hash": "sha256:" + "a" * 64},
        "gateway_manifest": {
            "status": "READY",
            "artifact_hash": "sha256:" + "b" * 64,
            "receipts": [{
                "source_id": "stock_basic", "status": "SUCCESS",
                "authority": "OFFICIAL_INPUT", "content_hash": "sha256:" + "c" * 64,
            }],
        },
        "auxiliary_receipts": [],
        "registered_context_receipts": [
            {"data_id": item, "status": "READY", "content_hash": canonical_hash({"id": item})}
            for item in ("portfolio_returns", "registered_evidence", "registered_factor_summary")
        ],
        "external_approval_needs": [],
    }
    plan_path = tmp_path / "plan.json"
    frozen_path = tmp_path / "frozen.json"
    coverage_path = tmp_path / "coverage.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    result = RepresentativePreflight().run(
        plan_path=plan_path,
        frozen_data_path=frozen_path,
        coverage_inputs_path=coverage_path,
        output_path=tmp_path / "preflight.json",
        available_runtime_surfaces=("frozen_panel", "registered_context"),
    )
    row = result["rows"][0]
    assert row["status"] == "READY"
    assert row["source_coverage_status"] == "RESEARCH_READY"
    assert row["official_source_coverage_status"] == "OFFICIAL_READY"
    assert row["official_required_data"] == ["stock_basic"]
