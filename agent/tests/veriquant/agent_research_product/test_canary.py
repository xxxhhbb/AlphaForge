from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from veriquant.agent_research_product.canary import (
    AgentResearchProductCanary,
    ProductCanaryConfiguration,
    _load_amendment,
    _prepare_data,
)
from veriquant.agent_research_sandbox import (
    ExperimentReviewPolicy,
    RegressionRow,
    RegressionTable,
    RegimeComparisonRow,
    RegimeComparisonTable,
    ScoreRow,
    ScoreTable,
)
from veriquant.agent_research_sandbox.process_runner import (
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessReceipt,
    canonical_hash,
)
from veriquant.data import MarketSnapshot, SnapshotRow
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.research_agent_runtime import (
    InvestigationReceipt,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    ResearchDraftArtifact,
    ResearchDraftKind,
)
from veriquant.research_validation_pipeline_v2.models import FrozenRowProvenance, FrozenValidationData
from veriquant.research_data_gateway.dynamic_tool_freeze import AUTHORITY_FILE


def test_amendment_receipt_resolution_is_independent_of_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    amendment = _load_amendment(ExperimentReviewPolicy())
    assert amendment.amendment_id == "A-02"


def test_product_runner_budget_accepts_full_vintage_but_remains_bounded(tmp_path):
    frozen_repository(tmp_path / "data")
    config = ProductCanaryConfiguration(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        question="研究完整冻结样本",
        docker_image="alphaforge/research-sandbox:local",
        docker_image_id=IMAGE_ID,
    )
    assert config.runner_max_input_bytes == 256 * 1024 * 1024
    assert config.runner_max_output_bytes == 64 * 1024 * 1024
    with pytest.raises(ValueError, match="runner_max_input_bytes"):
        replace(config, runner_max_input_bytes=257 * 1024 * 1024)


UTC = timezone.utc
IMAGE_ID = "sha256:" + "8" * 64
SOURCE = '''def run_research(training_rows, prediction_rows, seed):
    rows = []
    for item in prediction_rows:
        rows.append({
            "contract_type": "ScoreRow",
            "schema_version": "alphaforge.agent-research-sandbox/v1",
            "symbol": item["symbol"],
            "decision_time": item["decision_time"],
            "source_available_at": item["source_available_at"],
            "score": item["close"],
            "label_horizon_sessions": 2,
        })
    return {"rows": rows}
'''


def h(char: str) -> str:
    return "sha256:" + char * 64


def frozen_repository(root: Path, *, source_label: str = "APPROVED_PROXY_DATA_VENDOR") -> FrozenValidationData:
    start = datetime(2026, 1, 5, 15, tzinfo=UTC)
    sessions = tuple(start + timedelta(days=day) for day in range(15))
    fields = ("close", "volume")
    rows = tuple(
        SnapshotRow(
            event_time=session,
            symbol=symbol,
            values={"close": 10.0 + day + index * 0.1, "volume": 1000.0 + day + index},
        )
        for day, session in enumerate(sessions)
        for index, symbol in enumerate(("000001.SZ", "000002.SZ", "000003.SZ"))
    )
    snapshot = MarketSnapshot(
        dataset_id="approved_proxy_canary",
        decision_time=sessions[-1] + timedelta(hours=2),
        start_time=sessions[0],
        fields=fields,
        rows=rows,
        historical_universe={
            session.isoformat(): ("000001.SZ", "000002.SZ", "000003.SZ") for session in sessions
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    labels = {
        f"{session.isoformat()}|{symbol}": 0.001 * (day + index + 1)
        for day, session in enumerate(sessions[:-2])
        for index, symbol in enumerate(("000001.SZ", "000002.SZ", "000003.SZ"))
    }
    provenance = tuple(
        FrozenRowProvenance(
            symbol=row.symbol,
            event_time=row.event_time,
            published_at=row.event_time + timedelta(minutes=30),
            available_at=row.event_time + timedelta(hours=1),
            revision_id=f"rev-{row.event_time.date()}-{row.symbol}",
            source_kind="daily",
            source_label=source_label,
            provider_class="APPROVED_PROXY_DATA_VENDOR",
            source_class="TUSHARE_COMPATIBLE_PROXY",
            input_origin="FROZEN_REAL",
            captured_at=snapshot.decision_time,
            source_record_hash=canonical_hash({"event": row.event_time.isoformat(), "symbol": row.symbol}),
            adapter_manifest_hash=h("a"),
            calendar_hash=h("b"),
            universe_hash=h("c"),
            availability_policy_hash=h("d"),
            publication_time_policy="DISTINCT_PROVIDER_MARKET_CLOCKS",
        )
        for row in rows
    )
    value = FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=h("e"),
        source_label=source_label,
        row_provenance=provenance,
    )
    FrozenValidationDataRepository(root).save(value)
    return value


class FakeRuntime:
    def __init__(self, task, store) -> None:
        self.task = task
        self.handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
        self.calls = 0

    def run(self, *, task, toolbox, upstream_context, cancellation):
        self.calls += 1
        return ResearchAgentOutcome(
            task_id=task.task_id,
            task_hash=task.artifact_hash,
            specialist_id=task.specialist_id,
            status=ResearchAgentStatus.COMPLETED,
            plan_hash=canonical_hash({"plan": "bounded unit composition"}),
            investigation_receipts=toolbox.recorded_receipts,
            draft_artifacts=(
                ResearchDraftArtifact(
                    kind=ResearchDraftKind.CODE,
                    content_hash=self.handle.handle_id,
                    media_type="text/x-python-draft",
                ),
            ),
            provider_calls=1,
            tool_calls=len(toolbox.recorded_receipts),
        )


class DynamicInvestigationRuntime(FakeRuntime):
    """Uses an unfrozen Vibe investigation result to shape a draft.

    The generated score still runs against the frozen panel, but the dynamic
    tool result is not part of that panel's immutable data identity.  The
    product must therefore keep the computed metrics research-only.
    """

    def run(self, *, task, toolbox, upstream_context, cancellation):
        outcome = super().run(
            task=task,
            toolbox=toolbox,
            upstream_context=upstream_context,
            cancellation=cancellation,
        )
        receipt = InvestigationReceipt(
            tool_name="get_stock_news",
            arguments_hash=h("7"),
            result_hash=h("6"),
            available_at=task.decision_time,
        )
        receipts = tuple(sorted(
            (*outcome.investigation_receipts, receipt),
            key=lambda item: (item.tool_name, item.arguments_hash),
        ))
        return replace(
            outcome,
            investigation_receipts=receipts,
            tool_calls=len(receipts),
        )


class ContractRunner:
    def __init__(self, *, mismatched_clock: bool = False) -> None:
        self.mismatched_clock = mismatched_clock
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        base = request.process_request
        if base.output_kind == "RegimeComparisonTable":
            table = RegimeComparisonTable(
                experiment_id=base.experiment_id,
                code_identity_hash=base.code_identity_hash,
                data_identity_hash=base.data_identity_hash,
                rows=(
                    RegimeComparisonRow(
                        regime_id="high_shibor",
                        metric_name="signal_effect",
                        estimate=0.03,
                        sample_count=30,
                    ),
                    RegimeComparisonRow(
                        regime_id="low_shibor",
                        metric_name="signal_effect",
                        estimate=0.02,
                        sample_count=30,
                    ),
                ),
            )
            output = table.to_dict()
            return ResearchProcessReceipt(
                run_id=base.run_id,
                experiment_id=base.experiment_id,
                status=ProcessRunStatus.COMPLETED_AUXILIARY,
                isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
                input_hash=request.input_hash,
                code_hash=base.code_hash,
                output_hash=canonical_hash(
                    {"input_hash": request.input_hash, "output_kind": base.output_kind, "output": output}
                ),
                output_kind=base.output_kind,
                output=output,
                failure_code=None,
                diagnostic=None,
                amendment_hash=base.amendment_hash,
                official_evidence_eligible=False,
            )
        if base.output_kind == "RegressionTable":
            table = RegressionTable(
                experiment_id=base.experiment_id,
                code_identity_hash=base.code_identity_hash,
                data_identity_hash=base.data_identity_hash,
                specification_id="signal_validation",
                rows=(RegressionRow(
                    term="signal",
                    estimate=0.2,
                    std_error=0.1,
                    t_statistic=2.0,
                    p_value=0.0455,
                    observation_count=30,
                ),),
            )
            output = table.to_dict()
            return ResearchProcessReceipt(
                run_id=base.run_id,
                experiment_id=base.experiment_id,
                status=ProcessRunStatus.COMPLETED_AUXILIARY,
                isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
                input_hash=request.input_hash,
                code_hash=base.code_hash,
                output_hash=canonical_hash(
                    {"input_hash": request.input_hash, "output_kind": base.output_kind, "output": output}
                ),
                output_kind=base.output_kind,
                output=output,
                failure_code=None,
                diagnostic=None,
                amendment_hash=base.amendment_hash,
                official_evidence_eligible=False,
            )
        rows = []
        for item in base.feature_rows:
            available = item["source_available_at"]
            if self.mismatched_clock:
                available = item["decision_time"]
            rows.append(
                ScoreRow(
                    symbol=item["symbol"],
                    decision_time=datetime.fromisoformat(item["decision_time"]),
                    source_available_at=datetime.fromisoformat(available),
                    score=float(item["close"]),
                    label_horizon_sessions=2,
                )
            )
        table = ScoreTable(
            experiment_id=base.experiment_id,
            code_identity_hash=base.code_identity_hash,
            data_identity_hash=base.data_identity_hash,
            rows=tuple(sorted(rows, key=lambda row: row.artifact_hash)),
        )
        output = table.to_dict()
        return ResearchProcessReceipt(
            run_id=base.run_id,
            experiment_id=base.experiment_id,
            status=ProcessRunStatus.COMPLETED_AUXILIARY,
            isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
            input_hash=request.input_hash,
            code_hash=base.code_hash,
            output_hash=canonical_hash(
                {"input_hash": request.input_hash, "output_kind": base.output_kind, "output": output}
            ),
            output_kind=base.output_kind,
            output=output,
            failure_code=None,
            diagnostic=None,
            amendment_hash=base.amendment_hash,
            official_evidence_eligible=True,
        )


def configuration(tmp_path: Path) -> ProductCanaryConfiguration:
    data_root = tmp_path / "data"
    frozen_repository(data_root)
    return ProductCanaryConfiguration(
        data_root=data_root,
        artifact_root=tmp_path / "product",
        question="用真实冻结数据验证一个价格横截面信号",
        docker_image="veriquant-m2-test:local",
        docker_image_id=IMAGE_ID,
        label_horizon_sessions=2,
        max_sessions=12,
        max_symbols=3,
    )


def test_prepare_data_uses_three_distinct_clocks_and_separates_labels(tmp_path):
    frozen = frozen_repository(tmp_path / "data")
    prepared = _prepare_data(frozen, horizon=2, max_sessions=12, max_symbols=3)
    assert prepared.feature_rows
    for row in prepared.feature_rows:
        assert row.source_available_at == row.decision_time - timedelta(hours=1)
        assert all("label" not in key and "future" not in key for key in row.features)
    assert all(datetime.fromisoformat(row.outcome_available_at) <= datetime.fromisoformat(prepared.training_cutoff) for row in prepared.training_rows)
    assert {(row.event_time, row.symbol) for row in prepared.labels} == {
        (row.decision_time, row.symbol) for row in prepared.feature_rows
    }


def test_prepare_data_does_not_expose_zero_coverage_schema_fields(tmp_path):
    frozen = frozen_repository(tmp_path / "data")
    snapshot = replace(
        frozen.snapshot,
        fields=("close", "volume", "declared_but_empty"),
        rows=tuple(
            replace(row, values={**dict(row.values), "declared_but_empty": None})
            for row in frozen.snapshot.rows
        ),
        coverage_by_field={"close": 1.0, "volume": 1.0, "declared_but_empty": 0.0},
    )
    prepared = _prepare_data(replace(frozen, snapshot=snapshot), horizon=2, max_sessions=12, max_symbols=3)
    assert prepared.visible_fields == ("close", "volume")
    assert all("declared_but_empty" not in row.features for row in prepared.feature_rows)
    assert all("declared_but_empty" not in row.features for row in prepared.training_rows)


def test_prepare_data_normalizes_non_finite_values_to_missing_without_imputation(tmp_path):
    frozen = frozen_repository(tmp_path / "data")
    rows = tuple(
        replace(row, values={**dict(row.values), "nullable_metric": float("nan")})
        for row in frozen.snapshot.rows
    )
    snapshot = replace(
        frozen.snapshot,
        fields=("close", "volume", "nullable_metric"),
        rows=rows,
        coverage_by_field={"close": 1.0, "volume": 1.0, "nullable_metric": 0.5},
    )
    prepared = _prepare_data(replace(frozen, snapshot=snapshot), horizon=2, max_sessions=12, max_symbols=3)
    assert "nullable_metric" in prepared.visible_fields
    assert all(row.features["nullable_metric"] is None for row in prepared.feature_rows)
    assert all(row.features["nullable_metric"] is None for row in prepared.training_rows)


def test_fake_runtime_composes_through_review_runner_evidence_and_chinese_report(tmp_path):
    runtime_holder = {}
    runner = ContractRunner()

    def runtime_factory(task, store):
        runtime_holder["runtime"] = FakeRuntime(task, store)
        return runtime_holder["runtime"]

    result = AgentResearchProductCanary(configuration(tmp_path)).run(
        runtime_factory=runtime_factory,
        runner_factory=lambda config: runner,
    )
    assert result.lifecycle == "COMPLETED"
    assert result.experiments[0].status.value == "OFFICIAL_EVIDENCE"
    assert result.experiments[0].official_metrics
    assert result.experiments[0].court_outcome in {"REJECT", "REVISE", "VALIDATED_RESEARCH"}
    assert result.experiments[0].court_decision_hash.startswith("sha256:")
    assert runtime_holder["runtime"].calls == runner.calls == 1
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "AlphaForge（千因研究院）研究报告" in report
    assert "Agent 只生成研究草案与代码" in report
    assert "RankIC" in report
    assert "本结论没有可冒充的官方指标" not in report


def test_regression_output_remains_useful_auxiliary_research_evidence(tmp_path):
    config = replace(configuration(tmp_path), output_kind="RegressionTable")

    result = AgentResearchProductCanary(config).run(
        runtime_factory=lambda task, store: FakeRuntime(task, store),
        runner_factory=lambda _: ContractRunner(),
    )

    experiment = result.experiments[0]
    assert result.lifecycle == "COMPLETED"
    assert experiment.status.value == "AUXILIARY_ONLY"
    assert experiment.research_verdict == "SUPPORTED"
    assert experiment.court_outcome is None
    assert experiment.official_evidence_hash is None
    assert experiment.research_evidence_hash.startswith("sha256:")
    assert dict(experiment.research_metrics)["coefficient.signal"] == "0.2"
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "coefficient.signal" in report
    assert "值得继续验证" in report
    assert "SUPPORTED" not in report
    assert "sha256:" not in report
    assert "辅助研究" in report
    assert "尚未形成完整官方结论" in report
    assert "机器证据支持该研究命题" not in report


def test_unfrozen_dynamic_tool_research_cannot_masquerade_as_official_evidence(tmp_path):
    result = AgentResearchProductCanary(configuration(tmp_path)).run(
        runtime_factory=lambda task, store: DynamicInvestigationRuntime(task, store),
        runner_factory=lambda _: ContractRunner(),
    )

    experiment = result.experiments[0]
    assert result.lifecycle == "COMPLETED"
    assert experiment.status.value == "AUXILIARY_ONLY"
    assert experiment.official_evidence_hash is None
    assert experiment.court_outcome is None
    assert experiment.research_evidence_hash.startswith("sha256:")
    assert experiment.research_metrics
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "辅助研究" in report
    assert "动态调查结果尚未冻结进本次数据身份" in report


def test_auxiliary_data_authority_sidecar_caps_official_evidence(tmp_path):
    config = configuration(tmp_path)
    frozen = FrozenValidationDataRepository(config.data_root).load()
    payload = {
        "schema_version": "alphaforge.dynamic-vibe-freeze/v1",
        "status": "READY",
        "authority_ceiling": "AUXILIARY_ONLY",
        "authority_reason": "DYNAMIC_VIBE_TOOL_DATA_NOT_APPROVED_FOR_OFFICIAL_EVIDENCE",
        "snapshot_hash": frozen.snapshot.artifact_hash,
    }
    payload["artifact_hash"] = canonical_hash(payload)
    (config.data_root / AUTHORITY_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    result = AgentResearchProductCanary(config).run(
        runtime_factory=lambda task, store: FakeRuntime(task, store),
        runner_factory=lambda _: ContractRunner(),
    )

    experiment = result.experiments[0]
    assert result.lifecycle == "COMPLETED"
    assert experiment.status.value == "AUXILIARY_ONLY"
    assert experiment.official_evidence_hash is None
    assert experiment.court_outcome is None
    assert experiment.research_evidence_hash.startswith("sha256:")


def test_task_prompt_requires_one_train_prediction_feature_accessor(tmp_path):
    from veriquant.agent_research_product.canary import _task_question

    prompt = _task_question(configuration(tmp_path), ("close", "index_pct_change"))
    assert "统一使用一个特征访问函数" in prompt
    assert "若 'features' in item" in prompt
    assert "禁止在 lambda 或特殊分支中直接读取研究字段" in prompt


def test_dynamic_tool_prompt_requires_proxy_or_gap_disclosure(tmp_path):
    from veriquant.agent_research_product.canary import _task_question

    config = replace(configuration(tmp_path), vibe_tool_names=("get_stock_news",))
    prompt = _task_question(config, ("close", "turnover_rate"), specialist_id="alternative_data")
    assert "动态工具结果只用于调查" in prompt
    assert "proxy_specs 或 data_gaps" in prompt
    assert "不得声称直接验证了新闻" in prompt


def test_product_canary_composes_metadata_and_selected_vibe_tools(tmp_path, monkeypatch):
    observed = {}

    class FakeVibeToolbox:
        def __init__(self, *, task, tool_names, handle_store, allow_network):
            del handle_store
            observed["tool_names"] = tool_names
            observed["allow_network"] = allow_network
            self.names = tool_names
            self.definitions = tuple(
                {
                    "name": name,
                    "description": "approved read-only Vibe data tool",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    "readonly": True,
                }
                for name in tool_names
            )
            self.recorded_receipts = ()

        def call(self, name, arguments):
            raise AssertionError(f"unexpected fake tool call: {name} {arguments}")

    monkeypatch.setattr("veriquant.agent_research_product.canary.VibeResearchToolbox", FakeVibeToolbox)
    config = replace(
        configuration(tmp_path),
        vibe_tool_names=("get_market_data", "get_stock_news"),
        allow_vibe_network_tools=True,
    )
    holder = {}

    def runtime_factory(task, store):
        holder["task"] = task
        return FakeRuntime(task, store)

    result = AgentResearchProductCanary(config).run(
        runtime_factory=runtime_factory,
        runner_factory=lambda _: ContractRunner(),
    )

    assert result.lifecycle == "COMPLETED"
    assert observed == {
        "tool_names": ("get_market_data", "get_stock_news"),
        "allow_network": True,
    }
    assert holder["task"].allowed_tools == (
        "describe_available_data", "get_market_data", "get_stock_news", "query_data_schema",
    )


def test_product_canary_runs_multiple_isolated_specialists_on_one_product_path(tmp_path):
    config = replace(
        configuration(tmp_path),
        specialist_ids=("fundamental", "macro_regime", "market_structure", "meta_alpha"),
        specialist_output_kinds=(
            ("fundamental", "RegressionTable"),
            ("macro_regime", "RegimeComparisonTable"),
            ("market_structure", "RegressionTable"),
            ("meta_alpha", "RegressionTable"),
        ),
    )
    calls = []

    def runtime_factory(task, store):
        calls.append((task.specialist_id, task.output_kind))
        return FakeRuntime(task, store)

    result = AgentResearchProductCanary(config).run(
        runtime_factory=runtime_factory,
        runner_factory=lambda _: ContractRunner(),
    )
    assert calls == [
        ("fundamental", "RegressionTable"),
        ("macro_regime", "RegimeComparisonTable"),
        ("market_structure", "RegressionTable"),
        ("meta_alpha", "RegressionTable"),
    ]
    assert len(result.experiments) == 4
    assert {item.specialist_id for item in result.experiments} == {
        "fundamental", "macro_regime", "market_structure", "meta_alpha",
    }
def test_score_clock_mismatch_fails_closed_before_official_evidence(tmp_path):
    result = AgentResearchProductCanary(configuration(tmp_path)).run(
        runtime_factory=lambda task, store: FakeRuntime(task, store),
        runner_factory=lambda config: ContractRunner(mismatched_clock=True),
    )
    assert result.lifecycle == "PARTIAL"
    assert result.experiments[0].status.value == "PARTIAL"
    assert result.experiments[0].official_evidence_hash is None
    assert result.result_kind.value == "PARTIAL_RESEARCH_RESULT"
    assert Path(result.report_path).is_file()


def test_canary_never_falls_back_when_repository_is_missing(tmp_path):
    with pytest.raises(ValueError, match="frozen_validation_data"):
        ProductCanaryConfiguration(
            data_root=tmp_path / "missing",
            artifact_root=tmp_path / "product",
            question="test",
            docker_image="veriquant-m2-test:local",
            docker_image_id=IMAGE_ID,
        )


def test_configuration_accepts_six_registered_specialists_and_rejects_unknown(tmp_path):
    data_root = tmp_path / "data"
    frozen_repository(data_root)
    for specialist in (
        "alternative_data", "expectations_events", "fundamental",
        "macro_regime", "market_structure", "meta_alpha",
    ):
        configured = ProductCanaryConfiguration(
            data_root=data_root,
            artifact_root=tmp_path / specialist,
            question="研究可见输入",
            docker_image="veriquant-m2-test:local",
            docker_image_id=IMAGE_ID,
            specialist_id=specialist,
        )
        assert configured.specialist_id == specialist
    with pytest.raises(ValueError, match="registered research specialist"):
        ProductCanaryConfiguration(
            data_root=data_root,
            artifact_root=tmp_path / "bad",
            question="研究可见输入",
            docker_image="veriquant-m2-test:local",
            docker_image_id=IMAGE_ID,
            specialist_id="broker",
        )


def test_wrong_source_label_is_rejected_not_masqueraded(tmp_path):
    data_root = tmp_path / "data"
    frozen_repository(data_root, source_label="UNAPPROVED_VENDOR")
    config = ProductCanaryConfiguration(
        data_root=data_root,
        artifact_root=tmp_path / "product",
        question="test",
        docker_image="veriquant-m2-test:local",
        docker_image_id=IMAGE_ID,
        label_horizon_sessions=2,
        max_sessions=12,
    )
    with pytest.raises(ValueError, match="approved proxy"):
        AgentResearchProductCanary(config).run(
            runtime_factory=lambda task, store: FakeRuntime(task, store),
            runner_factory=lambda config: ContractRunner(),
        )
