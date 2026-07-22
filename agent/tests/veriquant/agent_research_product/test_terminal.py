from __future__ import annotations

from pathlib import Path
import hashlib
import json

from veriquant.agent_research_product import terminal


QUESTION = (
    "在大 A 市场中，寻找能够预测未来20个交易日收益、与现有50日动量低相关，"
    "并且在高低 Shibor 环境中表现稳定的 Alpha。请考虑估值、盈利质量、量价与流动性信息。"
)


def test_terminal_preserves_lead_but_stably_sorts_multi_specialist_identity(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "frozen_validation_data.json").write_text("{}", encoding="utf-8")
    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n", encoding="utf-8")
    captured = {}

    class Result:
        lifecycle = "COMPLETED"

        def __init__(self, path: Path):
            self.report_path = str(path)

        def to_dict(self):
            return {"lifecycle": "COMPLETED", "experiments": [], "report_path": self.report_path}

    class FakeCanary:
        def __init__(self, configuration):
            captured["configuration"] = configuration

        def run(self):
            return Result(report_path)

    monkeypatch.setattr(
        terminal,
        "_runtime_identity",
        lambda _path: ("veriquant-test:local", "sha256:" + "a" * 64),
    )
    monkeypatch.setattr(
        "veriquant.agent_research_product.canary.AgentResearchProductCanary",
        FakeCanary,
    )
    args = terminal._parser().parse_args([
        "--question", QUESTION,
        "--data-root", str(data_root),
        "--root", str(tmp_path / "runs"),
    ])

    code, payload, _report = terminal.run(args)

    configuration = captured["configuration"]
    assert code == 0
    assert configuration.specialist_id == "meta_alpha"
    assert configuration.specialist_ids == tuple(sorted(configuration.specialist_ids))
    assert configuration.specialist_ids == (
        "fundamental", "macro_regime", "market_structure", "meta_alpha",
    )
    assert dict(configuration.specialist_output_kinds) == {
        "fundamental": "RegressionTable",
        "macro_regime": "RegimeComparisonTable",
        "market_structure": "RegressionTable",
        "meta_alpha": "RegressionTable",
    }
    assert payload["selected_specialists"] == list(configuration.specialist_ids)


def test_explicit_specialist_subset_filters_planner_output_contracts(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "frozen_validation_data.json").write_text("{}", encoding="utf-8")
    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n", encoding="utf-8")
    captured = {}

    class Result:
        lifecycle = "COMPLETED"
        def __init__(self):
            self.report_path = str(report_path)
        def to_dict(self):
            return {"lifecycle": "COMPLETED", "experiments": [], "report_path": self.report_path}

    class FakeCanary:
        def __init__(self, configuration):
            captured["configuration"] = configuration
        def run(self):
            return Result()

    monkeypatch.setattr(terminal, "_runtime_identity", lambda _path: ("image", "sha256:" + "a" * 64))
    monkeypatch.setattr("veriquant.agent_research_product.canary.AgentResearchProductCanary", FakeCanary)
    args = terminal._parser().parse_args([
        "--question", QUESTION, "--data-root", str(data_root), "--root", str(tmp_path / "runs"),
        "--specialist", "fundamental", "--specialist", "macro_regime", "--specialist", "market_structure",
    ])
    terminal.run(args)
    config = captured["configuration"]
    assert {name for name, _kind in config.specialist_output_kinds} == set(config.specialist_ids)
    assert "meta_alpha" not in dict(config.specialist_output_kinds)


def test_reuse_existing_recovers_every_specialist_content_addressed_source(tmp_path):
    identity_root = tmp_path / "identity"
    experiments = []
    for specialist_id, source in (
        ("fundamental", b"print('fundamental')\n"),
        ("market_structure", b"print('market')\n"),
        ("meta_alpha", b"print('meta')\n"),
    ):
        digest = hashlib.sha256(source).hexdigest()
        path = identity_root / "draft_sources" / digest[:2] / digest[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        experiments.append({"specialist_id": specialist_id, "source_hash": "sha256:" + digest})
    result_path = identity_root / "research_runs" / "prior" / "product_result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps({"experiments": experiments}), encoding="utf-8")

    prior, recovered = terminal._recoverable_sources(
        identity_root, ("fundamental", "market_structure", "meta_alpha")
    )

    assert prior == result_path
    assert tuple(item[0] for item in recovered) == ("fundamental", "market_structure", "meta_alpha")
    assert all(item[1].is_file() for item in recovered)


def test_recovery_prefers_latest_best_committed_source_recursively(tmp_path):
    identity_root = tmp_path / "identity"
    blocked_source = b"blocked draft"
    accepted_source = b"accepted auxiliary draft"
    records = []
    for source in (blocked_source, accepted_source):
        digest = hashlib.sha256(source).hexdigest()
        path = identity_root / "recovery" / "r2" / "draft_sources" / digest[:2] / digest[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        records.append((path, "sha256:" + digest))
    old = identity_root / "research_runs" / "old" / "product_result.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"experiments": [{
        "specialist_id": "fundamental", "source_hash": records[0][1], "status": "BLOCKED",
    }]}), encoding="utf-8")
    latest = identity_root / "recovery" / "r2" / "research_runs" / "new" / "product_result.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"experiments": [
        {"specialist_id": "fundamental", "source_hash": records[0][1], "status": "BLOCKED"},
        {"specialist_id": "fundamental", "source_hash": records[1][1], "status": "AUXILIARY_ONLY"},
    ]}), encoding="utf-8")
    old.touch()
    latest.touch()

    prior, recovered = terminal._recoverable_sources(identity_root, ("fundamental",))

    assert prior == latest
    assert recovered[0][1] == records[1][0]
    assert recovered[0][2] == records[1][1]


def test_progress_output_is_driven_by_real_receipts_and_not_repeated(tmp_path, capsys):
    outcome = tmp_path / "run" / "branches" / "task_fundamental" / "outcome.json"
    review = tmp_path / "research_runs" / "r" / "experiments" / "exp" / "review.json"
    runner = review.with_name("runner.json")
    evidence = review.with_name("evidence_result.json")
    product = tmp_path / "research_runs" / "r" / "product_result.json"
    usage = tmp_path / "agent_runs" / "r" / "specialists" / "fundamental" / "llm_usage.json"
    for path, value in (
        (outcome, {"payload": {"specialist_id": "fundamental"}}),
        (review, {"status": "ACCEPTED"}),
        (runner, {"status": "COMPLETED_AUXILIARY", "failure_code": None}),
        (evidence, {"status": "OFFICIAL_EVIDENCE"}),
        (product, {"lifecycle": "COMPLETED"}),
        (usage, {"totals": {"calls": 2}}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    seen = set()

    terminal._emit_progress(tmp_path, seen)
    terminal._emit_progress(tmp_path, seen)
    output = capsys.readouterr().err

    assert output.count("基本面研究组已提交研究代码") == 1
    assert output.count("真实模型已完成 2 次有界迭代") == 1
    assert output.count("机械审查：ACCEPTED") == 1
    assert output.count("隔离实验：COMPLETED_AUXILIARY") == 1
    assert output.count("已生成机器 Evidence") == 1
    assert output.count("正式报告已生成：COMPLETED") == 1
