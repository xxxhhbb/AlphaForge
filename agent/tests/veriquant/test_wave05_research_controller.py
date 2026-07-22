from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.veriquant.contracts import Component, ResearchQuestion, ResearchState
from src.veriquant.evidence import TrialLedger
from src.veriquant.research import (
    BRANCH_ARTIFACTS,
    FIVE_STAGE_SEQUENCE,
    DeterministicOfficialEvidenceAuthority,
    DeterministicProposalProvider,
    IncompleteProviderResult,
    ResearchDAGController,
    TaskStatus,
)


def question(authority: DeterministicOfficialEvidenceAuthority) -> ResearchQuestion:
    return ResearchQuestion(
        question_id="wave05-six-desk-question",
        text="Find reproducible visible factors using only the offline fixture.",
        universe=("AAA", "BBB", "CCC", "DDD"),
        horizon="5d",
        decision_time=authority.dataset.decision_time,
        availability_assumptions=("Every feature must be available by decision time.",),
        state=ResearchState.DRAFT,
    )


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_six_desk_run_is_offline_isolated_and_uses_one_evidence_authority(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENABLE_LLM_RESEARCH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    authority = DeterministicOfficialEvidenceAuthority()
    controller = ResearchDAGController(authority=authority, artifact_root=tmp_path)
    result = controller.run(run_id="six-desk-run", question=question(authority))

    records = result.dag_result.records
    assert records["question"].status is TaskStatus.COMPLETED
    assert records["validated_aggregation"].status is TaskStatus.COMPLETED
    assert records["selection"].status is TaskStatus.COMPLETED
    assert len(records["selection"].output.ranked_branch_ids) >= 1

    branch_ids = {desk.desk_id.value for desk in controller.desks}
    assert set(result.branch_directories) == branch_ids
    assert len({path.resolve() for path in result.branch_directories.values()}) == 6

    for branch_id, directory in result.branch_directories.items():
        assert {path.name for path in directory.iterdir()} == set(BRANCH_ARTIFACTS)
        factor = read_json(directory / "factor_spec.json")
        manifest = read_json(directory / "experiment_manifest.json")
        evidence = read_json(directory / "evidence_pack.json")
        critics = read_json(directory / "critic_findings.json")
        assert factor["artifact_status"] == "PRODUCED"
        assert factor["branch_id"] == branch_id
        assert factor["content"]["contract_type"] == "FactorSpecDraft"
        assert factor["content"]["state"] == "DRAFT"
        assert manifest["artifact_status"] == "PRODUCED"
        assert manifest["content"]["branch_id"] == branch_id
        assert manifest["content"]["evidence_authority_id"] == authority.authority_id
        assert evidence["artifact_status"] == "PRODUCED"
        assert evidence["content"]["produced_by"] == Component.EVIDENCE_SERVICE.value
        assert critics["artifact_status"] == "PRODUCED"
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        completed_stages = tuple(
            item["stage"]
            for item in events
            if item["status"] == "COMPLETED" and item["stage"] in {stage.value for stage in FIVE_STAGE_SEQUENCE}
        )
        assert completed_stages == tuple(stage.value for stage in FIVE_STAGE_SEQUENCE)
        assert "sealed" not in json.dumps(events).lower()

    branch_records = [records[f"branch.{branch_id}"] for branch_id in sorted(branch_ids)]
    assert all(record.status is TaskStatus.COMPLETED for record in branch_records)
    assert all(record.output.proposal.factor_draft.state is ResearchState.DRAFT for record in branch_records)
    assert all(records[f"court.{branch_id}"].status is TaskStatus.COMPLETED for branch_id in branch_ids)

    official_calls = [call for call in authority.calls if call.operation == "OFFICIAL_EVALUATE"]
    assert len(official_calls) == 6
    assert {call.authority_object_id for call in official_calls} == {id(authority)}
    assert {call.authority_id for call in official_calls} == {authority.authority_id}
    assert {call.branch_id for call in official_calls} == branch_ids

    replay = TrialLedger(result.run_dir / "trial_ledger.sqlite3").replay()
    assert len(replay.families) == 6
    assert all(family.consumed == 1 for family in replay.families.values())


class _FailingProvider(DeterministicProposalProvider):
    def propose_hypothesis(self, **kwargs):
        raise RuntimeError("deliberate provider failure")


class _IncompleteProvider(DeterministicProposalProvider):
    def propose_hypothesis(self, **kwargs):
        raise IncompleteProviderResult("provider returned no substantive hypothesis")


def test_failed_and_incomplete_branches_do_not_stop_independent_branches_or_forge_results(
    tmp_path,
) -> None:
    authority = DeterministicOfficialEvidenceAuthority()

    def provider_factory(desk):
        if desk.desk_id.value == "fundamental":
            return _FailingProvider()
        if desk.desk_id.value == "expectations_events":
            return _IncompleteProvider()
        return DeterministicProposalProvider()

    controller = ResearchDAGController(
        authority=authority,
        provider_factory=provider_factory,
        artifact_root=tmp_path,
    )
    result = controller.run(run_id="partial-branch-run", question=question(authority))
    records = result.dag_result.records

    assert records["branch.fundamental"].status is TaskStatus.FAILED
    assert records["branch.fundamental"].output is None
    assert records["branch.expectations_events"].status is TaskStatus.INCOMPLETE
    assert records["branch.expectations_events"].output is None
    completed = {
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }
    assert all(records[f"branch.{branch}"].status is TaskStatus.COMPLETED for branch in completed)
    assert records["court.fundamental"].status is TaskStatus.BLOCKED
    assert records["court.expectations_events"].status is TaskStatus.BLOCKED
    assert all(records[f"court.{branch}"].status is TaskStatus.COMPLETED for branch in completed)
    assert records["validated_aggregation"].status is TaskStatus.BLOCKED
    assert records["selection"].status is TaskStatus.BLOCKED

    for branch_id in ("fundamental", "expectations_events"):
        directory = result.branch_directories[branch_id]
        evidence = read_json(directory / "evidence_pack.json")
        critics = read_json(directory / "critic_findings.json")
        assert evidence["artifact_status"] == "NOT_PRODUCED"
        assert evidence["content"] is None
        assert evidence["failure"] is not None
        assert critics["artifact_status"] == "NOT_PRODUCED"
        assert "official_metrics" not in json.dumps(evidence)

    for branch_id in completed:
        evidence = read_json(result.branch_directories[branch_id] / "evidence_pack.json")
        assert evidence["artifact_status"] == "PRODUCED"
        assert evidence["content"]["produced_by"] == Component.EVIDENCE_SERVICE.value

    official_calls = [call for call in authority.calls if call.operation == "OFFICIAL_EVALUATE"]
    assert {call.branch_id for call in official_calls} == completed
