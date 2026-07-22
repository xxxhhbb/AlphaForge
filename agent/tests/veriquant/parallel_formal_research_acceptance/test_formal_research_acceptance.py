from __future__ import annotations

from pathlib import Path

from veriquant.formal_research_acceptance import FormalResearchAcceptanceEvaluator, GateStatus


def test_current_formal_research_acceptance_is_honest() -> None:
    verdict = FormalResearchAcceptanceEvaluator().evaluate(repo_root=Path(__file__).resolve().parents[4])
    assert verdict.gates["contracts"] is GateStatus.IMPLEMENTED_AND_TESTED
    assert verdict.gates["formal_backend_command"] is GateStatus.IMPLEMENTED_AND_TESTED
    assert verdict.gates["dynamic_real_llm_candidates"] is GateStatus.IMPLEMENTED_NOT_RUN
    assert not verdict.all_required_passed or "dynamic_real_llm_candidates" in verdict.gates
    assert verdict.blockers
