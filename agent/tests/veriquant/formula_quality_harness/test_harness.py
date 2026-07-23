from __future__ import annotations

import json

from veriquant.formula_quality_harness import FormulaQualityHarness


def test_formula_quality_harness_is_atomic_replayable_and_qualified(tmp_path) -> None:
    harness = FormulaQualityHarness(output_root=tmp_path)
    first = harness.run()
    second = harness.run()
    assert first == second
    assert first["status"] == "QUALIFIED"
    assert first["passed_count"] == 5
    persisted = json.loads((tmp_path / "final_verdict.json").read_text(encoding="utf-8"))
    assert persisted == first
    assert len(list((tmp_path / "cases").glob("*.json"))) == 5
