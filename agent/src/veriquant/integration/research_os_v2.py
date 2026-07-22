"""AlphaForge Research OS V2 product integration.

This module owns orchestration and public projections only.  Candidate
calculation, PIT, official visible evidence and the Trial Ledger stay in their
existing deterministic services.  LLM providers remain proposal-only.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any, Callable, Mapping

from ..alphaforge_llm import AlphaForgeLLMConfigLoader
from ..candidate_translation import CandidateTranslationStatus
from ..contracts import ContractValidationError
from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..data_planning import DataAvailabilitySnapshot, DeterministicDataPlanner
from ..dynamic_data_planning import (
    CapabilityAwareResearchRouter,
    DataCapabilitySnapshot,
    DataPlanningRequest as DynamicDataPlanningRequest,
    DataReadiness,
    DynamicDataRequirementCompiler,
)
from ..dynamic_data_integration import enrich_frozen_validation_data
from ..evidence.models import CostModel, WalkForwardSpec
from ..research_campaign import MethodModel, ResearchDepth, ResearchMode
from ..research_group_real_llm_v2 import (
    AlphaForgeRealLLMTransport,
    GroupExecutionPolicy,
    GroupProposalProvenance,
    SelectedGroupRealLLMProposalProvider,
)
from ..research_group_runtime import (
    GroupCandidateDraft,
    GroupProposalPayload,
    GroupRuntimeBranchResult,
    GroupRuntimeResult,
    ResearchGroupProposalRuntime,
)
from ..research_group_runtime.runtime import GroupBranchStatus
from ..research_group_runtime.selector import (
    DeterministicResearchGroupSelector,
    GroupSelectionContext,
)
from ..research_os import AnswerLevel, ResearchGroupSubmission, ValidationDisposition
from ..research_planner import (
    DeterministicResearchQuestionPlanner,
    QuestionAssessmentRequest,
)
from ..research_validation_pipeline_v2 import (
    FrozenValidationData,
    ResearchValidationPipelineRequest,
    ResearchValidationPipelineV2,
)
from ..research_validation_pipeline_v2.models import FrozenRowProvenance


SCHEMA_VERSION = "alphaforge.research-os-v2-integration/v2"
FROZEN_DATA_SCHEMA_VERSION = "alphaforge.frozen-validation-data/v2"
LEGACY_FROZEN_DATA_SCHEMA_VERSIONS = {SCHEMA_VERSION}
APPROVED_SOURCE = "APPROVED_PROXY_DATA_VENDOR"

DEPTHS = {
    "快速探索": ResearchDepth.QUICK_EXPLORATION,
    "标准研究": ResearchDepth.STANDARD_RESEARCH,
    "严格验证": ResearchDepth.STRICT_VALIDATION,
}
MODE_LABELS = {
    ResearchMode.NEW_ALPHA_EXPLORATION: "新 Alpha 探索",
    ResearchMode.VALIDATE_IDEA: "验证研究想法",
    ResearchMode.IMPROVE_EXISTING_FACTOR: "改进现有因子",
    ResearchMode.PORTFOLIO_GAP_SEARCH: "补充组合缺口",
    ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: "因子与组合诊断",
    ResearchMode.EVENT_MARKET_TOPIC: "事件与市场专题",
}
GROUP_LABELS = {
    "fundamental": "基本面研究组",
    "expectations_events": "预期与事件研究组",
    "market_structure": "市场行为与交易可行性研究组",
    "alternative_data": "另类数据研究组",
    "macro_regime": "宏观状态研究组",
    "meta_alpha": "组合增量研究组",
    "research_baseline": "确定性研究基准",
}


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_id(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 128 or any(token in value for token in ("..", "/", "\\", "\x00", ":")):
        raise ValueError(f"invalid {field}")
    if not all(char.isalnum() or char in "._-" for char in value):
        raise ValueError(f"invalid {field}")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = handle.name
    try:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = handle.name
    try:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


class FrozenValidationDataRepository:
    """Fixed-root, canonical JSON repository for qualified frozen data."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / "frozen_validation_data.json"

    def save(self, value: FrozenValidationData) -> str:
        payload = {
            "schema_version": FROZEN_DATA_SCHEMA_VERSION,
            "qualification_hash": value.qualification_hash,
            "source_label": value.source_label,
            "snapshot": {
                "dataset_id": value.snapshot.dataset_id,
                "decision_time": value.snapshot.decision_time.isoformat(),
                "start_time": value.snapshot.start_time.isoformat(),
                "fields": list(value.snapshot.fields),
                "rows": [
                    {"event_time": row.event_time.isoformat(), "symbol": row.symbol, "values": dict(row.values)}
                    for row in value.snapshot.rows
                ],
                "historical_universe": {str(key): list(symbols) for key, symbols in value.snapshot.historical_universe.items()},
                "coverage_by_field": dict(value.snapshot.coverage_by_field),
            },
            "labels": dict(value.labels),
            "row_provenance": [
                {
                    "symbol": item.symbol,
                    "event_time": item.event_time.isoformat(),
                    "published_at": item.published_at.isoformat(),
                    "available_at": item.available_at.isoformat(),
                    "revision_id": item.revision_id,
                    "source_kind": item.source_kind,
                    "source_label": item.source_label,
                    "provider_class": item.provider_class,
                    "source_class": item.source_class,
                    "input_origin": item.input_origin,
                    "captured_at": item.captured_at.isoformat(),
                    "source_record_hash": item.source_record_hash,
                    "adapter_manifest_hash": item.adapter_manifest_hash,
                    "calendar_hash": item.calendar_hash,
                    "universe_hash": item.universe_hash,
                    "availability_policy_hash": item.availability_policy_hash,
                    "publication_time_policy": item.publication_time_policy,
                }
                for item in value.row_provenance
            ],
        }
        payload["artifact_hash"] = _hash(payload)
        _atomic_json(self.path, payload)
        return str(payload["artifact_hash"])

    def load(self, *, require_strict_provenance: bool = False) -> FrozenValidationData | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        claimed = payload.pop("artifact_hash", None)
        if claimed != _hash(payload):
            raise ValueError("frozen validation data hash mismatch")
        schema_version = str(payload.get("schema_version", ""))
        if schema_version not in LEGACY_FROZEN_DATA_SCHEMA_VERSIONS | {FROZEN_DATA_SCHEMA_VERSION}:
            raise ValueError("unsupported frozen validation data schema_version")
        raw_provenance = payload.get("row_provenance", ())
        provenance_fields = {
            "symbol", "event_time", "published_at", "available_at", "revision_id",
            "source_kind", "source_label", "provider_class", "source_class",
            "input_origin", "captured_at", "source_record_hash", "adapter_manifest_hash",
            "calendar_hash", "universe_hash", "availability_policy_hash",
            "publication_time_policy",
        }
        for item in raw_provenance:
            if not isinstance(item, Mapping) or not provenance_fields <= set(item):
                raise ValueError("frozen row provenance is incomplete")
        if require_strict_provenance and not raw_provenance:
            raise ValueError("strict frozen validation data unavailable: row clocks and provenance are missing")
        raw = payload["snapshot"]
        fields = tuple(str(item) for item in raw["fields"])
        snapshot = MarketSnapshot(
            dataset_id=str(raw["dataset_id"]),
            decision_time=datetime.fromisoformat(str(raw["decision_time"])),
            start_time=datetime.fromisoformat(str(raw["start_time"])),
            fields=fields,
            rows=tuple(
                SnapshotRow(
                    event_time=datetime.fromisoformat(str(row["event_time"])),
                    symbol=str(row["symbol"]),
                    values={field: row["values"].get(field) for field in fields},
                )
                for row in raw["rows"]
            ),
            historical_universe={str(key): tuple(value) for key, value in raw["historical_universe"].items()},
            coverage_by_field={str(key): float(value) for key, value in raw["coverage_by_field"].items()},
        )
        return FrozenValidationData(
            snapshot=snapshot,
            labels={str(key): float(value) for key, value in payload["labels"].items()},
            qualification_hash=str(payload["qualification_hash"]),
            source_label=str(payload["source_label"]),
            row_provenance=tuple(
                FrozenRowProvenance(
                    symbol=str(item["symbol"]),
                    event_time=datetime.fromisoformat(str(item["event_time"])),
                    published_at=datetime.fromisoformat(str(item["published_at"])),
                    available_at=datetime.fromisoformat(str(item["available_at"])),
                    revision_id=str(item["revision_id"]),
                    source_kind=str(item["source_kind"]),
                    source_label=str(item["source_label"]),
                    provider_class=str(item["provider_class"]),
                    source_class=str(item["source_class"]),
                    input_origin=str(item["input_origin"]),
                    captured_at=datetime.fromisoformat(str(item["captured_at"])),
                    source_record_hash=str(item["source_record_hash"]),
                    adapter_manifest_hash=str(item["adapter_manifest_hash"]),
                    calendar_hash=str(item["calendar_hash"]),
                    universe_hash=str(item["universe_hash"]),
                    availability_policy_hash=str(item["availability_policy_hash"]),
                    publication_time_policy=str(item["publication_time_policy"]),
                )
                for item in raw_provenance
            ),
        )


@dataclass(frozen=True)
class _StoredPlan:
    plan_id: str
    question: str
    depth_label: str
    collaboration_style: str
    suggestion: Mapping[str, Any]
    plan_hash: str


class ResearchOSV2Service:
    """Persistent product gateway with an idempotent background-run boundary."""

    def __init__(
        self,
        *,
        root: str | Path,
        data_loader: Callable[[], FrozenValidationData | None] | None = None,
        dotenv_path: str | Path = "agent/.env",
        dynamic_freeze_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_repository = FrozenValidationDataRepository(self.root / "data")
        self.data_loader = data_loader or self.data_repository.load
        self.dynamic_freeze_root = (
            None
            if dynamic_freeze_root is None
            else Path(dynamic_freeze_root).resolve()
        )
        self._cached_data_key: tuple[str, int, int] | None = None
        self._cached_data: FrozenValidationData | None = None
        self.dotenv_path = Path(dotenv_path)
        self.planner = DeterministicResearchQuestionPlanner(data_planner=DeterministicDataPlanner())
        self.dynamic_data_compiler = DynamicDataRequirementCompiler()
        self.dynamic_data_router = CapabilityAwareResearchRouter()
        self.selector = DeterministicResearchGroupSelector()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alphaforge-research")
        self._futures: dict[str, Future[None]] = {}
        self._lock = RLock()

    def create_suggestion(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        question = str(payload.get("question") or "").strip()
        if not question:
            raise ValueError("question is required")
        depth_label = str(payload.get("depth") or "标准研究")
        if depth_label not in DEPTHS:
            raise ValueError("unsupported research depth")
        collaboration = str(payload.get("collaborationStyle") or "标准式")
        if collaboration not in {"引导式", "标准式", "批处理式"}:
            raise ValueError("unsupported collaboration style")
        data, data_failure = self._try_data()
        availability = _availability(data) if data is not None else None
        assessment = self.planner.assess(QuestionAssessmentRequest(
            question=question,
            preferred_depth=DEPTHS[depth_label],
            decision_time=data.snapshot.decision_time if data else datetime.now(timezone.utc),
            availability=availability,
        ))
        dynamic_data_plan = self.dynamic_data_compiler.compile(
            DynamicDataPlanningRequest(
                question=assessment.sanitized_question,
                research_mode=assessment.assessment.intent.mode.value,
                factor_lookback_sessions=assessment.assessment.intent.factor_lookback_sessions,
                label_horizon_sessions=assessment.assessment.intent.label_horizon_sessions,
                # Match the versioned walk-forward policy used by the current
                # Evidence pipeline for the standard research tier.
                train_sessions=100,
                validation_sessions=60,
                # This is a data reservation only. The independent Promotion
                # Controller remains the sole sealed reader.
                sealed_sessions=30,
                buffer_sessions=15,
            )
        )
        dynamic_route = self.dynamic_data_router.route(
            dynamic_data_plan,
            _dynamic_data_capability(data),
        )
        fields = _executable_fields(data) if data else ("close", "volume", "amount", "adj_factor", "market_cap")
        campaign_hash = canonical_content_hash({
            "question": assessment.sanitized_question,
            "assessment": assessment.artifact_hash,
            "dynamic_data_plan": dynamic_data_plan.plan_hash,
            "data": data.artifact_hash if data else "UNAVAILABLE",
        })
        selection = self.selector.select(GroupSelectionContext(
            campaign_hash=campaign_hash,
            mode=assessment.assessment.intent.mode,
            method_models=_methods_for(assessment.assessment.intent.mode),
            question_tags=_question_tags(assessment.sanitized_question),
            available_fields=fields,
            max_groups=6,
            max_candidates_per_group=1,
            context_keys=("question", "data_scope", "visible_factor_summary"),
        ))
        can_run = (
            data is not None
            and assessment.data_planning.official_evaluation_ready
            and dynamic_route.readiness is DataReadiness.READY
            and DEPTHS[depth_label] is not ResearchDepth.QUICK_EXPLORATION
        )
        answer_level = "机器评价" if can_run else ("辅助研究" if data else "概念研究")
        suggestion = {
            "suggestionId": "suggestion-" + campaign_hash.removeprefix("sha256:")[:20],
            "question": assessment.sanitized_question,
            "researchMode": MODE_LABELS.get(assessment.assessment.intent.mode, assessment.assessment.intent.mode.value),
            "researchObject": _research_object(assessment.assessment.intent.factor_lookback_sessions),
            "canRun": can_run,
            "answerLevel": answer_level,
            "rationale": _rationale(can_run, data_failure, assessment.prompt_injection_removed, depth_label),
            "selectedGroups": [GROUP_LABELS.get(item.group_id, item.group_id) for item in selection.assignments],
            "selectedGroupIds": [item.group_id for item in selection.assignments],
            "plannedExperiments": _planned_experiments(assessment.assessment.intent.mode),
            "dataCoverage": _data_coverage(data),
            "dataGaps": _data_gaps(data, data_failure, assessment.assessment.intent.mode)
            + list(assessment.data_planning.official_blockers)
            + list(dynamic_route.missing_requirements),
            "provider": self._provider_view(),
            "dataPlan": _dynamic_data_plan_view(
                dynamic_data_plan,
                dynamic_route,
                data,
            ),
            "campaignHash": campaign_hash,
            "lookbackSessions": assessment.assessment.intent.factor_lookback_sessions,
            "labelHorizonSessions": assessment.assessment.intent.label_horizon_sessions,
            "depth": depth_label,
            "collaborationStyle": collaboration,
        }
        stored = dict(suggestion)
        stored["artifactHash"] = _hash(stored)
        _atomic_json(self.root / "suggestions" / f"{suggestion['suggestionId']}.json", stored)
        return _public_suggestion(suggestion)

    def confirm_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = payload.get("suggestion")
        if not isinstance(raw, Mapping):
            raise ValueError("suggestion is required")
        suggestion_id = _safe_id(str(raw.get("suggestionId") or ""), "suggestion id")
        stored = self._read(self.root / "suggestions" / f"{suggestion_id}.json")
        if _public_suggestion(stored) != _public_suggestion(dict(raw)):
            raise ValueError("suggestion changed after creation")
        depth = str(payload.get("depth") or stored.get("depth") or "标准研究")
        collaboration = str(payload.get("collaborationStyle") or stored.get("collaborationStyle") or "标准式")
        plan_material = {
            "suggestion_hash": stored["artifactHash"],
            "runtime_schema_version": SCHEMA_VERSION,
            "depth": depth,
            "collaboration_style": collaboration,
            "universe": "A 股历史可交易股票池（按日冻结）",
            "horizon": _horizon(stored.get("lookbackSessions"), stored.get("labelHorizonSessions")),
            "budget": {"groups": len(stored.get("selectedGroupIds", ())), "candidates": len(stored.get("selectedGroupIds", ())), "trials": max(3, len(stored.get("selectedGroupIds", ())) * 2)},
            "checkpoints": ["候选短名单", "机器评价完成", "独立封闭评审前"],
            "stop_conditions": ["试验预算耗尽", "没有可安全编译的候选", "用户停止", "验证完成"],
        }
        plan_hash = _hash(plan_material)
        plan_id = "plan-" + plan_hash.removeprefix("sha256:")[:20]
        plan = {
            **_public_suggestion(stored),
            "planId": plan_id,
            "state": "已确认",
            "depth": depth,
            "collaborationStyle": collaboration,
            "universe": plan_material["universe"],
            "horizon": plan_material["horizon"],
            "budget": plan_material["budget"],
            "checkpoints": plan_material["checkpoints"],
            "stopConditions": plan_material["stop_conditions"],
            "planHash": plan_hash,
            "runtimeSchemaVersion": SCHEMA_VERSION,
            "selectedGroupIds": list(stored.get("selectedGroupIds", ())),
            "campaignHash": stored.get("campaignHash"),
            "lookbackSessions": stored.get("lookbackSessions"),
            "labelHorizonSessions": stored.get("labelHorizonSessions"),
        }
        _atomic_json(self.root / "plans" / f"{plan_id}.json", plan)
        return _public_plan(plan)

    def start_research(self, plan_id: str) -> dict[str, Any]:
        plan_id = _safe_id(plan_id, "plan id")
        plan = self._read(self.root / "plans" / f"{plan_id}.json")
        run_id = "run-" + str(plan["planHash"]).removeprefix("sha256:")[:20]
        result_path = self.root / "runs" / run_id / "result.json"
        status_path = self.root / "runs" / run_id / "status.json"
        with self._lock:
            if result_path.exists():
                result = self._read(result_path)
                self._ensure_public_report_projection(run_id, result)
                return result
            future = self._futures.get(run_id)
            if future is None or future.done():
                running = _running_view(run_id, plan)
                _atomic_json(status_path, running)
                self._futures[run_id] = self._executor.submit(self._execute, run_id, plan)
            return self._read(status_path)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run_id = _safe_id(run_id, "run id")
        root = self.root / "runs" / run_id
        result = root / "result.json"
        if result.exists():
            payload = self._read(result)
            self._ensure_public_report_projection(run_id, payload)
            return payload
        return self._read(root / "status.json")

    def _ensure_public_report_projection(
        self, run_id: str, result: Mapping[str, Any]
    ) -> None:
        report_path = self.root / "runs" / run_id / "report.md"
        if not report_path.exists():
            return
        workbench_root = (
            self.root.parent / "workbench_artifacts" / "runs" / run_id
        )
        report_target = workbench_root / "report.json"
        if not report_target.exists():
            _atomic_json(
                report_target,
                {
                    "title": str(
                        result.get("question") or "AlphaForge 自动研究报告"
                    ),
                    "markdown": report_path.read_text(encoding="utf-8"),
                    "report_hash": str(result.get("reportHash") or ""),
                },
            )
        groups = result.get("groups")
        if not isinstance(groups, list):
            return
        lineage_target = workbench_root / "lineage.json"
        lineage = {"items": []}
        if lineage_target.exists():
            try:
                lineage = json.loads(lineage_target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                lineage = {"items": []}
        if not isinstance(lineage, dict) or not lineage.get("items"):
            items = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for candidate in group.get("candidates", ()):
                    if not isinstance(candidate, dict):
                        continue
                    decision = str(candidate.get("decision") or "")
                    items.append(
                        {
                            "candidate_id": str(
                                candidate.get("candidateId") or ""
                            ),
                            "family_id": str(group.get("groupId") or ""),
                            "status": (
                                "VALIDATED_RESEARCH"
                                if decision == "研究验证通过"
                                else "REJECTED"
                            ),
                            "operator": "restricted FactorDAG",
                            "hypothesis": {
                                "statement": str(
                                    candidate.get("hypothesis") or ""
                                ),
                                "falsification_tests": [],
                            },
                            "factor_draft": {
                                "name": str(
                                    candidate.get("title")
                                    or candidate.get("candidateId")
                                    or ""
                                ),
                                "description": str(
                                    candidate.get("decisionExplanation") or ""
                                ),
                                "expression": str(
                                    candidate.get("expression") or ""
                                ),
                            },
                        }
                    )
            _atomic_json(lineage_target, {"items": items})
        evidence_target = workbench_root / "evidence.json"
        candidate_evidence = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for candidate in group.get("candidates", ()):
                if not isinstance(candidate, dict):
                    continue
                candidate_evidence.append(
                    {
                        "candidate_id": str(
                            candidate.get("candidateId") or ""
                        ),
                        "group": str(group.get("title") or ""),
                        "status": str(candidate.get("decision") or ""),
                        "summary_hash": str(
                            candidate.get("evidenceHash") or ""
                        ),
                        "metrics": (
                            candidate.get("metricSummary")
                            if isinstance(
                                candidate.get("metricSummary"), dict
                            )
                            else {}
                        ),
                    }
                )
        if candidate_evidence:
            _atomic_json(
                evidence_target, {"candidates": candidate_evidence}
            )

    def _execute(self, run_id: str, plan: Mapping[str, Any]) -> None:
        run_root = self.root / "runs" / run_id
        try:
            data, failure = self._try_data()
            if data is None:
                result = _blocked_view(run_id, plan, failure or "冻结研究数据不可用")
                _atomic_json(run_root / "result.json", result)
                return
            if str(plan.get("depth")) == "快速探索":
                result = _blocked_view(run_id, plan, "快速探索只产生研究草案，不能启动机器 Evidence")
                _atomic_json(run_root / "result.json", result)
                return
            intent_result = self.planner.assess(QuestionAssessmentRequest(
                question=str(plan["question"]),
                preferred_depth=DEPTHS[str(plan["depth"])],
                decision_time=data.snapshot.decision_time,
                availability=_availability(data),
            ))
            selection = self.selector.select(GroupSelectionContext(
                campaign_hash=str(plan["campaignHash"]),
                mode=intent_result.assessment.intent.mode,
                method_models=_methods_for(intent_result.assessment.intent.mode),
                question_tags=_question_tags(str(plan["question"])),
                available_fields=_executable_fields(data),
                max_groups=min(6, int(plan["budget"]["groups"])),
                max_candidates_per_group=1,
                context_keys=("question", "data_scope", "visible_factor_summary"),
            ))
            provider, provider_view = self._proposal_provider(selection, data)
            runtime = _runtime(provider).run(
                selection=selection,
                intent=intent_result.assessment.intent,
                campaign_hash=str(plan["campaignHash"]),
            )
            if (
                isinstance(provider, SelectedGroupRealLLMProposalProvider)
                or bool(getattr(provider, "deterministic_baseline_enabled", False))
            ):
                runtime = _append_deterministic_baseline(
                    runtime,
                    intent=intent_result.assessment.intent,
                    campaign_hash=str(plan["campaignHash"]),
                    available_fields=_executable_fields(data),
                )
            runtime_diagnostics = _runtime_diagnostics(runtime, provider)
            _atomic_json(run_root / "group_runtime.json", runtime_diagnostics)
            if not any(
                branch.status is GroupBranchStatus.COMPLETED
                and branch.proposal is not None
                and branch.submission is not None
                for branch in runtime.branches
            ):
                report_path, report_hash = self._write_no_candidate_report(
                    run_id,
                    plan,
                    data,
                    runtime,
                    provider_view,
                    runtime_diagnostics,
                )
                result = _completed_without_candidates_view(
                    run_id,
                    plan,
                    runtime,
                    provider_view,
                    runtime_diagnostics,
                    report_path,
                    report_hash,
                )
                _atomic_json(run_root / "result.json", result)
                self._publish_no_candidate_workbench(
                    run_id,
                    plan,
                    data,
                    runtime_diagnostics,
                    provider_view,
                    report_hash,
                )
                return
            walk_forward = _walk_forward_spec(
                data,
                intent_result.assessment.intent.factor_lookback_sessions,
            )
            validation = ResearchValidationPipelineV2(
                ledger_path=run_root / "trial_ledger.sqlite3",
                walk_forward=walk_forward,
                cost_model=CostModel(spread_bps=5.0, linear_impact_bps=10.0),
            ).run(ResearchValidationPipelineRequest(
                campaign_hash=str(plan["campaignHash"]),
                runtime=runtime,
                frozen_data=data,
                requested_lookback_sessions=intent_result.assessment.intent.factor_lookback_sessions,
                family_budget_per_candidate=1,
                seed=7,
            ))
            report_path, report_hash = self._write_report(
                run_id,
                plan,
                data,
                runtime,
                validation,
                provider_view,
                walk_forward,
            )
            result = _completed_view(run_id, plan, runtime, validation, provider_view, report_path, report_hash)
            result["evaluationPolicy"] = {
                "trainPeriods": walk_forward.train_periods,
                "validationPeriods": walk_forward.validation_periods,
                "sealedPeriods": walk_forward.sealed_periods,
                "stepPeriods": walk_forward.step_periods,
            }
            result["candidateSelection"] = _candidate_selection_view(validation)
            _atomic_json(run_root / "result.json", result)
            self._publish_workbench(
                run_id, plan, data, runtime, validation, provider_view, report_hash
            )
        except Exception as exc:
            code = exc.code.value if isinstance(exc, ContractValidationError) else type(exc).__name__
            result = _failed_view(run_id, plan, str(code))
            _atomic_json(run_root / "result.json", result)

    def _write_no_candidate_report(
        self,
        run_id,
        plan,
        data,
        runtime,
        provider_view,
        runtime_diagnostics,
    ):
        lines = [
            f"# AlphaForge 自动研究报告：{plan['question']}",
            "",
            f"- 运行 ID：`{run_id}`",
            f"- 运行状态：`COMPLETED_WITH_REVISIONS`",
            f"- 研究结论：`REVISE`",
            f"- 数据集：`{data.snapshot.dataset_id}`",
            f"- 数据来源：`{data.source_label}`",
            f"- 冻结输入哈希：`{data.artifact_hash}`",
            f"- 数据区间：{data.snapshot.start_time.isoformat()} 至 {data.snapshot.decision_time.isoformat()}",
            f"- 提案来源：{provider_view['mode']}",
            "",
            "## 一页结论",
            "",
            "本次所选专业研究小组均已实际运行，但没有草案通过受限候选转换，"
            "因此没有启动 PIT、机器 Evidence 或独立封闭评审。"
            "这是一项可追溯的研究修订结论，不是系统运行崩溃。",
            "",
            "## 专业研究小组结果",
            "",
        ]
        attempt_by_group = {
            item["groupId"]: item for item in runtime_diagnostics.get("providerAttempts", ())
        }
        for branch in sorted(runtime.branches, key=lambda item: item.group_id):
            attempt = attempt_by_group.get(branch.group_id, {})
            lines.extend([
                f"### {GROUP_LABELS.get(branch.group_id, branch.group_id)}",
                "",
                f"- 分支状态：`{branch.status.value}`",
                f"- 失败代码：`{attempt.get('failureCode') or branch.failure_code or 'UNKNOWN'}`",
                f"- 模型调用：{attempt.get('providerCalls', 0)}",
                f"- 自动修订：{attempt.get('revisionsUsed', 0)}",
                "- 机器评价：未启动",
                "- 建议：按照冻结字段和受限算子清单修订候选后重新提交。",
                "",
            ])
        lines.extend([
            "## 权限与限制",
            "",
            "- 研究模型只产生 DRAFT，不拥有官方指标或晋级权限。",
            "- 未通过安全候选转换的草案不会进入 FactorDAG、PIT 或 Evidence。",
            "- 独立封闭评审未满足启动条件；没有封闭信息进入研究上下文。",
            "- 本报告不构成投资建议。",
        ])
        content = "\n".join(lines) + "\n"
        report_path = self.root / "runs" / run_id / "report.md"
        _atomic_text(report_path, content)
        return (
            f"/reports/{run_id}",
            "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def _publish_no_candidate_workbench(
        self,
        run_id,
        plan,
        data,
        runtime_diagnostics,
        provider_view,
        report_hash,
    ):
        root = self.root.parent / "workbench_artifacts"
        run_root = root / "runs" / run_id
        overview = {
            "run_id": run_id,
            "title": str(plan["question"]),
            "status": "REVISE",
            "decision_time": data.snapshot.decision_time.isoformat(),
            "report_hash": report_hash,
            "provider": provider_view,
            "research_dag": {
                "nodes": [
                    {
                        "id": item["groupId"],
                        "role": "专业研究小组",
                        "status": "BLOCKED",
                        "summary": item["failureCode"],
                    }
                    for item in runtime_diagnostics["branches"]
                ],
            },
        }
        _atomic_json(run_root / "overview.json", overview)
        _atomic_json(
            run_root / "report.json",
            {
                "title": str(plan["question"]),
                "markdown": (
                    self.root / "runs" / run_id / "report.md"
                ).read_text(encoding="utf-8"),
                "report_hash": report_hash,
            },
        )
        _atomic_json(run_root / "evidence.json", {"candidates": []})
        _atomic_json(run_root / "lineage.json", {"items": []})
        _atomic_json(
            run_root / "critics.json",
            {
                "items": [
                    {
                        "group_id": item["groupId"],
                        "reason_codes": [item["failureCode"]],
                    }
                    for item in runtime_diagnostics["branches"]
                ]
            },
        )
        _atomic_json(
            run_root / "promotion.json",
            {"status": "NOT_REQUESTED", "sealed_status": "NOT_STARTED"},
        )
        for name in ("citations", "shadow_portfolio", "nav_attribution", "drift_alerts"):
            _atomic_json(run_root / f"{name}.json", {"items": []})
        _upsert_workbench_index(root, overview)

    def _proposal_provider(self, selection, data: FrozenValidationData):
        transport = AlphaForgeRealLLMTransport.from_dotenv(self.dotenv_path)
        if transport.available:
            policies = tuple(GroupExecutionPolicy(
                group_id=context.group_id,
                allowed_fields=tuple(field for field in data.snapshot.fields if "future_return" not in field.lower()),
                tool_whitelist=context.tool_whitelist,
                max_tokens=4096,
                max_iterations=2,
                timeout_seconds=60.0,
                candidate_budget=1,
                max_revisions=1,
            ) for context in selection.isolated_contexts)
            return (
                SelectedGroupRealLLMProposalProvider(
                    transport=transport,
                    selected_group_ids=tuple(item.group_id for item in selection.assignments),
                    policies=policies,
                    fallback_on_unavailable=True,
                ),
                {"mode": "真实模型", "providerLabel": "已配置的受限研究模型", "modelLabel": str(transport.public_view.get("model") or "已配置模型")},
            )
        return None, {"mode": "确定性回退", "providerLabel": "离线可复现研究建议", "reason": "真实模型未启用或配置不可用"}

    def _provider_view(self) -> dict[str, Any]:
        try:
            transport = AlphaForgeRealLLMTransport.from_dotenv(self.dotenv_path)
        except Exception:
            transport = None
        if transport is not None and transport.available:
            return {"mode": "真实模型", "providerLabel": "受限研究模型已就绪", "modelLabel": str(transport.public_view.get("model") or "已配置模型")}
        return {"mode": "确定性回退", "providerLabel": "离线可复现研究建议", "reason": "真实模型不可用时仍可完成确定性研究"}

    def _try_data(self):
        try:
            data = self.data_loader()
            if (
                data is not None
                and self.dynamic_freeze_root is not None
                and (self.dynamic_freeze_root / "freeze_manifest.json").exists()
            ):
                manifest_path = self.dynamic_freeze_root / "freeze_manifest.json"
                stat = manifest_path.stat()
                manifest_claim = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                ).get("artifact_hash")
                if (
                    isinstance(manifest_claim, str)
                    and data.snapshot.dataset_id.endswith(
                        ".dynamic." + manifest_claim[7:19]
                    )
                ):
                    return data, None
                cache_key = (
                    data.artifact_hash,
                    stat.st_mtime_ns,
                    stat.st_size,
                )
                if self._cached_data_key != cache_key:
                    self._cached_data = enrich_frozen_validation_data(
                        data,
                        freeze_root=self.dynamic_freeze_root,
                    )
                    self._cached_data_key = cache_key
                data = self._cached_data
            return data, None
        except Exception as exc:
            return None, f"冻结数据校验失败：{type(exc).__name__}"

    def _write_report(
        self,
        run_id,
        plan,
        data,
        runtime,
        validation,
        provider_view,
        walk_forward,
    ):
        lines = [
            f"# AlphaForge 自动研究报告：{plan['question']}", "",
            f"- 运行 ID：`{run_id}`",
            f"- 研究模式：{plan['researchMode']}",
            f"- 数据集：`{data.snapshot.dataset_id}`",
            f"- 数据来源：`{data.source_label}`",
            f"- 冻结输入哈希：`{data.artifact_hash}`",
            f"- 数据区间：{data.snapshot.start_time.isoformat()} 至 {data.snapshot.decision_time.isoformat()}",
            f"- 股票数：{len({row.symbol for row in data.snapshot.rows})}",
            f"- 交易时点数：{len(data.snapshot.historical_universe)}",
            (
                "- Walk-forward："
                f"train={walk_forward.train_periods}, "
                f"validation={walk_forward.validation_periods}, "
                f"sealed={walk_forward.sealed_periods}, "
                f"step={walk_forward.step_periods}"
            ),
            f"- 提案来源：{provider_view['mode']}", "",
            "## 一页结论", "",
        ]
        terminal = validation.terminal_records
        validated = [item for item in terminal if item.disposition is ValidationDisposition.VALIDATED_RESEARCH]
        if validated:
            lines.append(f"{len(validated)} 个候选通过可见研究验证；是否成为挑战者仍由独立封闭评审决定。")
        else:
            lines.append("本轮没有候选通过可见研究验证。流程仍视为正确完成，结果为拒绝或需要修订。")
        selection_view = _candidate_selection_view(validation)
        if selection_view["bestObserved"] is not None:
            best = selection_view["bestObserved"]
            lines.extend([
                "",
                "## 可见证据候选比较",
                "",
                f"- 比较状态：`{selection_view['status']}`",
                f"- 最佳可见研究线索所属：`{best['groupId']}`",
                f"- 复合分数：{best['compositeScore']:.6f}",
                "- 权限：仅用于可见研究比较，不代表独立晋级或挑战者。",
            ])
            if selection_view["duplicateFactorDAGs"]:
                lines.append(
                    "- 重复FactorDAG：不同小组提交了相同的安全执行图；"
                    "报告保留来源，但不会把重复表达解释为独立发现。"
                )
        lines.extend(["", "## 专业研究小组与候选", ""])
        proposals = {branch.group_id: branch.proposal for branch in runtime.branches if branch.proposal is not None}
        for record in terminal:
            proposal = proposals.get(record.group_id)
            candidate = next((item for item in proposal.candidates if item.candidate_id == record.candidate_id), None) if proposal else None
            lines.extend([
                f"### {GROUP_LABELS.get(record.group_id, record.group_id)} · {record.candidate_id}", "",
                f"- 假设：{proposal.hypothesis if proposal else '未形成可用假设'}",
                f"- FactorDAG：`{candidate.expression if candidate else '不可用'}`",
                f"- 安全转换：{record.translation.status.value}",
                f"- 机器结论：{_decision_label(record.disposition)}",
                f"- 失败/评审代码：{', '.join(record.failure_codes) if record.failure_codes else '无'}",
            ])
            metrics = record.validation.visible_metrics.metrics if record.validation.visible_metrics else {}
            if metrics:
                lines.extend([
                    f"- ICIR：{_metric(metrics, 'validation_icir')}",
                    f"- RankIC：{_metric(metrics, 'validation_rank_ic_mean')}",
                    f"- 成本后收益：{_metric(metrics, 'validation_net_return')}",
                    f"- 换手：{_metric(metrics, 'validation_turnover')}",
                    f"- 稳定性：{_metric(metrics, 'validation_subperiod_positive_fraction')}",
                    f"- 增量 IC：{_metric(metrics, 'validation_incremental_ic_mean')}",
                ])
            lines.append("")
        failed_branches = [
            branch for branch in runtime.branches
            if branch.status is GroupBranchStatus.FAILED
        ]
        if failed_branches:
            lines.extend(["## 未进入机器评价的研究分支", ""])
            for branch in sorted(failed_branches, key=lambda item: item.group_id):
                lines.extend([
                    f"### {GROUP_LABELS.get(branch.group_id, branch.group_id)}",
                    "",
                    f"- 分支状态：`{branch.status.value}`",
                    f"- 失败代码：`{branch.failure_code or 'UNKNOWN'}`",
                    "- 处理：该分支没有伪造候选或机器指标；其他独立分支继续运行。",
                    "",
                ])
        lines.extend([
            "## 权限与限制", "",
            "- 研究模型只提交 DRAFT，不写机器指标和晋级结论。",
            "- 本报告不显示封闭样本内容或私有排名。",
            "- 研究验证通过不等于挑战者，不构成投资建议。",
        ])
        content = "\n".join(lines) + "\n"
        report_path = self.root / "runs" / run_id / "report.md"
        _atomic_text(report_path, content)
        return f"/reports/{run_id}", "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _publish_workbench(
        self, run_id, plan, data, runtime, validation, provider_view, report_hash
    ):
        root = self.root.parent / "workbench_artifacts"
        run_root = root / "runs" / run_id
        records = validation.terminal_records
        records_by_group: dict[str, list[Any]] = {}
        for record in records:
            records_by_group.setdefault(record.group_id, []).append(record)
        status = "VALIDATED_RESEARCH" if any(item.disposition is ValidationDisposition.VALIDATED_RESEARCH for item in records) else "REJECTED"
        overview = {
            "run_id": run_id, "title": str(plan["question"]), "status": status,
            "decision_time": data.snapshot.decision_time.isoformat(), "report_hash": report_hash,
            "provider": provider_view,
            "research_dag": {
                "nodes": [
                    {
                        "id": branch.group_id,
                        "role": "专业研究小组",
                        "status": (
                            "BLOCKED"
                            if branch.status is GroupBranchStatus.FAILED
                            else "VALIDATED_RESEARCH"
                            if any(
                                item.disposition
                                is ValidationDisposition.VALIDATED_RESEARCH
                                for item in records_by_group.get(
                                    branch.group_id, ()
                                )
                            )
                            else "REJECTED"
                        ),
                        "summary": (
                            branch.failure_code
                            or "已完成安全转换、PIT 与可见机器评价"
                        ),
                    }
                    for branch in sorted(
                        runtime.branches, key=lambda item: item.group_id
                    )
                ],
            },
        }
        evidence = {"candidates": [{
            "candidate_id": item.candidate_id,
            "group": GROUP_LABELS.get(item.group_id, item.group_id),
            "status": item.disposition.value,
            "evidence_hashes": list(item.validation.result_ref.evidence_hashes),
            "summary_hash": item.validation.result_ref.summary_hash,
            "metrics": (
                dict(item.validation.visible_metrics.metrics)
                if item.validation.visible_metrics
                else {}
            ),
        } for item in records]}
        record_by_candidate = {item.candidate_id: item for item in records}
        lineage_items = []
        for branch in sorted(runtime.branches, key=lambda item: item.group_id):
            proposal = branch.proposal
            if proposal is None:
                continue
            for candidate in proposal.candidates:
                record = record_by_candidate.get(candidate.candidate_id)
                lineage_items.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "family_id": branch.group_id,
                        "status": (
                            record.disposition.value if record is not None else "DRAFT"
                        ),
                        "operator": "restricted FactorDAG",
                        "hypothesis": {
                            "statement": proposal.hypothesis,
                            "economic_mechanism": proposal.economic_mechanism,
                            "falsification_tests": list(
                                proposal.falsification_tests
                            ),
                        },
                        "factor_draft": {
                            "name": candidate.candidate_id,
                            "description": candidate.rationale,
                            "expression": candidate.expression,
                        },
                        "factor_dag_hash": (
                            record.translation.factor_dag_hash
                            if record is not None
                            and record.translation.factor_dag_hash is not None
                            else None
                        ),
                    }
                )
        _atomic_json(run_root / "overview.json", overview)
        _atomic_json(
            run_root / "report.json",
            {
                "title": str(plan["question"]),
                "markdown": (
                    self.root / "runs" / run_id / "report.md"
                ).read_text(encoding="utf-8"),
                "report_hash": report_hash,
            },
        )
        _atomic_json(run_root / "evidence.json", evidence)
        _atomic_json(run_root / "lineage.json", {"items": lineage_items})
        critic_items = [
            {
                "candidate_id": item.candidate_id,
                "reason_codes": list(item.failure_codes),
            }
            for item in records
        ]
        critic_items.extend({
            "group_id": branch.group_id,
            "reason_codes": [branch.failure_code or "UNKNOWN"],
        } for branch in runtime.branches if branch.status is GroupBranchStatus.FAILED)
        _atomic_json(run_root / "critics.json", {"items": critic_items})
        _atomic_json(run_root / "promotion.json", {"status": "NOT_REQUESTED", "sealed_status": "OPAQUE"})
        for name in ("citations", "shadow_portfolio", "nav_attribution", "drift_alerts"):
            _atomic_json(run_root / f"{name}.json", {"items": []})
        index_path = root / "index.json"
        current = {"runs": []}
        if index_path.exists():
            try:
                current = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                current = {"runs": []}
        runs = [item for item in current.get("runs", []) if item.get("run_id") != run_id]
        runs.append(overview)
        _atomic_json(index_path, {"runs": sorted(runs, key=lambda item: item["run_id"])})

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path.name)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("stored artifact must be an object")
        return value


def _runtime(provider):
    if provider is None:
        return ResearchGroupProposalRuntime()
    try:
        from ..concurrent_research_group_runtime_v2 import ConcurrentResearchGroupRuntimeV2
    except ImportError:
        return ResearchGroupProposalRuntime(provider=provider)
    return _ConcurrentRuntimeAdapter(
        ConcurrentResearchGroupRuntimeV2(
            provider=provider,
            max_workers=6,
            branch_timeout_seconds=65.0,
        )
    )


class _ConcurrentRuntimeAdapter:
    """Project proposal-only concurrent results into the existing validation input."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def run(self, **kwargs) -> GroupRuntimeResult:
        result = self.runtime.run(**kwargs)
        branches = []
        for item in result.branches:
            if item.status.value == "COMPLETED":
                branches.append(GroupRuntimeBranchResult(
                    group_id=item.group_id,
                    status=GroupBranchStatus.COMPLETED,
                    proposal=item.proposal,
                    submission=item.submission,
                ))
            else:
                branches.append(GroupRuntimeBranchResult(
                    group_id=item.group_id,
                    status=GroupBranchStatus.FAILED,
                    proposal=None,
                    submission=None,
                    failure_code=item.failure_code.value,
                    message=item.failure_type,
                ))
        return GroupRuntimeResult(branches=tuple(branches))


def _availability(data: FrozenValidationData) -> DataAvailabilitySnapshot:
    return DataAvailabilitySnapshot(
        availability_id="availability." + data.snapshot.dataset_id,
        available_fields=_executable_fields(data),
        available_sessions=len(data.snapshot.historical_universe),
        available_symbols=len({row.symbol for row in data.snapshot.rows}),
        provider_label=data.source_label,
        snapshot_hash=data.snapshot.artifact_hash,
    )


def _walk_forward_spec(
    data: FrozenValidationData,
    requested_lookback_sessions: int | None,
) -> WalkForwardSpec:
    """Choose a declared, reproducible split that accounts for factor warmup."""

    sessions = len(data.snapshot.historical_universe)
    warmup = max(0, int(requested_lookback_sessions or 0))
    usable = max(0, sessions - warmup)
    if usable >= 240:
        return WalkForwardSpec(
            train_periods=120,
            validation_periods=80,
            sealed_periods=40,
            step_periods=20,
        )
    if usable >= 190:
        return WalkForwardSpec(
            train_periods=100,
            validation_periods=60,
            sealed_periods=30,
            step_periods=20,
        )
    if usable >= 120:
        return WalkForwardSpec(
            train_periods=60,
            validation_periods=40,
            sealed_periods=20,
            step_periods=10,
        )
    # Keep the minimum policy explicit. The Evidence Service will fail closed
    # rather than silently manufacture a shorter research evaluation.
    return WalkForwardSpec(
        train_periods=60,
        validation_periods=40,
        sealed_periods=20,
        step_periods=10,
    )


def _methods_for(mode: ResearchMode) -> tuple[MethodModel, ...]:
    mapping = {
        ResearchMode.NEW_ALPHA_EXPLORATION: (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.DATA_ANOMALY, MethodModel.ADVERSARIAL_FALSIFICATION),
        ResearchMode.VALIDATE_IDEA: (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        ResearchMode.IMPROVE_EXISTING_FACTOR: (MethodModel.BOUNDED_EVOLUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
        ResearchMode.PORTFOLIO_GAP_SEARCH: (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.ADVERSARIAL_FALSIFICATION),
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING),
        ResearchMode.EVENT_MARKET_TOPIC: (MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING),
    }
    return mapping.get(mode, (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION))


def _question_tags(question: str) -> tuple[str, ...]:
    tags = []
    mapping = {
        "momentum": ("动量", "趋势", "momentum"), "reversal": ("反转", "reversal"),
        "volume": ("成交量", "成交额", "volume"), "fundamental": ("基本面", "盈利", "估值"),
        "event": ("事件", "财报", "公告"), "diagnosis": ("失效", "回撤", "诊断"),
        "incrementality": ("低相关", "增量", "组合缺口"),
    }
    lowered = question.lower()
    for tag, tokens in mapping.items():
        if any(token.lower() in lowered for token in tokens):
            tags.append(tag)
    return tuple(tags or ("alpha",))


def _public_suggestion(value: Mapping[str, Any]) -> dict[str, Any]:
    required_keys = (
        "suggestionId",
        "question",
        "researchMode",
        "researchObject",
        "canRun",
        "answerLevel",
        "rationale",
        "selectedGroups",
        "plannedExperiments",
        "dataCoverage",
        "dataGaps",
        "provider",
    )
    public = {key: value[key] for key in required_keys}
    for key in ("dataPlan", "lookbackSessions", "labelHorizonSessions"):
        if key in value:
            public[key] = value[key]
    return public


def _public_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**_public_suggestion(value), **{key: value[key] for key in ("planId", "state", "depth", "collaborationStyle", "universe", "horizon", "budget", "checkpoints", "stopConditions", "planHash")}}


def _research_object(lookback: Any) -> str:
    return f"A 股 {int(lookback or 20)} 日横截面 Alpha"


def _horizon(lookback: Any, label: Any) -> str:
    return f"{int(lookback or 20)} 日观察窗 / {int(label or 5)} 日预测期"


def _rationale(can_run: bool, failure: str | None, injection_removed: bool, depth: str) -> str:
    if depth == "快速探索":
        return "快速探索只形成研究草案和数据需求，不启动机器评价。"
    if can_run:
        suffix = " 已移除问题中的越权指令。" if injection_removed else ""
        return "冻结数据满足当前研究问题的核心字段，可进入受控机器评价。" + suffix
    return failure or "当前数据不足以形成机器指标；仍会返回概念分析和数据补充建议。"


def _planned_experiments(mode: ResearchMode) -> list[str]:
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        return ["分解表现变化与风格暴露", "检查成本、容量与市场状态", "执行反证和根因排序"]
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return ["检验与已登记因子的相关性", "计算增量性和成本后表现", "比较稳定性与容量"]
    return ["比较原始候选与稳健变体", "检验成本后表现和子时期稳定性", "检验相关性与增量性"]


def _data_coverage(data: FrozenValidationData | None) -> list[dict[str, Any]]:
    if data is None:
        return [{"field": "冻结 A 股研究数据", "availability": "不可用", "coverageLabel": "尚未加载", "sourceLabel": "未配置"}]
    fields = set(data.snapshot.fields)
    executable_fields = set(_executable_fields(data))
    groups = [
        ("价格与复权", {"close", "adj_factor"}), ("成交量与成交额", {"volume", "amount"}),
        ("历史股票池与交易日历", set()), ("行业与市值", {"industry", "market_cap"}),
        ("财务报表与质量指标", {"revenue", "roe"}),
        ("资金流", {"net_moneyflow_amount"}),
        ("宏观与市场状态", {"shibor_overnight", "index_close"}),
    ]
    result = []
    for label, required in groups:
        if not required or required <= executable_fields:
            availability = "可用"
        elif required & fields:
            availability = "部分可用"
        else:
            availability = "不可用"
        result.append({
            "field": label, "availability": availability,
            "coverageLabel": f"{len(data.snapshot.historical_universe)} 个交易时点",
            "sourceLabel": data.source_label,
            **({
                "limitation": "缺失或低覆盖字段只作描述性数据，不会伪装成全股票池可执行输入"
            } if availability != "可用" else {}),
        })
    return result


def _data_gaps(data: FrozenValidationData | None, failure: str | None, mode: ResearchMode) -> list[str]:
    gaps = []
    if data is None:
        gaps.append(failure or "缺少通过资格检查的冻结数据")
    elif "industry" not in data.snapshot.fields:
        gaps.append("当前快照没有 PIT-safe 历史行业；纯价格研究可运行，行业中性研究需补充数据。")
    if mode is ResearchMode.EVENT_MARKET_TOPIC and data is not None and "event_time" not in data.snapshot.fields:
        gaps.append("事件发布时间字段未冻结，只能给出辅助研究建议。")
    return gaps


def _dynamic_data_capability(
    data: FrozenValidationData | None,
) -> DataCapabilitySnapshot:
    """Project a frozen snapshot into a secret-free capability declaration.

    Upstream APIs are intentionally not inferred from a field name.  A
    normalized frozen dataset is declared ready by dataset id, while an absent
    dataset remains a visible acquisition requirement.
    """

    if data is None:
        return DataCapabilitySnapshot()
    raw_fields = set(_executable_fields(data))
    fields = set(raw_fields)
    # The dynamic catalog describes provider fields while the frozen research
    # snapshot uses normalized names.  These aliases only describe capability;
    # they never mutate evidence inputs.
    if "volume" in raw_fields:
        fields.add("vol")
    if "symbol" in raw_fields or data.snapshot.rows:
        fields.add("ts_code")
    if data.snapshot.rows:
        fields.add("trade_date")
    datasets = set()
    if {"close", "vol"} <= fields:
        datasets.add("market_daily")
    if data.snapshot.historical_universe:
        datasets.add("historical_universe")
        fields.update(("list_date", "delist_date"))
    if {"market_cap", "turnover_rate"} & raw_fields:
        datasets.add("market_valuation")
        fields.update(("total_mv", "turnover_rate"))
    if raw_fields & {
        "revenue",
        "net_income",
        "roe",
        "gross_margin",
        "operating_cashflow",
    }:
        datasets.add("fundamentals")
        fields.update(("end_date", "ann_date", "f_ann_date"))
    if raw_fields & {
        "forecast_profit_change_min",
        "forecast_profit_change_max",
        "express_net_income",
    }:
        datasets.add("corporate_events")
        fields.update(("end_date", "ann_date"))
    if raw_fields & {"net_moneyflow_amount", "buy_large_amount"}:
        datasets.add("money_flow")
    if raw_fields & {"shibor_overnight", "shibor_1w", "index_return"}:
        datasets.add("macro_regime")
        fields.add("date")
    if raw_fields & {
        "registered_factor_summary",
        "factor_library_summary",
        "public_validation_summary",
    }:
        datasets.add("registered_research")
    return DataCapabilitySnapshot(
        available_datasets=tuple(sorted(datasets)),
        available_api_names=(),
        available_fields=tuple(sorted(fields)),
        available_sessions=len(data.snapshot.historical_universe),
        provider_label=data.source_label,
        frozen_snapshot=True,
    )


def _executable_fields(
    data: FrozenValidationData,
    *,
    minimum_coverage: float = 0.5,
) -> tuple[str, ...]:
    """Fields below the declared cross-sectional coverage stay descriptive.

    This is a proportion, not a hard-coded stock/day count. Sparse real-smoke
    fields remain frozen and reportable but cannot make a whole-universe
    research branch look executable.
    """

    return tuple(
        field
        for field in data.snapshot.fields
        if float(data.snapshot.coverage_by_field.get(field, 0.0))
        >= minimum_coverage
    )


def _dynamic_data_plan_view(
    plan,
    route,
    data: FrozenValidationData | None,
) -> dict[str, Any]:
    available_sessions = (
        len(data.snapshot.historical_universe) if data is not None else 0
    )
    minimum_sessions = plan.history.minimum_total_sessions
    requested_apis = list(plan.requested_api_names)
    requested_fields = list(plan.requested_fields)
    missing_requirements = list(route.missing_requirements)
    for item in route.dataset_readiness:
        if item.readiness is DataReadiness.READY:
            continue
        details = tuple(item.missing_fields) + tuple(item.missing_api_names)
        missing_requirements.append(
            item.dataset_id
            + (":" + ",".join(details) if details else "")
        )
    return {
        "planHash": plan.plan_hash,
        "lookbackSessions": plan.history.warmup_sessions,
        "requiredApis": requested_apis,
        "requiredFields": requested_fields,
        # ``requested*`` aliases keep the API explicit for acquisition clients;
        # ``required*`` is retained for the product UI.
        "requestedApis": requested_apis,
        "requestedFields": requested_fields,
        "minimumSessions": minimum_sessions,
        "availableSessions": available_sessions,
        "currentSessions": available_sessions,
        "sourceLabel": data.source_label if data is not None else "UNAVAILABLE",
        "universe": {
            "kind": plan.universe.kind.value,
            "symbolCount": (
                len({row.symbol for row in data.snapshot.rows})
                if data is not None
                else None
            ),
            "indexCode": plan.universe.index_code,
        },
        "acquisitionRequired": (
            route.readiness is not DataReadiness.READY
            or available_sessions < minimum_sessions
        ),
        "readiness": route.readiness.value,
        "missingRequirements": list(dict.fromkeys(missing_requirements)),
        "degradationSuggestions": [
            _localize_data_message(item)
            for item in route.degradation_suggestions
        ],
        "researchableParts": [
            _localize_data_message(item)
            for item in route.researchable_parts
        ],
        "selectedDataCapableGroups": list(route.selected_group_ids),
        "groupDecisions": [
            {
                "groupId": item.group_id,
                "selected": item.selected,
                "reason": item.reason,
                "missingDatasets": list(item.missing_datasets),
                "missingFields": list(item.missing_fields),
            }
            for item in route.group_decisions
        ],
    }


def _localize_data_message(value: str) -> str:
    replacements = {
        ": declared fields/APIs support bounded research":
            "：已冻结必要字段，可进行有界研究",
        ": partial descriptive research only":
            "：仅可用于描述性或来源型研究",
        "Proceed only with the listed researchable datasets and disclose omitted mechanisms.":
            "仅使用上方明确可研究的数据，并在报告中披露未覆盖的机制。",
        "Use partial datasets for descriptive or sourced analysis, not official evidence.":
            "部分数据只用于描述性或来源型分析，不能冒充官方机器证据。",
        "Acquire and freeze missing datasets with provenance and PIT clocks before evaluating those hypotheses.":
            "评价相关假设前，先补充获取缺失数据并冻结来源与 PIT 时间。",
        "Acquire more history or explicitly reduce protocol budgets; never silently shorten the requested scope.":
            "补充获取历史数据；如需调整研究协议必须显式确认，不能静默缩短范围。",
        "Return a conceptual data-needs answer only; do not fabricate observations, metrics, or conclusions.":
            "当前只返回概念分析和数据需求，不虚构观测、指标或结论。",
    }
    if value in replacements:
        return replacements[value]
    for source, target in replacements.items():
        if source.startswith(": ") and value.endswith(source):
            return value[: -len(source)] + target
    return value


def _running_view(run_id: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "runId": run_id, "planId": plan["planId"], "state": "运行中",
        "runStateCode": "RUNNING", "researchOutcomeCode": "NOT_EVALUATED",
        "provider": plan["provider"],
        "groups": [], "trialsUsed": 0, "trialLimit": plan["budget"]["trials"],
        "currentCheckpoint": "正在组织专业研究小组并准备机器评价",
        "sealedReview": "未申请", "sealedMessage": "封闭信息不会进入研究上下文。", "limitations": [],
    }


def _blocked_view(run_id: str, plan: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        **_running_view(run_id, plan), "state": "已阻断", "provider": plan["provider"],
        "runStateCode": "BLOCKED_BY_DATA",
        "currentCheckpoint": reason, "sealedReview": "不可用", "sealedMessage": "未启动独立封闭评审。",
        "limitations": [reason],
    }


def _failed_view(run_id: str, plan: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {
        **_running_view(run_id, plan), "state": "失败", "currentCheckpoint": "研究运行失败并已关闭",
        "runStateCode": "FAILED_SYSTEM",
        "sealedReview": "不可用", "sealedMessage": "未启动独立封闭评审。", "limitations": [f"失败代码：{code}"],
    }


def _completed_without_candidates_view(
    run_id,
    plan,
    runtime,
    provider_view,
    diagnostics,
    report_path,
    report_hash,
):
    attempts = {
        item["groupId"]: item for item in diagnostics.get("providerAttempts", ())
    }
    groups = []
    for branch in sorted(runtime.branches, key=lambda item: item.group_id):
        attempt = attempts.get(branch.group_id, {})
        failure_code = attempt.get("failureCode") or branch.failure_code or "UNKNOWN"
        groups.append({
            "groupId": branch.group_id,
            "title": GROUP_LABELS.get(branch.group_id, branch.group_id),
            "purpose": "从本组专业视角提出可证伪候选，并接受同一机器证据标准。",
            "state": "需修订",
            "budgetUsed": 1,
            "budgetLimit": 1,
            "progress": ["提出研究草案", "安全转换未通过", "机器评价未启动"],
            "candidates": [],
            "failure": failure_code,
            "failureType": branch.message,
            "providerCalls": attempt.get("providerCalls", 0),
            "revisionsUsed": attempt.get("revisionsUsed", 0),
        })
    return {
        "runId": run_id,
        "planId": plan["planId"],
        "state": "已完成",
        "runStateCode": "COMPLETED_WITH_REVISIONS",
        "researchOutcomeCode": "REVISE",
        "provider": provider_view,
        "groups": groups,
        "trialsUsed": 0,
        "trialLimit": plan["budget"]["trials"],
        "proposalAttemptsUsed": len(groups),
        "modelCallsUsed": sum(
            int(item.get("providerCalls", 0))
            for item in diagnostics.get("providerAttempts", ())
        ),
        "currentCheckpoint": "研究草案均需修订；未进入机器评价",
        "sealedReview": "未申请",
        "sealedMessage": "没有候选进入可见机器评价，未满足独立封闭评审启动条件。",
        "formalReportPath": report_path,
        "reportHash": report_hash,
        "limitations": sorted({
            str(group["failure"]) for group in groups
        }),
    }


def _completed_view(run_id, plan, runtime, validation, provider_view, report_path, report_hash):
    records_by_group: dict[str, list[Any]] = {}
    for record in validation.terminal_records:
        records_by_group.setdefault(record.group_id, []).append(record)
    branches = {branch.group_id: branch for branch in runtime.branches}
    groups = []
    for group_id in sorted(branches):
        branch = branches[group_id]
        records = records_by_group.get(group_id, [])
        candidates = []
        proposal = branch.proposal
        for record in records:
            candidate = next((item for item in proposal.candidates if item.candidate_id == record.candidate_id), None) if proposal else None
            metrics = record.validation.visible_metrics.metrics if record.validation.visible_metrics else None
            candidates.append({
                "candidateId": record.candidate_id, "title": record.candidate_id,
                "hypothesis": proposal.hypothesis if proposal else "未形成假设",
                "expression": candidate.expression if candidate else "不可用",
                "decision": _decision_label(record.disposition),
                "decisionExplanation": "；".join(record.failure_codes) if record.failure_codes else "机器证据已完成可见评审。",
                **({"metricSummary": {
                    "netIcir": float(metrics.get("validation_icir", 0.0)),
                    "stability": float(metrics.get("validation_subperiod_positive_fraction", 0.0)),
                    "turnover": float(metrics.get("validation_turnover", 0.0)),
                    "incrementalScore": float(metrics.get("validation_incremental_ic_mean", 0.0)),
                }, "evidenceHash": record.validation.visible_metrics.metric_source_hash} if metrics else {}),
            })
        if branch.status.value == "FAILED":
            state = "失败"
        elif any(item.disposition is ValidationDisposition.VALIDATED_RESEARCH for item in records):
            state = "已完成"
        elif records:
            state = "需修订"
        else:
            state = "已阻断"
        groups.append({
            "groupId": group_id, "title": GROUP_LABELS.get(group_id, group_id),
            "purpose": "从本组专业视角提出可证伪候选并接受同一机器证据标准。",
            "state": state, "budgetUsed": len(records) or (1 if branch.status.value == "FAILED" else 0),
            "budgetLimit": 1, "progress": ["提出研究草案", "安全转换", "PIT 与机器评价"],
            "candidates": candidates, **({"failure": branch.failure_code} if branch.failure_code else {}),
        })
    validated = any(item.disposition is ValidationDisposition.VALIDATED_RESEARCH for item in validation.terminal_records)
    return {
        "runId": run_id, "planId": plan["planId"], "state": "已完成",
        "runStateCode": "COMPLETED",
        "researchOutcomeCode": "VALIDATED_RESEARCH" if validated else "REVISE",
        "provider": provider_view,
        "groups": groups, "trialsUsed": len(validation.terminal_records), "trialLimit": plan["budget"]["trials"],
        "currentCheckpoint": "可见机器评价完成" if validated else "候选均被拒绝或需要修订",
        "sealedReview": "等待独立评审" if validated else "未申请",
        "sealedMessage": "封闭数据和指标保持不可见；这里只显示是否进入独立评审。",
        "formalReportPath": report_path, "reportHash": report_hash,
        "limitations": ["研究验证通过不等于挑战者。", "报告不构成投资建议。"],
    }


def _runtime_diagnostics(runtime, provider) -> dict[str, Any]:
    provider_attempts = []
    for attempt in getattr(provider, "attempts", ()) if provider is not None else ():
        provider_attempts.append({
            "groupId": attempt.group_id,
            "status": attempt.status.value,
            "providerCalls": attempt.provider_calls,
            "revisionsUsed": attempt.revisions_used,
            "tokensUsed": attempt.tokens_used,
            "failureCode": attempt.failure_code,
            "proposalHash": attempt.proposal_hash,
        })
    attempts_by_group = {item["groupId"]: item for item in provider_attempts}
    branches = []
    for branch in sorted(runtime.branches, key=lambda item: item.group_id):
        attempt = attempts_by_group.get(branch.group_id, {})
        branches.append({
            "groupId": branch.group_id,
            "status": branch.status.value,
            "failureCode": attempt.get("failureCode") or branch.failure_code,
            "failureType": branch.message or None,
            "proposalHash": branch.proposal.artifact_hash if branch.proposal else None,
        })
    return {
        "schemaVersion": "alphaforge.research-run-diagnostics/v1",
        "runtimeHash": runtime.artifact_hash,
        "branches": branches,
        "providerAttempts": sorted(provider_attempts, key=lambda item: item["groupId"]),
    }


def _candidate_selection_view(validation) -> dict[str, Any]:
    factor_groups: dict[str, list[str]] = {}
    for record in validation.terminal_records:
        factor_hash = record.translation.factor_dag_hash
        if factor_hash:
            factor_groups.setdefault(factor_hash, []).append(record.group_id)
    duplicates = [
        {
            "factorDagHash": factor_hash,
            "groupIds": sorted(set(group_ids)),
        }
        for factor_hash, group_ids in sorted(factor_groups.items())
        if len(set(group_ids)) > 1
    ]
    best = validation.best_observed
    return {
        "status": validation.tournament.status.value,
        "rankedCandidateHashes": list(
            validation.tournament.ranked_candidate_hashes
        ),
        "winnerCandidateHash": validation.tournament.winner_candidate_hash,
        "bestObserved": None if best is None else {
            "candidateDraftHash": best.candidate_draft_hash,
            "groupId": best.group_id,
            "compositeScore": best.composite_score,
            "eligibleForSelection": best.eligible_for_selection,
            "authority": best.authority,
        },
        "duplicateFactorDAGs": duplicates,
    }


def _append_deterministic_baseline(
    runtime: GroupRuntimeResult,
    *,
    intent,
    campaign_hash: str,
    available_fields: tuple[str, ...],
) -> GroupRuntimeResult:
    """Guarantee an executable benchmark without impersonating an LLM result.

    The benchmark is added only when every selected real-model branch failed,
    the question is factor exploration, and the frozen data can represent the
    requested price lookback.  It remains a DRAFT until the existing
    FactorDAG/PIT/Evidence pipeline validates it.
    """

    if any(
        branch.status is GroupBranchStatus.COMPLETED
        and branch.proposal is not None
        and branch.submission is not None
        for branch in runtime.branches
    ):
        return runtime
    if intent.mode is not ResearchMode.NEW_ALPHA_EXPLORATION:
        return runtime
    if "close" not in set(available_fields):
        return runtime
    lookback = intent.factor_lookback_sessions
    if not isinstance(lookback, int) or lookback < 1:
        return runtime

    expression = f"rank_cross_section(pct_change(close, {lookback}))"
    candidate = GroupCandidateDraft(
        candidate_id=f"baseline_momentum_{lookback}d",
        expression=expression,
        variables=("close",),
        lookback_sessions=lookback,
        rationale=(
            f"按用户要求建立 {lookback} 日价格动量的确定性基准；"
            "它不代表真实模型研究小组的产出。"
        ),
    )
    proposal = GroupProposalPayload(
        group_id="research_baseline",
        hypothesis=f"{lookback} 日价格变化可能包含可证伪的横截面动量信息。",
        economic_mechanism="确定性基准用于校准研究问题是否可由当前冻结数据和受限算子执行。",
        falsification_tests=(
            "成本后净表现为负则拒绝",
            "子时期不稳定则修订",
            "缺少增量性则不进入晋级",
        ),
        candidates=(candidate,),
        answer_level=AnswerLevel.CONCEPTUAL,
    )
    submission = ResearchGroupSubmission(
        campaign_hash=campaign_hash,
        group_id=proposal.group_id,
        hypothesis_hash=proposal.artifact_hash,
        candidate_draft_hashes=(candidate.artifact_hash,),
        answer_level=proposal.answer_level,
    )
    baseline = GroupRuntimeBranchResult(
        group_id=proposal.group_id,
        status=GroupBranchStatus.COMPLETED,
        proposal=proposal,
        submission=submission,
    )
    return GroupRuntimeResult(
        branches=tuple(sorted((*runtime.branches, baseline), key=lambda item: item.group_id))
    )


def _upsert_workbench_index(root: Path, overview: Mapping[str, Any]) -> None:
    index_path = root / "index.json"
    current = {"runs": []}
    if index_path.exists():
        try:
            current = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            current = {"runs": []}
    runs = [
        item for item in current.get("runs", ())
        if item.get("run_id") != overview.get("run_id")
    ]
    runs.append(dict(overview))
    _atomic_json(index_path, {"runs": sorted(runs, key=lambda item: item["run_id"])})


def _decision_label(value: ValidationDisposition) -> str:
    return {
        ValidationDisposition.NOT_EVALUATED: "未评价", ValidationDisposition.REJECT: "拒绝",
        ValidationDisposition.REVISE: "需修订", ValidationDisposition.VALIDATED_RESEARCH: "研究验证通过",
        ValidationDisposition.CHALLENGER: "挑战者",
    }[value]


def _metric(metrics: Mapping[str, Any], name: str) -> str:
    value = metrics.get(name)
    return "不可用" if value is None else f"{float(value):.6f}"


__all__ = ["FrozenValidationDataRepository", "ResearchOSV2Service", "SCHEMA_VERSION"]
