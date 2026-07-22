from __future__ import annotations

import json

from veriquant.product_stability_harness import finalize
from veriquant.product_stability_harness.finalize import ProductFinalizationHarness
from veriquant.research_data_gateway.models import canonical_hash


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _bound(path, payload):
    value = dict(payload)
    value["artifact_hash"] = canonical_hash(value)
    _write(path, value)


def _harness(tmp_path, monkeypatch, *, source="value = 1\n"):
    monkeypatch.setattr(finalize, "_route_findings", lambda: [])
    source_root = tmp_path / "trusted"
    source_root.mkdir()
    (source_root / "module.py").write_text(source, encoding="utf-8")
    qualification = tmp_path / "receipts" / "qualification.json"
    dynamic = tmp_path / "receipts" / "dynamic.json"
    representative = tmp_path / "representative.json"
    coverage = tmp_path / "coverage.json"
    _bound(qualification, {
        "status": "QUALIFIED_FOR_RESEARCH_PRODUCT",
        "official_research_qualified_count": 5,
        "auxiliary_research_qualified_count": 1,
        "blocked_groups": [],
    })
    _bound(dynamic, {
        "status": "PARTIAL",
        "authority_ceiling": "AUXILIARY_ONLY",
        "recovery_cursor": "COMPLETE",
        "document_count": 3,
        "temporally_covered_symbol_count": 0,
        "failure_codes": ["TOOL_DATA_OUTSIDE_EVIDENCE_WINDOW"],
    })
    _write(representative, {"status": "PASSED", "passed_count": 25})
    _write(coverage, {
        "level1_static_status": "PASSED",
        "question_status_counts": {"COVERED_NOT_RUN": 500},
    })
    return ProductFinalizationHarness(
        output_root=tmp_path / "output",
        qualification_receipt=qualification,
        dynamic_data_receipt=dynamic,
        representative_receipt=representative,
        static_coverage_receipt=coverage,
        source_roots=(source_root,),
        tests_passed=192,
        test_command="pytest trusted product spine: 192 passed",
    )


def test_finalization_is_content_addressed_and_recoverable(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    first = harness.run()
    replay = harness.run()
    assert first["status"] == "QUALIFIED_FOR_RESEARCH_PRODUCT_WITH_AUXILIARY_LIMITATION"
    assert first["artifact_hash"] == replay["artifact_hash"]
    assert replay["replayed_without_recomputation"] is True
    assert len((tmp_path / "output/events.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_finalization_blocks_direct_dynamic_execution(tmp_path, monkeypatch):
    result = _harness(tmp_path, monkeypatch, source="value = eval('1 + 1')\n").run()
    assert result["status"] == "BLOCKED"
    assert result["gates"]["security"]["dynamic_execution_findings"]
