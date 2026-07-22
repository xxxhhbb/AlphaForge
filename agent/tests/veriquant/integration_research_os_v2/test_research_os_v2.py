from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository, ResearchOSV2Service
from veriquant.research_validation_pipeline_v2 import FrozenValidationData


def frozen_data() -> FrozenValidationData:
    start = datetime(2025, 1, 2, 15, tzinfo=timezone.utc)
    fields = ("open", "high", "low", "close", "volume", "amount", "adj_factor", "market_cap", "status")
    symbols = tuple(f"S{index:03d}" for index in range(30))
    rows = []
    labels = {}
    universe = {}
    for day in range(260):
        when = start + timedelta(days=day)
        universe[when.isoformat()] = symbols
        for index, symbol in enumerate(symbols):
            close = 10.0 + 0.01 * day * (1 + index / 30.0) + index * 0.02
            values = {
                "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
                "close": close, "volume": 100_000.0 + day * 10 + index,
                "amount": (100_000.0 + day * 10 + index) * close,
                "adj_factor": 1.0, "market_cap": 1_000_000_000.0 + index * 10_000_000.0,
                "status": "LISTED",
            }
            rows.append(SnapshotRow(event_time=when, symbol=symbol, values=values))
            if day < 255:
                labels[f"{when.isoformat()}|{symbol}"] = (index - 14.5) * 0.0002 + day * 0.000001
    snapshot = MarketSnapshot(
        dataset_id="approved-proxy.test.v2",
        decision_time=start + timedelta(days=260),
        start_time=start,
        fields=fields,
        rows=tuple(rows),
        historical_universe=universe,
        coverage_by_field={field: 1.0 for field in fields},
    )
    return FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=canonical_content_hash({"qualified": True}),
        source_label="APPROVED_PROXY_DATA_VENDOR",
    )


def service(tmp_path, data=None) -> ResearchOSV2Service:
    selected = data if data is not None else frozen_data()
    return ResearchOSV2Service(
        root=tmp_path / "research-os-v2",
        data_loader=lambda: selected,
        dotenv_path=tmp_path / "missing.env",
    )


def test_frozen_repository_replay_and_tamper_fail_closed(tmp_path):
    repository = FrozenValidationDataRepository(tmp_path)
    expected = frozen_data()
    first_hash = repository.save(expected)
    loaded = repository.load()
    assert loaded is not None
    assert loaded.artifact_hash == expected.artifact_hash
    assert repository.save(expected) == first_hash
    payload = json.loads(repository.path.read_text(encoding="utf-8"))
    payload["source_label"] = "OFFICIAL_TUSHARE"
    repository.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        repository.load()


def test_question_to_confirmed_plan_is_hash_bound_and_uses_user_language(tmp_path):
    selected = service(tmp_path)
    suggestion = selected.create_suggestion({
        "question": "挖一下最近 50 天的大动量因子",
        "depth": "标准研究",
        "collaborationStyle": "标准式",
    })
    assert suggestion["canRun"] is True
    assert suggestion["answerLevel"] == "机器评价"
    assert 1 <= len(suggestion["selectedGroups"]) <= 6
    assert all("Desk" not in value and "Router" not in value for value in suggestion["selectedGroups"])
    plan = selected.confirm_plan({"suggestion": suggestion, "depth": "标准研究", "collaborationStyle": "标准式"})
    assert plan["state"] == "已确认"
    assert plan["horizon"] == "50 日观察窗 / 5 日预测期"
    assert plan["stopConditions"]
    stored_plan = selected._read(
        tmp_path / "research-os-v2" / "plans" / f"{plan['planId']}.json"
    )
    assert stored_plan["runtimeSchemaVersion"].endswith("/v2")
    tampered = {**suggestion, "question": "被替换的问题"}
    with pytest.raises(ValueError, match="changed"):
        selected.confirm_plan({"suggestion": tampered, "depth": "标准研究", "collaborationStyle": "标准式"})


def test_missing_data_answers_but_does_not_claim_machine_evaluation(tmp_path):
    selected = ResearchOSV2Service(root=tmp_path / "missing", data_loader=lambda: None, dotenv_path=tmp_path / "missing.env")
    suggestion = selected.create_suggestion({"question": "北向资金能否预测未来收益", "depth": "标准研究", "collaborationStyle": "标准式"})
    assert suggestion["canRun"] is False
    assert suggestion["answerLevel"] == "概念研究"
    assert suggestion["dataGaps"]


def test_background_run_finishes_with_terminal_candidates_and_is_idempotent(tmp_path):
    selected = service(tmp_path)
    suggestion = selected.create_suggestion({"question": "挖一下最近 50 天的大动量因子", "depth": "标准研究", "collaborationStyle": "标准式"})
    plan = selected.confirm_plan({"suggestion": suggestion, "depth": "标准研究", "collaborationStyle": "标准式"})
    running = selected.start_research(plan["planId"])
    assert running["state"] in {"运行中", "已完成"}
    deadline = time.monotonic() + 60
    current = running
    while current["state"] == "运行中" and time.monotonic() < deadline:
        time.sleep(0.05)
        current = selected.get_run(running["runId"])
    assert current["state"] == "已完成", current
    assert current["groups"]
    assert any(group["candidates"] for group in current["groups"])
    assert current["reportHash"].startswith("sha256:")
    assert current["evaluationPolicy"] == {
        "trainPeriods": 100,
        "validationPeriods": 60,
        "sealedPeriods": 30,
        "stepPeriods": 20,
    }
    assert current["candidateSelection"]["status"] in {
        "COMPLETED_WITH_WINNER",
        "COMPLETED_NO_WINNER",
    }
    if current["candidateSelection"]["bestObserved"] is not None:
        assert current["candidateSelection"]["bestObserved"]["authority"] == (
            "VISIBLE_RESEARCH_CLUE_ONLY"
        )
    replay = selected.start_research(plan["planId"])
    assert replay == current
    report = tmp_path / "research-os-v2" / "runs" / running["runId"] / "report.md"
    assert "真实股票数" not in report.read_text(encoding="utf-8")
    assert "机器结论" in report.read_text(encoding="utf-8")
    workbench_run = tmp_path / "workbench_artifacts" / "runs" / running["runId"]
    public_report = json.loads(
        (workbench_run / "report.json").read_text(encoding="utf-8")
    )
    assert public_report["markdown"] == report.read_text(encoding="utf-8")
    assert public_report["report_hash"] == current["reportHash"]
    lineage = json.loads(
        (workbench_run / "lineage.json").read_text(encoding="utf-8")
    )
    assert lineage["items"]
    assert all(
        item["factor_draft"]["expression"] for item in lineage["items"]
    )


def test_quick_exploration_never_starts_evidence(tmp_path):
    selected = service(tmp_path)
    suggestion = selected.create_suggestion({"question": "快速找一个 20 日因子草案", "depth": "快速探索", "collaborationStyle": "标准式"})
    assert suggestion["canRun"] is False
    plan = selected.confirm_plan({"suggestion": suggestion, "depth": "快速探索", "collaborationStyle": "标准式"})
    running = selected.start_research(plan["planId"])
    deadline = time.monotonic() + 10
    current = running
    while current["state"] == "运行中" and time.monotonic() < deadline:
        time.sleep(0.01)
        current = selected.get_run(running["runId"])
    assert current["state"] == "已阻断"
    assert "快速探索" in current["currentCheckpoint"]


class AlwaysFailProvider:
    def propose(self, **_kwargs):
        raise ValueError("invalid provider draft")


class BaselineEnabledFailProvider(AlwaysFailProvider):
    deterministic_baseline_enabled = True


def test_all_failed_real_groups_are_reported_as_revisions_not_system_failure(tmp_path):
    selected = service(tmp_path)
    selected._proposal_provider = lambda _selection, _data: (
        AlwaysFailProvider(),
        {
            "mode": "真实模型",
            "providerLabel": "测试模型",
            "modelLabel": "test-model",
        },
    )
    suggestion = selected.create_suggestion({
        "question": "挖一下最近50天的大动量因子",
        "depth": "标准研究",
        "collaborationStyle": "标准式",
    })
    plan = selected.confirm_plan({
        "suggestion": suggestion,
        "depth": "标准研究",
        "collaborationStyle": "标准式",
    })
    running = selected.start_research(plan["planId"])
    deadline = time.monotonic() + 10
    current = running
    while current["state"] == "运行中" and time.monotonic() < deadline:
        time.sleep(0.01)
        current = selected.get_run(running["runId"])

    assert current["state"] == "已完成"
    assert current["runStateCode"] == "COMPLETED_WITH_REVISIONS"
    assert current["researchOutcomeCode"] == "REVISE"
    assert len(current["groups"]) == 2
    assert {group["groupId"] for group in current["groups"]} == {
        "market_structure",
        "meta_alpha",
    }
    assert all(group["state"] == "需修订" for group in current["groups"])
    assert current["trialsUsed"] == 0
    assert current["proposalAttemptsUsed"] == 2
    assert current["sealedReview"] == "未申请"
    assert current["reportHash"].startswith("sha256:")

    run_root = tmp_path / "research-os-v2" / "runs" / running["runId"]
    diagnostics = json.loads((run_root / "group_runtime.json").read_text(encoding="utf-8"))
    assert len(diagnostics["branches"]) == 2
    assert all(item["status"] == "FAILED" for item in diagnostics["branches"])
    report = (run_root / "report.md").read_text(encoding="utf-8")
    assert "不是系统运行崩溃" in report
    assert "没有启动 PIT、机器 Evidence 或独立封闭评审" in report


def test_real_provider_failure_uses_an_explicit_deterministic_50_day_baseline(tmp_path):
    selected = service(tmp_path)
    selected._proposal_provider = lambda _selection, _data: (
        BaselineEnabledFailProvider(),
        {
            "mode": "真实模型",
            "providerLabel": "测试模型",
            "modelLabel": "test-model",
        },
    )
    suggestion = selected.create_suggestion({
        "question": "挖一下最近50天的大动量因子",
        "depth": "标准研究",
        "collaborationStyle": "标准式",
    })
    plan = selected.confirm_plan({
        "suggestion": suggestion,
        "depth": "标准研究",
        "collaborationStyle": "标准式",
    })
    running = selected.start_research(plan["planId"])
    deadline = time.monotonic() + 60
    current = running
    while current["state"] == "运行中" and time.monotonic() < deadline:
        time.sleep(0.05)
        current = selected.get_run(running["runId"])

    assert current["runStateCode"] == "COMPLETED"
    baseline = next(
        group for group in current["groups"]
        if group["groupId"] == "research_baseline"
    )
    assert baseline["candidates"]
    assert baseline["candidates"][0]["expression"] == (
        "rank_cross_section(pct_change(close, 50))"
    )
    failed = [
        group for group in current["groups"]
        if group["groupId"] != "research_baseline"
    ]
    assert failed
    assert all(group["state"] == "失败" for group in failed)
