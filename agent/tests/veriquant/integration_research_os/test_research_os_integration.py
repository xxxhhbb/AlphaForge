from __future__ import annotations

from datetime import timedelta

from veriquant.data import DatasetRegistry, LABEL_FIELD, build_synthetic_market_fixture
from veriquant.evidence.ledger import TrialLedger
from veriquant.research_closure_contracts import ResearchOutcomeStatus
from veriquant.research_os_integration import AlphaForgeResearchOS


def _inputs():
    fixture = build_synthetic_market_fixture(seed=7, session_count=120)
    registry = DatasetRegistry()
    fixture.register(registry)
    decision_time = fixture.calendar[-1] + timedelta(hours=7)
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=decision_time,
        fields=("close", "volume"),
    )
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": row.value
        for row in registry.labels(
            fixture.dataset_id,
            label_field=LABEL_FIELD,
            evaluation_time=decision_time,
        )
        if row.value is not None
    }
    return snapshot, labels


def test_question_to_multi_group_machine_evidence_and_report(tmp_path) -> None:
    snapshot, labels = _inputs()
    result = AlphaForgeResearchOS(artifact_root=tmp_path).run(
        run_id="research-50d",
        question="挖一下最近50天大动量的因子",
        snapshot=snapshot,
        labels=labels,
    )

    assert result.selected_group_ids == ("market_structure", "meta_alpha")
    assert result.candidate_count == result.terminal_candidate_count == 4
    assert all("50" in item.expression for item in result.report.candidates)
    assert all(item.official_metrics for item in result.report.candidates)
    assert all(item.outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH for item in result.report.candidates)
    assert result.best_observed_candidate_id is not None
    assert result.tournament_status == "COMPLETED_WITH_WINNER"
    assert (tmp_path / "published" / "research-50d" / "machine_report.json").exists()
    assert (tmp_path / "published" / "research-50d" / "report.md").exists()


def test_replay_is_idempotent_and_does_not_double_consume_trial_budget(tmp_path) -> None:
    snapshot, labels = _inputs()
    service = AlphaForgeResearchOS(artifact_root=tmp_path)
    kwargs = dict(
        run_id="research-replay",
        question="挖一下最近50天大动量的因子",
        snapshot=snapshot,
        labels=labels,
    )
    first = service.run(**kwargs)
    before = TrialLedger(tmp_path / "trial_ledger.sqlite3").replay()
    second = service.run(**kwargs)
    after = TrialLedger(tmp_path / "trial_ledger.sqlite3").replay()

    assert first.report.artifact_hash == second.report.artifact_hash
    assert first.publication.artifact_hash == second.publication.artifact_hash
    assert sum(item.consumed for item in before.families.values()) == 4
    assert sum(item.consumed for item in after.families.values()) == 4
    assert all(item.result_ref.budget_consumed for item in second.validation_results)


def test_report_public_surface_uses_research_story_language(tmp_path) -> None:
    snapshot, labels = _inputs()
    result = AlphaForgeResearchOS(artifact_root=tmp_path).run(
        run_id="research-language",
        question="挖一下最近50天大动量的因子",
        snapshot=snapshot,
        labels=labels,
    )
    markdown = (tmp_path / "published" / result.run_id / "report.md").read_text(encoding="utf-8")
    assert "专业研究" in markdown
    assert "Desk" not in markdown
    assert "Router" not in markdown
    assert "Swarm" not in markdown
    assert "OFFICIAL_TUSHARE" not in markdown
    assert "50" in markdown
