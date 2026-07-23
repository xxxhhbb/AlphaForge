from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from veriquant.agent_research_product import (
    AgentResearchProductService,
    ContentAddressedDraftSourceRepository,
    ProductExperimentStatus,
    ProductFailureCode,
    ProductRunRequest,
    ResearchReviewGateway,
)
from veriquant.agent_research_sandbox.contracts import (
    DiagnosticRow,
    DiagnosticTable,
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewPolicy,
    ExperimentReviewRequest,
    ReviewerSuggestion,
    SandboxAmendmentIdentity,
)
from veriquant.agent_research_sandbox.evaluator import EvidenceAdmissionStatus, SandboxEvidenceResult
from veriquant.agent_research_sandbox.process_runner import (
    AMENDMENT_VERSION,
    ProcessFailureCode,
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessReceipt,
    ResearchProcessRequest,
    canonical_hash,
)
from veriquant.agent_research_sandbox.report import ChineseResearchReportComposer
from veriquant.agent_research_sandbox.review import ExperimentReviewGate
from veriquant.agent_research_product.service import _receipt_dict
from veriquant.research_agent_runtime import (
    BranchCheckpointStore,
    InMemoryResearchToolbox,
    ResearchAgentFailureCode,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    ResearchAgentTask,
    ResearchCancellationToken,
    ResearchDraftArtifact,
    ResearchDraftKind,
    ResearchSwarmSchedulerAdapter,
)
from veriquant.research_run_controller import ResearchRunLifecycle, ResearchRunManifest
from veriquant.research_results import ResearchResultKind
from veriquant.research_toolbox import ContentAddressedHandleStore


NOW = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)


def h(char: str) -> str:
    return "sha256:" + char * 64


SOURCE = "def run_research(feature_rows, seed):\n    return {'rows': []}\n"
SOURCE_HASH = "sha256:" + hashlib.sha256(SOURCE.encode()).hexdigest()


def task(index: int) -> ResearchAgentTask:
    return ResearchAgentTask(
        task_id=f"task_{index}", run_id="run_product", specialist_id=f"specialist_{index}",
        question="研究可证伪信号", campaign_hash=h("a"), data_snapshot_hash=h("b"),
        decision_time=NOW, allowed_tools=(f"tool_{index}",),
    )


def manifest(tasks, *, run_id="run_product") -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id, campaign_hash=h("a"), intent_hash=h("c"), data_plan_hash=h("d"),
        route_plan_hash=h("e"), policy_hash=h("f"), code_hash=h("0"),
        selected_group_ids=tuple(sorted(item.specialist_id for item in tasks)),
        candidate_request_hashes=(), created_at=NOW, provider_id="fake_provider",
        model_id="fake_model", data_snapshot_hash=h("b"),
        current_stage=ResearchRunLifecycle.PLANNING, remaining_candidate_slots=12,
    )


class Runtime:
    def __init__(self, source_hash=SOURCE_HASH, status=ResearchAgentStatus.COMPLETED, wait=False):
        self.source_hash = source_hash
        self.status = status
        self.wait = wait
        self.calls = 0

    def run(self, *, task, toolbox, upstream_context, cancellation: ResearchCancellationToken):
        self.calls += 1
        if self.wait:
            for _ in range(100):
                if cancellation.cancelled:
                    return outcome(task, ResearchAgentStatus.CANCELLED)
                time.sleep(0.005)
        return outcome(task, self.status, self.source_hash)


def outcome(task, status, source_hash=SOURCE_HASH):
    failure = status in {ResearchAgentStatus.FAILED, ResearchAgentStatus.BLOCKED, ResearchAgentStatus.CANCELLED}
    return ResearchAgentOutcome(
        task_id=task.task_id, task_hash=task.artifact_hash, specialist_id=task.specialist_id,
        status=status, plan_hash=None if failure else h("9"), investigation_receipts=(),
        draft_artifacts=() if failure else (
            ResearchDraftArtifact(kind=ResearchDraftKind.CODE, content_hash=source_hash, media_type="text/x-python-draft"),
        ), provider_calls=1, tool_calls=0,
        failure_code=ResearchAgentFailureCode.CANCELLED if status is ResearchAgentStatus.CANCELLED else (
            ResearchAgentFailureCode.RUNTIME_FAILED if failure else None
        ), failure_origin="FAKE" if failure else None,
    )


class Binding:
    def __init__(self):
        self.policy = ExperimentReviewPolicy()
        self.amendment = SandboxAmendmentIdentity(
            amendment_id="amendment_02", amendment_version="research_sandbox_v1",
            constitution_hash=h("1"), policy_hash=h("2"), authorized_at=NOW,
        )
        self.data = ExperimentDataIdentity(
            dataset_hash=h("3"), snapshot_hash=h("b"), universe_hash=h("4"), schema_hash=h("5"),
            availability_policy_hash=h("6"), decision_start=NOW, decision_end=NOW,
        )

    def review_request(self, *, manifest, outcome, source_text):
        code = ExperimentCodeIdentity(
            source_hash="sha256:" + hashlib.sha256(source_text.encode()).hexdigest(),
            python_runtime_hash=h("7"), dependency_manifest_hash=h("8"),
            review_policy_hash=self.policy.artifact_hash, amendment_hash=self.amendment.artifact_hash,
        )
        return ExperimentReviewRequest(
            experiment_id=f"{outcome.task_id}_{code.source_hash[-12:]}", proposer_id=outcome.specialist_id,
            reviewer_id="mechanical_reviewer", source_text=source_text, code_identity=code,
            data_identity=self.data, amendment=self.amendment, policy=self.policy,
        )

    def process_request(self, *, manifest, review_request, review_decision, source_text):
        return ResearchProcessRequest(
            run_id=manifest.run_id, experiment_id=review_request.experiment_id, code_text=source_text,
            code_hash=review_request.code_identity.source_hash,
            code_identity_hash=review_request.code_identity.artifact_hash,
            data_identity_hash=review_request.data_identity.artifact_hash,
            review_decision=review_decision, feature_rows=({"close": 1.0},),
            output_kind="DiagnosticTable", seed=7, amendment_hash=review_request.amendment.artifact_hash,
            amendment_version=AMENDMENT_VERSION,
        )

    def parse_table(self, *, review_request, receipt):
        return DiagnosticTable(
            experiment_id=review_request.experiment_id,
            code_identity_hash=review_request.code_identity.artifact_hash,
            data_identity_hash=review_request.data_identity.artifact_hash,
            rows=(DiagnosticRow(
                diagnostic_id="sandbox_smoke", metric_name="row_count", value=1.0,
                unit="count", interpretation="受控测试输出；不是官方指标。",
            ),),
        )

    def evidence_arguments(self, **kwargs):
        return {"table": kwargs["table"]}


class Runner:
    def execute(self, request):
        output = {"kind": "fake-bound-output"}
        output_hash = canonical_hash({"input_hash": request.input_hash, "output_kind": request.output_kind, "output": output})
        return ResearchProcessReceipt(
            run_id=request.run_id, experiment_id=request.experiment_id,
            status=ProcessRunStatus.COMPLETED_AUXILIARY,
            isolation_level=ProcessIsolationLevel.OS_SANDBOXED, input_hash=request.input_hash,
            code_hash=request.code_hash, output_hash=output_hash, output_kind=request.output_kind,
            output=output, failure_code=None, diagnostic=None, amendment_hash=request.amendment_hash,
            official_evidence_eligible=True,
        )


class ProtocolFailThenCompleteRunner(Runner):
    def __init__(self):
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if self.calls == 1:
            return ResearchProcessReceipt(
                run_id=request.run_id,
                experiment_id=request.experiment_id,
                status=ProcessRunStatus.FAILED,
                isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
                input_hash=request.input_hash,
                code_hash=request.code_hash,
                output_hash=None,
                output_kind=request.output_kind,
                output=None,
                failure_code=ProcessFailureCode.PROTOCOL_FAILED,
                diagnostic="PROTOCOL_FAILED:TypeError:line12",
                amendment_hash=request.amendment_hash,
                official_evidence_eligible=False,
            )
        return super().execute(request)


class RevisingRuntime(Runtime):
    supports_review_revision = True

    def __init__(self, initial_hash, revised_hash):
        super().__init__(initial_hash)
        self.revised_hash = revised_hash

    def run(self, *, task, toolbox, upstream_context, cancellation):
        self.calls += 1
        selected = self.revised_hash if upstream_context else self.source_hash
        return outcome(task, ResearchAgentStatus.COMPLETED, selected)


class Evaluator:
    def evaluate(self, **kwargs):
        return SandboxEvidenceResult(
            status=EvidenceAdmissionStatus.OFFICIAL_EVIDENCE,
            table_hash=kwargs["table"].artifact_hash,
            official_evidence=SimpleNamespace(artifact_hash=h("a")), evaluation_row_count=2,
        )


class AuxiliaryEvaluator:
    def evaluate(self, **kwargs):
        return SandboxEvidenceResult(
            status=EvidenceAdmissionStatus.AUXILIARY_ONLY,
            table_hash=kwargs["table"].artifact_hash,
            auxiliary_output_class=kwargs["table"].output_class,
        )


def toolbox(item):
    return InMemoryResearchToolbox(task=item, handlers={item.allowed_tools[0]: lambda args: None})


def service(tmp_path, source_store):
    return AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=source_store, review_gate=ExperimentReviewGate(), runner=Runner(),
        binding=Binding(), evidence_evaluator=Evaluator(), report_composer=ChineseResearchReportComposer(),
    )


def request(tasks, runtimes, *, run_manifest=None):
    tasks = tuple(tasks)
    return ProductRunRequest(
        manifest=run_manifest or manifest(tasks), question_id="q_001", question="测试一个可证伪的 Alpha 问题",
        tasks=tasks, runtimes=runtimes, toolboxes={item.task_id: toolbox(item) for item in tasks},
    )


def test_runner_receipt_persists_only_the_typed_diagnostic() -> None:
    receipt = SimpleNamespace(
        artifact_hash=h("a"), run_id="run_1", experiment_id="experiment_1",
        status=ProcessRunStatus.FAILED, isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
        input_hash=h("b"), code_hash=h("c"), output_hash=None, output_kind="RegressionTable",
        failure_code=ProcessFailureCode.PROTOCOL_FAILED,
        diagnostic="PROTOCOL_FAILED:TypeError", official_evidence_eligible=False,
    )
    payload = _receipt_dict(receipt)
    assert payload["diagnostic"] == "PROTOCOL_FAILED:TypeError"
    assert "traceback" not in str(payload).casefold()


def test_typed_runner_protocol_failure_triggers_one_agent_revision(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    initial = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    revised_source = SOURCE.replace("rows': []", "rows': [{'diagnostic_id': 'sandbox_smoke'}]")
    revised = store.put_bytes(revised_source.encode(), media_type="text/x-python-draft")
    item = task(1)
    runtime = RevisingRuntime(initial.handle_id, revised.handle_id)
    runner = ProtocolFailThenCompleteRunner()
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(),
        runner=runner,
        binding=Binding(),
        evidence_evaluator=Evaluator(),
    )

    result = product.run(request((item,), {item.specialist_id: runtime}))

    assert runtime.calls == 2
    assert runner.calls == 2
    assert len(result.experiments) == 2
    assert result.experiments[0].failure_code == ProductFailureCode.RUNNER_BLOCKED.value
    assert result.experiments[0].review_feedback_codes == (
        "RUNNER_PROTOCOL_FAILED_TYPEERROR_LINE12",
    )
    assert result.experiments[1].status is ProductExperimentStatus.OFFICIAL_EVIDENCE


def test_shared_content_addressed_source_runs_review_runner_evidence_and_report(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    result = service(tmp_path, ContentAddressedDraftSourceRepository(store)).run(
        request((item,), {item.specialist_id: Runtime(handle.handle_id)})
    )
    assert result.lifecycle == ResearchRunLifecycle.COMPLETED.value
    assert result.experiments[0].status is ProductExperimentStatus.OFFICIAL_EVIDENCE
    assert Path(result.report_path).exists()
    report_text = Path(result.report_path).read_text(encoding="utf-8")
    assert "机器指标" in report_text
    run_dir = Path(result.report_path).parent
    for artifact in (
        "answer_spec.json", "candidate_definitions.json", "evidence_collaboration.json",
        "research_answer.json", "research_result_bundle.json", "report.json",
    ):
        assert (run_dir / artifact).is_file()
    report_payload = __import__("json").loads((run_dir / "report.json").read_text(encoding="utf-8"))
    answer_payload = __import__("json").loads((run_dir / "research_answer.json").read_text(encoding="utf-8"))
    assert report_payload["schema_version"] == "alphaforge.customer-research-report/v3"
    assert report_payload["research_answer"]["artifact_hash"] == result.answer_hash
    assert answer_payload["direct_answer"]
    assert result.answer_hash is not None
    assert result.result_kind is ResearchResultKind.EVALUATED_RESEARCH_RESULT
    assert (tmp_path / "research_runs" / result.run_id / "research_result_bundle.json").is_file()


def test_missing_source_is_typed_blocker_and_still_has_customer_report(tmp_path):
    item = task(1)
    result = service(tmp_path, ContentAddressedDraftSourceRepository(ContentAddressedHandleStore(tmp_path / "empty"))).run(
        request((item,), {item.specialist_id: Runtime()})
    )
    assert result.lifecycle == ResearchRunLifecycle.BLOCKED.value
    assert result.experiments[0].failure_code == ProductFailureCode.SOURCE_ARTIFACT_MISSING.value
    assert Path(result.report_path).exists()


def test_auxiliary_output_is_partial_research_result_with_report(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(), runner=Runner(), binding=Binding(),
        evidence_evaluator=AuxiliaryEvaluator(),
    )
    result = product.run(request((item,), {item.specialist_id: Runtime(handle.handle_id)}))
    assert result.lifecycle == ResearchRunLifecycle.COMPLETED.value
    assert result.experiments[0].status is ProductExperimentStatus.AUXILIARY_ONLY
    assert result.result_kind is ResearchResultKind.PARTIAL_RESEARCH_RESULT
    assert "尚未形成完整官方结论" in Path(result.report_path).read_text(encoding="utf-8")


def test_static_rejection_is_negative_research_result_not_platform_failure(tmp_path):
    unsafe = "import os\ndef run_research(feature_rows, seed):\n    return {'rows': []}\n"
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(unsafe.encode(), media_type="text/x-python-draft")
    item = task(1)
    result = service(tmp_path, ContentAddressedDraftSourceRepository(store)).run(
        request((item,), {item.specialist_id: Runtime(handle.handle_id)})
    )
    assert result.lifecycle == ResearchRunLifecycle.COMPLETED.value
    assert result.experiments[0].status is ProductExperimentStatus.NEGATIVE
    assert result.experiments[0].runner_receipt_hash is None
    assert result.result_kind is ResearchResultKind.NEGATIVE_RESEARCH_RESULT
    assert "没有候选通过统一审查" in Path(result.report_path).read_text(encoding="utf-8")


def test_mechanical_rejection_returns_redacted_codes_for_one_agent_revision(tmp_path):
    unsafe = "import os\ndef run_research(feature_rows, seed):\n    return {'rows': []}\n"
    safe = SOURCE
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    unsafe_handle = store.put_bytes(unsafe.encode(), media_type="text/x-python-draft")
    safe_handle = store.put_bytes(safe.encode(), media_type="text/x-python-draft")
    item = task(1)

    class RevisingRuntime(Runtime):
        supports_review_revision = True

        def __init__(self):
            super().__init__(unsafe_handle.handle_id)
            self.feedback = None

        def run(self, *, task, toolbox, upstream_context, cancellation):
            self.calls += 1
            if self.calls == 1:
                return outcome(task, ResearchAgentStatus.COMPLETED, unsafe_handle.handle_id)
            self.feedback = dict(upstream_context)
            return outcome(task, ResearchAgentStatus.COMPLETED, safe_handle.handle_id)

    runtime = RevisingRuntime()
    result = service(tmp_path, ContentAddressedDraftSourceRepository(store)).run(
        request((item,), {item.specialist_id: runtime})
    )
    assert runtime.calls == 2
    assert "FORBIDDEN_IMPORT" in runtime.feedback["review_feedback_codes"]
    assert "sealed" not in str(runtime.feedback).casefold()
    assert [entry.status for entry in result.experiments] == [
        ProductExperimentStatus.NEGATIVE,
        ProductExperimentStatus.OFFICIAL_EVIDENCE,
    ]
    assert result.result_kind is ResearchResultKind.EVALUATED_RESEARCH_RESULT


def test_data_driven_formula_review_uses_existing_single_agent_revision(tmp_path):
    initial_source = (
        "def run_research(feature_rows, seed):\n"
        "    feature_fields = ['random_feature', 'turnover_rate']\n"
        "    z_scores = [1.0, 2.0]\n"
        "    score = z_scores[0] + z_scores[1]\n"
        "    return {'rows': []}\n"
    )
    revised_source = initial_source.replace(
        "z_scores[0] + z_scores[1]", "z_scores[0] * z_scores[1]"
    )
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    initial_handle = store.put_bytes(initial_source.encode(), media_type="text/x-python-draft")
    revised_handle = store.put_bytes(revised_source.encode(), media_type="text/x-python-draft")
    item = task(1)

    class SemanticBinding(Binding):
        def formula_semantic_arguments(self, *, proposer_id):
            return {
                "output_kind": "ScoreTable",
                "data_snapshot_hash": h("b"),
                "feature_rows": (
                    {"decision_time": "2026-01-01", "random_feature": 1, "turnover_rate": 1},
                    {"decision_time": "2026-01-01", "random_feature": 1, "turnover_rate": 2},
                    {"decision_time": "2026-01-01", "random_feature": 1, "turnover_rate": 3},
                    {"decision_time": "2026-01-02", "random_feature": 2, "turnover_rate": 2},
                    {"decision_time": "2026-01-02", "random_feature": 2, "turnover_rate": 4},
                    {"decision_time": "2026-01-02", "random_feature": 2, "turnover_rate": 8},
                ),
            }

    class SemanticRevisingRuntime(Runtime):
        supports_review_revision = True

        def __init__(self):
            super().__init__(initial_handle.handle_id)
            self.feedback = None

        def run(self, *, task, toolbox, upstream_context, cancellation):
            self.calls += 1
            if not upstream_context:
                return outcome(task, ResearchAgentStatus.COMPLETED, initial_handle.handle_id)
            self.feedback = dict(upstream_context)
            return outcome(task, ResearchAgentStatus.COMPLETED, revised_handle.handle_id)

    runtime = SemanticRevisingRuntime()
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(), runner=Runner(), binding=SemanticBinding(),
        evidence_evaluator=Evaluator(),
    )
    result = product.run(request((item,), {item.specialist_id: runtime}))

    assert runtime.calls == 2
    assert runtime.feedback["review_feedback_codes"] == "CROSS_SECTION_CONSTANT_ADDITIVE_TERM"
    assert [entry.status for entry in result.experiments] == [
        ProductExperimentStatus.NEGATIVE,
        ProductExperimentStatus.OFFICIAL_EVIDENCE,
    ]
    assert result.experiments[0].failure_code == (
        ProductFailureCode.FORMULA_SEMANTIC_REVIEW_REQUIRED.value
    )
    assert result.experiments[0].formula_review_hash is not None
    assert result.experiments[1].formula_review_hash is not None
    assert result.experiments[1].revision_of_source_hash == initial_handle.handle_id
    assert result.experiments[1].revision_reason_codes == (
        "CROSS_SECTION_CONSTANT_ADDITIVE_TERM",
    )
    report_text = Path(result.report_path).read_text(encoding="utf-8")
    assert "本轮修订" in report_text
    assert "横截面加法项" in report_text


def test_tool_unavailable_branch_becomes_data_gap_report(tmp_path):
    item = task(1)

    class DataGapRuntime:
        def run(self, *, task, toolbox, upstream_context, cancellation):
            return ResearchAgentOutcome(
                task_id=task.task_id, task_hash=task.artifact_hash,
                specialist_id=task.specialist_id, status=ResearchAgentStatus.BLOCKED,
                plan_hash=None, investigation_receipts=(), draft_artifacts=(),
                provider_calls=0, tool_calls=0,
                failure_code=ResearchAgentFailureCode.TOOL_UNAVAILABLE,
                failure_origin="RESEARCH_TOOLBOX",
            )

    result = service(
        tmp_path,
        ContentAddressedDraftSourceRepository(ContentAddressedHandleStore(tmp_path / "empty")),
    ).run(request((item,), {item.specialist_id: DataGapRuntime()}))
    assert result.lifecycle == ResearchRunLifecycle.PARTIAL.value
    assert result.result_kind is ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
    assert "数据缺口报告" in Path(result.report_path).read_text(encoding="utf-8")


def test_custom_output_parse_failure_is_partial_and_still_reported(tmp_path):
    class CustomOutputBinding(Binding):
        def parse_table(self, *, review_request, receipt):
            raise ValueError("custom output has no registered evaluator")

    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(), runner=Runner(), binding=CustomOutputBinding(),
        evidence_evaluator=Evaluator(),
    )
    result = product.run(request((item,), {item.specialist_id: Runtime(handle.handle_id)}))
    assert result.lifecycle == ResearchRunLifecycle.PARTIAL.value
    assert result.experiments[0].status is ProductExperimentStatus.PARTIAL
    assert result.result_kind is ResearchResultKind.PARTIAL_RESEARCH_RESULT
    assert result.experiments[0].review_hash is not None
    assert result.experiments[0].runner_receipt_hash is not None
    assert Path(result.report_path).is_file()


def test_semantic_review_advice_flows_through_one_gateway_but_cannot_grant_authority(tmp_path):
    class Advisor:
        def suggest(self, review_request):
            return (ReviewerSuggestion(
                suggestion_id="reviewer.method_note",
                message="建议在报告中解释代理变量与基准的差异。",
            ),)

    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(), runner=Runner(), binding=Binding(),
        evidence_evaluator=Evaluator(), review_advisor=Advisor(),
    )
    result = product.run(request((item,), {item.specialist_id: Runtime(handle.handle_id)}))
    review_path = next((tmp_path / "research_runs" / result.run_id / "experiments").glob("*/review.json"))
    review = __import__("json").loads(review_path.read_text(encoding="utf-8"))
    assert review["reviewer_suggestions"][0]["suggestion_id"] == "reviewer.method_note"
    assert review["grants_execution_authority"] is False
    assert result.experiments[0].status is ProductExperimentStatus.OFFICIAL_EVIDENCE


def test_all_six_specialists_use_one_gateway_without_native_field_qualification(tmp_path):
    specialists = (
        "alternative_data", "expectations_events", "fundamental",
        "macro_regime", "market_structure", "meta_alpha",
    )
    tasks = tuple(
        ResearchAgentTask(
            task_id=f"task_{name}", run_id="run_product", specialist_id=name,
            question="自由设计可审查实验", campaign_hash=h("a"), data_snapshot_hash=h("b"),
            decision_time=NOW, allowed_tools=(f"tool_{name}",),
        )
        for name in specialists
    )
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    product = service(tmp_path, ContentAddressedDraftSourceRepository(store))
    result = product.run(request(
        tasks, {item.specialist_id: Runtime(handle.handle_id) for item in tasks}
    ))
    assert isinstance(product.review_gateway, ResearchReviewGateway)
    assert len(result.experiments) == 6
    assert {item.status for item in result.experiments} == {ProductExperimentStatus.OFFICIAL_EVIDENCE}
    gateway_source = Path("agent/src/veriquant/agent_research_product/review_gateway.py").read_text(encoding="utf-8")
    assert "_GROUP_NATIVE_FIELDS" not in gateway_source
    assert "qualification_role" not in gateway_source


def test_independent_failed_branch_retains_completed_branch_and_partial_report(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    first, second = task(1), task(2)
    result = service(tmp_path, ContentAddressedDraftSourceRepository(store)).run(request(
        (first, second),
        {first.specialist_id: Runtime(handle.handle_id), second.specialist_id: Runtime(status=ResearchAgentStatus.FAILED)},
    ))
    assert result.lifecycle == ResearchRunLifecycle.PARTIAL.value
    assert len(result.experiments) == 1
    assert any(item.startswith(ProductFailureCode.BRANCH_FAILED.value) for item in result.failures)


def test_terminal_replay_is_idempotent_and_does_not_rerun_agent(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item, runtime = task(1), Runtime(handle.handle_id)
    product = service(tmp_path, ContentAddressedDraftSourceRepository(store))
    run_request = request((item,), {item.specialist_id: runtime})
    first = product.run(run_request)
    second = product.run(run_request)
    assert first == second
    assert runtime.calls == 1


def test_cooperative_cancel_then_resume_does_not_duplicate_committed_work(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    waiting = Runtime(handle.handle_id, wait=True)
    product = service(tmp_path, ContentAddressedDraftSourceRepository(store))
    run_request = request((item,), {item.specialist_id: waiting})
    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault("result", product.run(run_request)))
    thread.start()
    for _ in range(100):
        if product.cancel(item.run_id):
            break
        time.sleep(0.005)
    thread.join(timeout=3)
    assert holder["result"].lifecycle == ResearchRunLifecycle.CANCELLED.value
    resumed_runtime = Runtime(handle.handle_id)
    resumed = product.run(request((item,), {item.specialist_id: resumed_runtime}), resume=True)
    assert resumed.lifecycle == ResearchRunLifecycle.COMPLETED.value
    assert resumed_runtime.calls == 1


def test_product_path_has_no_legacy_formula_or_fixture_imports():
    source = Path("agent/src/veriquant/agent_research_product/service.py").read_text(encoding="utf-8")
    for forbidden in ("FactorDAG", "deterministic fixture", "campaign_real_llm", "research_os_v2"):
        assert forbidden not in source


def test_broken_optional_report_builder_falls_back_to_terminal_report(tmp_path):
    class BrokenReportBuilder:
        def build(self, **kwargs):
            raise RuntimeError("presentation provider failed")

    store = ContentAddressedHandleStore(tmp_path / "draft_sources")
    handle = store.put_bytes(SOURCE.encode(), media_type="text/x-python-draft")
    item = task(1)
    product = AgentResearchProductService(
        root=tmp_path,
        scheduler=ResearchSwarmSchedulerAdapter(checkpoint_store=BranchCheckpointStore(tmp_path)),
        source_repository=ContentAddressedDraftSourceRepository(store),
        review_gate=ExperimentReviewGate(), runner=Runner(), binding=Binding(),
        evidence_evaluator=Evaluator(), report_builder=BrokenReportBuilder(),
    )
    result = product.run(request((item,), {item.specialist_id: Runtime(handle.handle_id)}))
    assert result.lifecycle == ResearchRunLifecycle.FAILED.value
    assert ProductFailureCode.INFRASTRUCTURE_FAILED.value in result.failures
    assert Path(result.report_path).exists()
