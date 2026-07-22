from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.research_explorer_tools import (
    ExplorerFailureCode,
    InMemoryVisibleEvidenceReader,
    ResearchExplorerError,
    VisibleEvidenceToolbox,
)


NOW = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def reader(**candidate):
    base = {
        "candidate_id": "candidate_a",
        "split": "validation",
        "source_component": "EVIDENCE_SERVICE",
        "evidence_class": "OFFICIAL_EVIDENCE",
        "metrics": {
            "gross_return": 0.02,
            "net_return": -0.01,
            "net_icir": 0.3,
            "stability": 0.4,
            "incrementality": 0.1,
            "cost_bps": 30.0,
        },
        "reason_codes": ("NET_RETURN_NEGATIVE",),
    }
    base.update(candidate)
    return InMemoryVisibleEvidenceReader(candidates={"candidate_a": base}, budgets={"family_a": {"budget_limit": 4, "budget_consumed": 2, "budget_remaining": 2}})


def test_nested_sealed_fields_are_rejected():
    toolbox = VisibleEvidenceToolbox(reader(nested={"sealed_metric": 0.2}))
    with pytest.raises(ResearchExplorerError) as exc:
        toolbox.query_visible_evidence("candidate_a")
    assert exc.value.code is ExplorerFailureCode.SEALED_LEAKAGE


def test_qlib_auxiliary_cannot_masquerade_as_official():
    toolbox = VisibleEvidenceToolbox(reader(source_component="QLIB", evidence_class="OFFICIAL_EVIDENCE"))
    with pytest.raises(ResearchExplorerError) as exc:
        toolbox.query_visible_evidence("candidate_a")
    assert exc.value.code is ExplorerFailureCode.AUXILIARY_MASQUERADE


def test_gross_good_net_negative_rejection_explains_cost_failure():
    result = VisibleEvidenceToolbox(reader()).explain_rejection("candidate_a")
    assert "COST_FAILURE" in result.payload["reason_codes"]


def test_same_input_produces_same_design_hash():
    toolbox = VisibleEvidenceToolbox(reader())
    first = toolbox.design_walk_forward(
        plan_id="plan_a",
        hypothesis_id="hyp_a",
        horizon="5d",
        universe="cn_ashare",
        decision_time=NOW,
    )
    second = toolbox.design_walk_forward(
        plan_id="plan_a",
        hypothesis_id="hyp_a",
        horizon="5d",
        universe="cn_ashare",
        decision_time=NOW,
    )
    assert first.artifact_hash == second.artifact_hash


def test_promotion_payload_is_rejected_from_preregistration():
    toolbox = VisibleEvidenceToolbox(reader())
    with pytest.raises(ResearchExplorerError) as exc:
        toolbox.preregister_hypothesis(
            {
                "hypothesis_id": "hyp_a",
                "statement": "test",
                "falsification_tests": ["fail when net IC is non-positive"],
                "promotion_decision": "PROMOTE_CHALLENGER",
            }
        )
    assert exc.value.code is ExplorerFailureCode.AUTHORITY_REJECTED


def test_trial_ledger_has_no_write_interface():
    toolbox = VisibleEvidenceToolbox(reader())
    assert not hasattr(toolbox, "append_trial")
    assert not hasattr(toolbox, "reserve_budget")
    result = toolbox.inspect_trial_budget("family_a")
    assert result.payload["budget_consumed"] == 2


def test_quick_exploration_does_not_request_official_evidence():
    toolbox = VisibleEvidenceToolbox(reader())
    plan = toolbox.design_walk_forward(
        plan_id="plan_quick",
        hypothesis_id="hyp_a",
        horizon="5d",
        universe="cn_ashare",
        decision_time=NOW,
        research_depth="QUICK_EXPLORATION",
    )
    assert plan.official_evidence_requested is False
    assert plan.planned_tests == ("draft_only_research_brief",)


def test_rankic_only_explanation_is_insufficient():
    toolbox = VisibleEvidenceToolbox(reader(metrics={"rank_ic": 0.1}, reason_codes=()))
    result = toolbox.explain_rejection("candidate_a")
    assert "RANKIC_ONLY_INSUFFICIENT" in result.payload["reason_codes"]
