from __future__ import annotations

import json
import math

from veriquant.research_quality_delivery_harness.contracts import (
    CaseState, EvidenceAuthority, ExecutionStatus, ReportStatus,
)
from veriquant.research_quality_delivery_harness.storage import atomic_json, canonical_hash, load_hashed


def test_atomic_json_normalizes_nonfinite_to_null_and_replays(tmp_path):
    saved = atomic_json(tmp_path / "x.json", {"value": math.nan, "nested": {"inf": math.inf}})
    assert saved["value"] is None
    assert saved["nested"]["inf"] is None
    assert load_hashed(tmp_path / "x.json") == saved
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8"))["value"] is None


def test_case_state_tracks_independent_status_axes():
    state = CaseState(
        case_id="case", trial_id="trial", idempotency_key=canonical_hash({"trial": 1}),
        group_id="fundamental", role="NATIVE_OFFICIAL", output_kind="ScoreTable",
        execution_status=ExecutionStatus.PASSED,
        evidence_authority=EvidenceAuthority.OFFICIAL_EVIDENCE,
        report_status=ReportStatus.QUALIFIED,
    ).to_dict()
    assert state["execution_status"] == "PASSED"
    assert state["evidence_authority"] == "OFFICIAL_EVIDENCE"
    assert state["report_status"] == "QUALIFIED"


def test_auxiliary_case_cannot_qualify_report():
    import pytest
    with pytest.raises(ValueError, match="auxiliary"):
        CaseState(
            case_id="case", trial_id="trial", idempotency_key=canonical_hash({"trial": 1}),
            group_id="alternative_data", role="NATIVE_OFFICIAL", output_kind="RegressionTable",
            evidence_authority=EvidenceAuthority.AUXILIARY_ONLY,
            report_status=ReportStatus.QUALIFIED,
        )
