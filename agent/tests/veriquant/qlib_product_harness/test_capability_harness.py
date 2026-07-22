from __future__ import annotations

import json

from src.veriquant.qlib_expression.api import STATIC_QLIB_OPERATOR_MANIFEST
from src.veriquant.qlib_product_harness import QlibProductCapabilityHarness, build_semantic_templates


def test_broad_official_operator_templates_are_classified_and_certified(tmp_path) -> None:
    catalog = tmp_path / "result.json"
    catalog.write_text(json.dumps({
        "operational_smoke_status": "STATIC_461_PROJECTION_REPLAY_PASSED",
        "artifact_hashes": {"projection_hash": "sha256:" + "a" * 64},
    }), encoding="utf-8")
    report = QlibProductCapabilityHarness(catalog_result_path=catalog).run()

    assert report.pinned_operator_count == len(STATIC_QLIB_OPERATOR_MANIFEST.operators)
    assert report.classified_operator_count == report.pinned_operator_count
    assert report.semantic_template_count >= 150
    assert report.failed_template_count == 0
    assert report.catalog_record_count == 461
    assert report.catalog_classification_complete is True
    assert report.passed


def test_harness_is_stable_and_missing_catalog_fails_closed() -> None:
    templates = build_semantic_templates()
    assert templates == build_semantic_templates()
    first = QlibProductCapabilityHarness().run()
    second = QlibProductCapabilityHarness().run()
    assert first.report_hash == second.report_hash
    assert first.catalog_classification_complete is None
    assert not first.passed


def test_templates_cover_every_non_excluded_operator() -> None:
    covered = {operator for _, operator, _ in build_semantic_templates()}
    expected = {
        item.name for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust.value != "EXCLUDED"
    }
    assert covered == expected
