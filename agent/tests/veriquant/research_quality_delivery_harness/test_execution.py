from __future__ import annotations

from veriquant.research_quality_delivery_harness.execution import _field_closure, _mechanical_safe_probe


def test_field_closure_accepts_frozen_fields_and_rejects_unknown(tmp_path):
    good = tmp_path / "good.py"
    good.write_text(
        "def run_research(train_rows, prediction_rows, seed):\n"
        "    return {'rows': [{'score': item['close']} for item in prediction_rows]}\n",
        encoding="utf-8",
    )
    assert _field_closure(good, {"close"})["status"] == "PASSED"
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def run_research(train_rows, prediction_rows, seed):\n"
        "    return {'rows': [{'score': item['invented_signal']} for item in prediction_rows]}\n",
        encoding="utf-8",
    )
    result = _field_closure(bad, {"close"})
    assert result["status"] == "FAILED"
    assert result["unknown_fields"] == ["invented_signal"]


def test_all_six_safe_probes_fail_closed_with_typed_codes():
    expected = {
        "market_structure": "FIELD_NOT_IN_FROZEN_SCHEMA",
        "fundamental": "PIT_AVAILABILITY_VIOLATION",
        "expectations_events": "EVENT_CLOCK_VIOLATION",
        "alternative_data": "UNAUTHORIZED_PATH_OR_NETWORK",
        "macro_regime": "REGIME_SAMPLE_INSUFFICIENT",
        "meta_alpha": "SELF_APPROVAL_FORBIDDEN",
    }
    assert {key: _mechanical_safe_probe(key) for key in expected} == expected
