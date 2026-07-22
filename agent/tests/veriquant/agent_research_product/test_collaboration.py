from __future__ import annotations

from veriquant.agent_research_product.collaboration import EvidenceCollaborationAggregator
from veriquant.agent_research_product.contracts import ProductExperimentResult, ProductExperimentStatus, ProductRunResult


def _experiment(group: str, outcome: str, value: str, digit: str) -> ProductExperimentResult:
    return ProductExperimentResult(
        experiment_id=f"exp_{group}", specialist_id=group,
        source_hash="sha256:" + digit * 64,
        status=ProductExperimentStatus.OFFICIAL_EVIDENCE,
        official_evidence_hash="sha256:" + ("a" if digit != "a" else "b") * 64,
        official_metrics=(("net_return", value),), court_outcome=outcome,
    )


def test_collaboration_uses_only_official_evidence_and_surfaces_conflict() -> None:
    result = ProductRunResult(
        run_id="run_1", manifest_hash="sha256:" + "1" * 64, lifecycle="COMPLETED",
        swarm_hash="sha256:" + "2" * 64,
        experiments=(
            _experiment("fundamental", "VALIDATED_RESEARCH", "0.1", "3"),
            _experiment("market_structure", "REJECT", "-0.2", "4"),
        ), failures=(), report_hash="sha256:" + "5" * 64, report_path="report.md",
    )
    collaboration = EvidenceCollaborationAggregator().aggregate(result)
    assert collaboration.aggregate_outcome == "REJECT"
    assert "SPECIALIST_VERDICT_CONFLICT" in collaboration.conflict_codes
    assert "METRIC_DIRECTION_CONFLICT:net_return" in collaboration.conflict_codes
    assert collaboration.artifact_hash == EvidenceCollaborationAggregator().aggregate(result).artifact_hash


def test_collaboration_rejects_single_specialist_or_auxiliary_only() -> None:
    result = ProductRunResult(
        run_id="run_1", manifest_hash="sha256:" + "1" * 64, lifecycle="COMPLETED",
        swarm_hash=None, experiments=(_experiment("fundamental", "REJECT", "0", "3"),),
        failures=(), report_hash="sha256:" + "5" * 64, report_path="report.md",
    )
    try:
        EvidenceCollaborationAggregator().aggregate(result)
    except ValueError as exc:
        assert "distinct specialists" in str(exc)
    else:
        raise AssertionError("single specialist must fail closed")
