"""AlphaForge Campaign product routes.

The router is a thin product adapter over ``veriquant.campaign_api`` and the
static factor-library catalog.  It owns no official Evidence, Promotion,
Portfolio, Risk, Execution, Order, Broker, or Ledger authority.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # FastAPI is optional for local route-unit tests.
    from fastapi import APIRouter, HTTPException
except ModuleNotFoundError:  # pragma: no cover - exercised when FastAPI is unavailable.

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self) -> None:
            self.routes: list[dict[str, Any]] = []

        def get(self, path: str, **kwargs: Any):  # type: ignore[no-untyped-def]
            def decorator(func):
                self.routes.append({"method": "GET", "path": path, "kwargs": kwargs, "endpoint": func})
                return func

            return decorator

        def post(self, path: str, **kwargs: Any):  # type: ignore[no-untyped-def]
            def decorator(func):
                self.routes.append({"method": "POST", "path": path, "kwargs": kwargs, "endpoint": func})
                return func

            return decorator

from src.veriquant.campaign_api import (
    CampaignApiService,
    CampaignConfirmationRequest,
    CampaignDraftRequest,
)
from src.veriquant.contracts import Component, ContractValidationError
from src.veriquant.data import FieldRole
from src.veriquant.factor_library_qualification import (
    FactorDagCompatibility,
    FactorLibraryCatalog,
    FactorLibraryCatalogBuilder,
)
from src.veriquant.integration.e2e import run_offline_acceptance
from src.veriquant.integration.llm_desks import FactorDAGCompilePITAdapter, InMemoryOfficialEvaluationAdapter
from src.veriquant.integration.workbench import publish_workbench_run
from src.veriquant.llm_desks.api import (
    DeskId,
    DeskLLMConfig,
    ProviderBudget,
    ResearchQuestionView,
    ResearchStageController,
    build_default_desk_configs,
    build_default_tools_for_desk,
)
from src.veriquant.alphaforge_llm import AlphaForgeLLMConfigLoader, build_openai_compatible_provider, probe_provider
from src.veriquant.research_campaign import (
    CampaignRoutedLLMRuntime,
    CampaignState,
    InteractionStyle,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchStageControllerFactorExecutor,
)


DEFAULT_HASH = "sha256:" + "0" * 64
DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
SUPPORTED_RUNTIME_MODES = frozenset({"DETERMINISTIC", "REAL_LLM"})

DESK_LABELS = {
    "fundamental": "基本面研究 Desk",
    "expectations_events": "预期与事件 Desk",
    "market_structure": "市场结构 Desk",
    "alternative_data": "另类数据 Desk",
    "macro_regime": "宏观状态 Desk",
    "meta_alpha": "元阿尔法 Desk",
}

MODE_LABELS = {
    "NEW_ALPHA_EXPLORATION": "新 Alpha 探索",
    "VALIDATE_IDEA": "验证我的想法",
    "IMPROVE_EXISTING_FACTOR": "改进现有因子",
    "PORTFOLIO_GAP_SEARCH": "补充组合缺口",
    "DIAGNOSE_FACTOR_PORTFOLIO": "诊断因子/组合",
    "EVENT_MARKET_TOPIC": "事件与市场专题",
    "PAPER_STRATEGY_REPLICATION": "论文/策略复现",
    "NEW_DATA_SOURCE_EVALUATION": "新数据源评估",
    "PRELAUNCH_REVIEW": "上线前评审",
    "MONITOR_DECAY_RETIREMENT": "监控、衰减与退役",
}

METHOD_LABELS = {
    "HYPOTHESIS_DRIVEN": "假设驱动",
    "DATA_ANOMALY": "受限异常探索",
    "RESIDUAL_INCREMENTAL": "残差与增量研究",
    "DIAGNOSTIC_ATTRIBUTION": "诊断归因",
    "EVENT_STUDY": "事件研究",
    "REGIME_CONDITIONING": "市场状态条件",
    "BOUNDED_EVOLUTION": "受限演化",
    "REPLICATION": "复现",
    "ADVERSARIAL_FALSIFICATION": "对抗证伪",
    "ONLINE_MONITORING": "在线监控",
}

STOP_LABELS = {
    "BUDGET_EXHAUSTED": "预算耗尽",
    "NO_INCREMENTAL_CANDIDATE": "没有增量候选",
    "USER_STOPPED": "用户停止",
    "VALIDATION_COMPLETED": "验证完成",
    "CHECKPOINT_REQUIRED": "等待检查点确认",
    "PROVIDER_UNAVAILABLE": "Provider 不可用",
    "DATA_UNAVAILABLE": "数据不可用",
    "MAX_RUNTIME_REACHED": "达到最大运行时间",
}

DATA_LABELS = {
    "price_volume": "价格与成交量",
    "fundamentals": "基本面",
    "events": "事件字段",
    "macro_regime": "简单宏观状态",
    "registered_factor_summary": "已登记因子公开摘要",
}

FAMILY_MAP = {
    "academic": "academic",
    "alpha101": "alpha101",
    "fundamental": "fundamental",
    "gtja191": "gtja191",
    "qlib158": "qlib158",
}


@dataclass
class _CampaignRouteState:
    service: CampaignApiService
    drafts: dict[str, ResearchCampaignDraft]
    campaigns: dict[str, ResearchCampaign]
    runs: dict[str, dict[str, Any]]
    factor_catalog: FactorLibraryCatalog | None = None


_state = _CampaignRouteState(service=CampaignApiService(), drafts={}, campaigns={}, runs={})


def reset_alphaforge_campaign_state() -> None:
    """Clear in-memory draft/campaign state for deterministic tests."""

    _state.drafts.clear()
    _state.campaigns.clear()
    _state.runs.clear()


def _require_mapping(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return payload


def _parse_depth(value: Any) -> ResearchDepth | None:
    if value is None:
        return None
    try:
        return ResearchDepth(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid research depth") from exc


def _parse_interaction(value: Any) -> InteractionStyle | None:
    if value is None:
        return None
    try:
        return InteractionStyle(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid interaction style") from exc


def _draft_id(draft: ResearchCampaignDraft) -> str:
    return f"draft_{draft.artifact_hash.removeprefix('sha256:')[:16]}"


def _campaign_id(campaign: ResearchCampaign) -> str:
    return f"campaign_{campaign.artifact_hash.removeprefix('sha256:')[:16]}"


def _budget_view(draft: ResearchCampaignDraft | ResearchCampaign) -> dict[str, int]:
    return {
        "factorFamilies": draft.budget.max_factor_families,
        "trials": draft.budget.max_trials,
        "modelCalls": draft.budget.max_llm_calls,
        "runtimeMinutes": max(1, draft.budget.max_runtime_seconds // 60),
    }


def _draft_view(draft: ResearchCampaignDraft, *, state: str | None = None, campaign_id: str | None = None) -> dict[str, Any]:
    selected_state = state or draft.state.value
    return {
        "schemaVersion": "alphaforge.campaign-ui/v1",
        "draftId": _draft_id(draft),
        **({"campaignId": campaign_id} if campaign_id else {}),
        "state": "CONFIRMED" if selected_state == "CONFIRMED" else "NEEDS_CONFIRMATION",
        "question": draft.question,
        "mode": draft.mode.value,
        "depth": draft.research_depth.value,
        "interactionStyle": draft.interaction_style.value,
        "researchObject": draft.subject.description,
        "universe": list(draft.subject.universe),
        "horizon": draft.subject.horizon,
        "allowedData": [DATA_LABELS.get(item, item) for item in draft.subject.allowed_data],
        "methodModels": [METHOD_LABELS.get(item.value, item.value) for item in draft.method_models],
        "selectedDesks": list(draft.candidate_agents),
        "budget": _budget_view(draft),
        "checkpoints": [item.description or item.stage for item in draft.user_checkpoints],
        "stopConditions": [STOP_LABELS.get(item.value, item.value) for item in draft.stop_conditions],
        "clarificationQuestions": [
            {
                "id": item.question_id,
                "field": item.field_name,
                "question": item.question,
                "required": item.required,
            }
            for item in draft.clarification_questions
        ],
        "unresolvedFields": list(draft.unresolved_fields),
        "runtimeMode": "DETERMINISTIC",
        "allowsOfficialEvidence": draft.research_depth is not ResearchDepth.QUICK_EXPLORATION,
        "allowsPromotion": False,
        "fixtureFingerprint": draft.artifact_hash,
    }


def _recommendation_view(draft: ResearchCampaignDraft | None, response_message: str, questions: Iterable[str]) -> dict[str, Any]:
    mode = draft.mode if draft is not None else ResearchMode.NEW_ALPHA_EXPLORATION
    return {
        "recommendedMode": mode.value,
        "explanation": response_message,
        "questions": list(questions)[:3] or ["请确认研究对象、股票池、预测周期和预算。"],
        "canRun": False,
    }


def _draft_request_from_payload(payload: dict[str, Any]) -> CampaignDraftRequest:
    question = str(payload.get("question") or payload.get("user_text") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    return CampaignDraftRequest(
        user_text=question,
        decision_time=DEFAULT_DECISION_TIME,
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        allowed_data=("price_volume", "fundamentals"),
        data_manifest_hash=DEFAULT_HASH,
        policy_hash=DEFAULT_HASH,
        code_hash=DEFAULT_HASH,
        preferred_depth=_parse_depth(payload.get("depth")),
        interaction_style=_parse_interaction(payload.get("interactionStyle") or payload.get("interaction_style")),
    )


def _route_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    status_code = 422 if isinstance(exc, ContractValidationError) else 400
    detail = getattr(exc, "code", None)
    return HTTPException(status_code=status_code, detail=str(detail or exc))


def _stored_draft(payload: dict[str, Any]) -> ResearchCampaignDraft:
    draft_id = str(payload.get("draftId") or payload.get("draft_id") or "")
    if not draft_id:
        raise HTTPException(status_code=400, detail="draftId is required")
    draft = _state.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return draft


def _run_view(campaign: ResearchCampaign, selected_desks: tuple[str, ...]) -> dict[str, Any]:
    desks = []
    for desk_id in selected_desks:
        status = "WAITING"
        desks.append(
            {
                "id": desk_id,
                "title": DESK_LABELS.get(desk_id, desk_id),
                "status": status,
                "runtimeMode": "DETERMINISTIC",
                "budgetUsed": 0,
                "budgetLimit": max(1, campaign.budget.max_trials // max(1, len(selected_desks))),
                "stages": [
                    {"id": "hypothesis", "title": "提出可证伪假设", "status": "WAITING", "detail": "等待用户检查点确认后才启动。"},
                    {"id": "construct", "title": "构造因子草案", "status": "WAITING", "detail": "只允许 FactorDAG 草案。"},
                    {
                        "id": "compile",
                        "title": "FactorDAG / PIT",
                        "status": "WAITING",
                        "detail": "尚未执行；不会把路由意图伪装成 PIT 或 Evidence。",
                    },
                    {
                        "id": "evaluate",
                        "title": "官方 Evidence",
                        "status": "WAITING",
                        "detail": "尚未执行；官方 Evidence 只能由确定性 Evidence Service 产生。",
                    },
                    {
                        "id": "feedback",
                        "title": "脱敏反馈",
                        "status": "WAITING",
                        "detail": "尚未执行；不包含 sealed 内容。",
                    },
                ],
            }
        )
    return {
        "campaignId": _campaign_id(campaign),
        "state": "ROUTED",
        "runtimeMode": "DETERMINISTIC",
        "selectedDesks": desks,
        "budget": _budget_view(campaign),
        "trialsUsed": 0,
        "currentCheckpoint": "等待用户确认启动",
        "checkpointState": "PENDING",
        "messages": [
            "离线 Campaign API 已生成 ROUTED run intent；尚未启动 Desk、FactorDAG、PIT、Evidence 或 Promotion。",
            "real_ashare_extended 和 real_llm_campaign_smoke 在本次集成中保持 disabled/optional。",
        ],
        "reportPath": "/reports",
        "workbenchPath": "/veriquant/workbench",
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _agent_dotenv_path() -> Path:
    return _repo_root() / "agent" / ".env"


def _safe_campaign_id(campaign_id: str) -> str:
    value = str(campaign_id or "").strip()
    if not value or "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid campaign id")
    return value


def _runtime_mode_from_payload(payload: Any) -> str:
    if payload is None:
        return "DETERMINISTIC"
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="runtime request must be an object")
    value = str(payload.get("runtimeMode", "DETERMINISTIC")).strip().upper()
    if value not in SUPPORTED_RUNTIME_MODES:
        raise HTTPException(status_code=422, detail="unsupported runtimeMode")
    return value


def _campaign_run_output_root(campaign_id: str) -> Path:
    return _repo_root() / ".autobuild" / "veriquant" / "integration" / "alphaforge_campaign_runs" / campaign_id


def _campaign_run_result_path(campaign_id: str) -> Path:
    return _campaign_run_output_root(campaign_id) / "result.json"


def _campaign_runtime_result_path(campaign_id: str, runtime_mode: str = "DETERMINISTIC") -> Path:
    if runtime_mode == "REAL_LLM":
        return _campaign_run_output_root(campaign_id) / "campaign_runtime.real_llm.json"
    return _campaign_run_output_root(campaign_id) / "campaign_runtime.json"


def _campaign_report_json_path(campaign_id: str) -> Path:
    return _campaign_run_output_root(campaign_id) / "campaign_report.json"


def _campaign_report_markdown_path(campaign_id: str) -> Path:
    return _campaign_run_output_root(campaign_id) / "campaign_report.md"


def _workbench_artifact_root() -> Path:
    return _repo_root() / ".autobuild" / "veriquant" / "integration" / "workbench_artifacts"


def _workbench_run_path(campaign_id: str) -> str:
    return f"/veriquant/workbench?run_id={campaign_id}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib

    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _result_json(campaign_id: str) -> dict[str, Any] | None:
    path = _campaign_run_result_path(campaign_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"stored campaign result is unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="stored campaign result must be an object")
    return payload


def _runtime_json(campaign_id: str, runtime_mode: str = "DETERMINISTIC") -> dict[str, Any] | None:
    path = _campaign_runtime_result_path(campaign_id, runtime_mode)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"stored campaign runtime is unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="stored campaign runtime must be an object")
    payload.setdefault("runtime_mode", runtime_mode)
    payload.setdefault("artifact_hash", _stable_payload_hash(payload))
    return payload


def _latest_runtime_json(campaign_id: str) -> dict[str, Any] | None:
    candidates: list[tuple[float, str]] = []
    for mode in sorted(SUPPORTED_RUNTIME_MODES):
        path = _campaign_runtime_result_path(campaign_id, mode)
        if path.exists():
            candidates.append((path.stat().st_mtime, mode))
    if not candidates:
        return None
    _, mode = max(candidates)
    return _runtime_json(campaign_id, mode)


def _llm_configuration_view() -> dict[str, Any]:
    try:
        configuration = AlphaForgeLLMConfigLoader(dotenv_path=_agent_dotenv_path()).load()
    except ContractValidationError as exc:
        return {"status": "UNAVAILABLE", "reasonCode": exc.code.value}
    public = dict(configuration.public_view())
    probe = probe_provider(configuration)
    if probe is None:
        reason = "LLM_DISABLED" if not bool(public.get("enabled")) else "ADAPTER_UNAVAILABLE"
        return {"status": "UNAVAILABLE", "reasonCode": reason}
    payload = {
        "status": probe.status.value,
        "host": probe.endpoint_host,
        "model": probe.model,
    }
    if probe.reason_code:
        payload["reasonCode"] = probe.reason_code
    return payload


def _real_llm_executor(campaign: ResearchCampaign, route: Any) -> ResearchStageControllerFactorExecutor:
    configuration = AlphaForgeLLMConfigLoader(dotenv_path=_agent_dotenv_path()).load()
    probe = probe_provider(configuration)
    if probe is None or probe.status.value != "AVAILABLE":
        raise HTTPException(status_code=409, detail="real LLM provider is unavailable")
    if configuration.settings is None:
        raise HTTPException(status_code=409, detail="real LLM provider settings are unavailable")
    provider = build_openai_compatible_provider(configuration)
    selected = tuple(str(item) for item in route.selected_desks)
    defaults = {desk.desk_id.value: desk for desk in build_default_desk_configs() if desk.desk_id.value in selected}
    desks: dict[str, DeskLLMConfig] = {}
    providers: dict[DeskId, Any] = {}
    tools: dict[DeskId, Any] = {}
    for desk_name in selected:
        base = defaults.get(desk_name)
        if base is None:
            continue
        desk_id = DeskId(desk_name)
        budget = ProviderBudget(
            model=configuration.settings.model,
            # Provider settings limit output per call; this budget is the
            # cumulative input+output allowance across all Desk stages.
            max_tokens=min(16384, max(8192, int(configuration.settings.max_tokens) * 3)),
            max_calls=6,
            max_cost=1.0,
            timeout_seconds=min(30.0, float(configuration.settings.timeout_seconds)),
            max_retries=int(configuration.settings.max_retries),
        )
        desks[desk_name] = DeskLLMConfig(
            desk_id=desk_id,
            role=base.role,
            context=base.context,
            model=configuration.settings.model,
            budget=budget,
            tool_names=base.tool_names,
            artifact_namespace=base.artifact_namespace,
        )
        providers[desk_id] = provider
        tools[desk_id] = build_default_tools_for_desk(desk_id)
    question = ResearchQuestionView(
        question_id="question." + campaign.campaign_id.replace("campaign.", "").replace(":", "_"),
        text=campaign.question,
        universe=campaign.subject.universe,
        horizon=campaign.subject.horizon,
        decision_time=campaign.subject.decision_time,
        availability_assumptions=("Campaign subject allowed data is visible train/validation context only.",),
    )
    field_roles = {
        "close": FieldRole.FEATURE,
        "volume": FieldRole.FEATURE,
        "market_cap": FieldRole.FEATURE,
        "book_value": FieldRole.FEATURE,
        "activity_proxy": FieldRole.FEATURE,
        "macro_rate": FieldRole.FEATURE,
    }
    controller = ResearchStageController(
        compiler=FactorDAGCompilePITAdapter(field_roles=field_roles, pit_snapshot_hash=DEFAULT_HASH),
        evaluator=InMemoryOfficialEvaluationAdapter(),
    )
    return ResearchStageControllerFactorExecutor(
        controller=controller,
        question=question,
        desks=desks,
        providers=providers,
        tools=tools,
    )


def _public_research_outputs(executor: ResearchStageControllerFactorExecutor) -> list[dict[str, Any]]:
    """Project typed Desk results without prompts, raw responses, or authority claims."""

    outputs: list[dict[str, Any]] = []
    for task_id, result in executor.desk_results.items():
        hypothesis = result.hypothesis
        factor = result.factor_draft
        compiled = result.compile_pit
        evaluation = result.official_evaluation
        feedback = result.feedback
        item: dict[str, Any] = {
            "task_id": task_id,
            "desk_id": result.desk_id.value,
            "status": result.status.value,
            "failure_code": result.failure_code,
            "message": result.message,
            "authority": "DRAFT_ONLY",
            "hypothesis": None,
            "factor_draft": None,
            "compile_smoke": None,
            "visible_evaluation_smoke": None,
            "redacted_feedback": None,
        }
        if hypothesis is not None:
            item["hypothesis"] = {
                "statement": hypothesis.statement,
                "economic_mechanism": hypothesis.economic_mechanism,
                "expected_sign": hypothesis.expected_sign.value,
                "falsification_tests": list(hypothesis.falsification_tests),
                "artifact_hash": hypothesis.artifact_hash,
            }
        if factor is not None:
            item["factor_draft"] = {
                "name": factor.name,
                "description": factor.description,
                "expression": factor.expression,
                "variables": list(factor.variables),
                "lookback_bars": factor.lookback_bars,
                "neutralization": list(factor.neutralization),
                "artifact_hash": factor.artifact_hash,
            }
        if compiled is not None:
            item["compile_smoke"] = {
                "status": "FACTOR_DAG_PARSE_ACCEPTED_WITH_PLACEHOLDER_PIT_BINDING",
                "compiled_factor_hash": compiled.compiled_factor_hash,
                "pit_snapshot_hash": compiled.pit_snapshot_hash,
                "is_production_pit": False,
            }
        if evaluation is not None:
            item["visible_evaluation_smoke"] = {
                "status": "INTEGRATION_SMOKE_NOT_OFFICIAL_EVIDENCE",
                "evidence_hash": evaluation.evidence_hash,
                "visible_metrics": dict(evaluation.redacted_summary.visible_metrics),
                "allowed_notes": list(evaluation.redacted_summary.allowed_notes),
                "is_official_evidence": False,
            }
        if feedback is not None:
            item["redacted_feedback"] = {
                "explanation": feedback.explanation,
                "artifact_hash": feedback.artifact_hash,
            }
        outputs.append(item)
    return outputs


def _run_campaign_runtime(campaign_id: str, campaign: ResearchCampaign, *, runtime_mode: str = "DETERMINISTIC") -> dict[str, Any]:
    cached = _runtime_json(campaign_id, runtime_mode)
    if cached is not None:
        return cached
    _, _, route = _state.service.plan_run(campaign)
    if route is None:
        raise HTTPException(status_code=409, detail="campaign route is blocked")
    protocol_plan = _state.service.protocol_compiler.compile(campaign=campaign, route=route)
    executor = _real_llm_executor(campaign, route) if runtime_mode == "REAL_LLM" else None
    manifest = CampaignRoutedLLMRuntime(executor=executor).run(
        run_id=f"runtime.{campaign_id}",
        campaign=campaign,
        route=route,
        protocol_plan=protocol_plan,
    )
    payload = manifest.to_dict()
    payload["runtime_mode"] = runtime_mode
    if executor is not None:
        payload["research_outputs"] = _public_research_outputs(executor)
    payload["artifact_hash"] = _stable_payload_hash(payload)
    _atomic_write_json(_campaign_runtime_result_path(campaign_id, runtime_mode), payload)
    return payload


def _completed_stage(status: str) -> dict[str, str]:
    return {"id": status.lower(), "title": status, "status": "COMPLETED", "detail": "offline deterministic acceptance step completed"}


def _stage_status_from_runtime(status: str) -> str:
    if status in {"DRAFT", "EXPLANATION", "CONTROL_REFERENCE"}:
        return "COMPLETED"
    if status in {"BLOCKED", "SKIPPED", "CANCELLED"}:
        return "BLOCKED"
    return "FAILED"


def _runtime_blocks_offline_projection(runtime_payload: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_payload, dict):
        return False
    if str(runtime_payload.get("runtime_mode", "DETERMINISTIC")) != "REAL_LLM":
        return False
    runtime_status = str(runtime_payload.get("status", "BLOCKED"))
    if runtime_status in {"COMPLETED", "PARTIAL"}:
        return not any(
            isinstance(branch, dict) and str(branch.get("status")) == "COMPLETED"
            for branch in runtime_payload.get("branch_results", [])
        )
    return True


def _desks_from_runtime(runtime_payload: dict[str, Any]) -> list[dict[str, Any]]:
    desks: list[dict[str, Any]] = []
    runtime_mode = str(runtime_payload.get("runtime_mode", "DETERMINISTIC"))
    stage_detail = (
        "REAL_LLM Campaign stage; output remains DRAFT/blocked and carries no official authority"
        if runtime_mode == "REAL_LLM"
        else "Campaign-routed deterministic runtime stage"
    )
    for branch in runtime_payload.get("branch_results", []):
        if not isinstance(branch, dict):
            continue
        desk_id = str(branch.get("desk_id", "unknown"))
        branch_status = str(branch.get("status", "BLOCKED"))
        stages = []
        for stage in branch.get("stage_results", []):
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id", "stage"))
            stages.append(
                {
                    "id": stage_id,
                    "title": stage_id,
                    "status": _stage_status_from_runtime(str(stage.get("status", "BLOCKED"))),
                    "detail": stage_detail,
                }
            )
        desks.append(
            {
                "id": desk_id,
                "title": DESK_LABELS.get(desk_id, desk_id),
                "status": "COMPLETED" if branch_status == "COMPLETED" else "BLOCKED",
                "runtimeMode": runtime_mode,
                "budgetUsed": max(1, len(stages)),
                "budgetLimit": max(1, len(stages)),
                "stages": stages,
                **({"failure": str(branch.get("message"))} if branch.get("message") else {}),
            }
        )
    return desks


def _campaign_report(campaign_id: str, result: dict[str, Any], runtime_payload: dict[str, Any] | None) -> dict[str, Any]:
    path = _campaign_report_json_path(campaign_id)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            return payload
    runtime_hash = _stable_payload_hash(runtime_payload) if isinstance(runtime_payload, dict) else None
    selected_desks = [desk["id"] for desk in _desks_from_runtime(runtime_payload or {})]
    report = {
        "schema_version": "alphaforge.campaign-report/v1",
        "campaign_id": campaign_id,
        "status": "COMPLETED" if result.get("status") == "PASSED" else "FAILED",
        "runtime_manifest_hash": runtime_hash,
        "offline_acceptance_hash": result.get("artifact_hash"),
        "selected_desks": selected_desks,
        "offline_acceptance_run_id": result.get("run_id"),
        "workbench_detail_hash": result.get("artifact_hashes", {}).get("workbench_detail"),
        "factor_dag_hash": result.get("artifact_hashes", {}).get("factor_dag"),
        "shadow_status": result.get("shadow", {}).get("status"),
        "limitations": (
            "standard/strict start currently combines Campaign-routed runtime with generic offline A-F acceptance projection",
            "real_30x180 and real_llm smoke remain disabled/optional",
            "no real account, live order, secret, network, or provider key is required",
        ),
    }
    report["artifact_hash"] = _stable_payload_hash(report)
    _atomic_write_json(path, report)
    markdown = "\n".join(
        (
            "# AlphaForge Campaign Report",
            "",
            f"- Campaign: `{campaign_id}`",
            f"- Status: `{report['status']}`",
            f"- Selected desks: `{', '.join(selected_desks) if selected_desks else 'none'}`",
            f"- Runtime manifest: `{runtime_hash}`",
            f"- Offline acceptance: `{report['offline_acceptance_hash']}`",
            f"- FactorDAG: `{report['factor_dag_hash']}`",
            f"- Workbench detail: `{report['workbench_detail_hash']}`",
            f"- Shadow status: `{report['shadow_status']}`",
            "",
            "## Boundary",
            "",
            "- This is a deterministic offline Campaign report.",
            "- The official evidence projection is from the local A-F acceptance chain, not real 30x180 or real LLM smoke.",
            "- No live account, live order, broker path, secret, provider key, or network access is required.",
            "",
        )
    )
    _atomic_write_text(_campaign_report_markdown_path(campaign_id), markdown)
    return report


def _publish_runtime_workbench_projection(campaign_id: str, runtime_payload: dict[str, Any], *, status: str) -> dict[str, Any]:
    """Publish a read-only runtime report without claiming official evidence.

    This is a UI/report-center materialization only.  It never creates
    official metrics, promotion, portfolio, shadow execution, or ledger state.
    """

    desks = _desks_from_runtime(runtime_payload)
    runtime_mode = str(runtime_payload.get("runtime_mode", "DETERMINISTIC"))
    runtime_hash = runtime_payload.get("artifact_hash") or _stable_payload_hash(runtime_payload)
    nodes: list[dict[str, Any]] = []
    lineage_items: list[dict[str, Any]] = []
    critic_findings: list[dict[str, Any]] = []
    research_outputs = {
        str(item.get("desk_id")): item
        for item in runtime_payload.get("research_outputs", [])
        if isinstance(item, dict) and item.get("desk_id")
    }
    for desk in desks:
        desk_id = str(desk.get("id", "unknown"))
        desk_status = str(desk.get("status", "BLOCKED"))
        research_output = research_outputs.get(desk_id, {})
        lineage_items.append(
            {
                "candidate_id": f"desk:{desk_id}",
                "family_id": "campaign-runtime-only",
                "status": "DRAFT" if desk_status == "COMPLETED" else "BLOCKED",
                "operator": "Campaign-routed Research Desk",
                "artifact_hash": runtime_hash,
                "hypothesis": research_output.get("hypothesis"),
                "factor_draft": research_output.get("factor_draft"),
                "compile_smoke": research_output.get("compile_smoke"),
                "redacted_feedback": research_output.get("redacted_feedback"),
                "authority": "DRAFT_ONLY",
            }
        )
        for stage in desk.get("stages", []):
            if not isinstance(stage, dict):
                continue
            stage_status = str(stage.get("status", "BLOCKED"))
            nodes.append(
                {
                    "id": f"{desk_id}:{stage.get('id', 'stage')}",
                    "role": desk_id,
                    "stage": stage.get("title", stage.get("id", "stage")),
                    "status": "DRAFT" if stage_status == "COMPLETED" else "BLOCKED",
                    "summary": stage.get("detail"),
                }
            )
        if desk.get("failure"):
            critic_findings.append(
                {
                    "critic": f"ResearchDesk:{desk_id}",
                    "verdict": "BLOCKED",
                    "failure_code": "REAL_LLM_RUNTIME_BLOCKED",
                    "message": str(desk["failure"]),
                }
            )
    if not critic_findings:
        critic_findings.append(
            {
                "critic": "RuntimeBoundary",
                "verdict": "DRAFT_ONLY",
                "message": "Campaign runtime completed without official Evidence or Promotion.",
            }
        )
    artifacts = {
        "overview": {
            "run_id": campaign_id,
            "title": "AlphaForge Campaign Runtime Report",
            "status": status,
            "runtime_mode": runtime_mode,
            "updated_at": DEFAULT_DECISION_TIME.isoformat(),
            "decision_time": DEFAULT_DECISION_TIME.isoformat(),
            "campaign_runtime_hash": runtime_hash,
            "candidate_count": len(desks),
            "alert_count": len(critic_findings),
            "research_dag": {"nodes": nodes},
            "authority_boundary": "runtime-only DRAFT/blocked report; no official metrics, promotion, shadow, order, or ledger authority",
        },
        "lineage": {
            "items": lineage_items,
            "runtime_mode": runtime_mode,
            "campaign_runtime_hash": runtime_hash,
        },
        "evidence": {
            "items": [
                {
                    "evidence_id": "runtime_only.no_official_evidence",
                    "metric": "official_evidence",
                    "value": "NOT_RUN",
                    "scope": "PUBLIC_RUNTIME",
                    "summary": "Runtime-only Campaign reports do not contain official Evidence.",
                }
            ],
            "sealed_redacted": True,
        },
        "critics": {"findings": critic_findings},
        "promotion": {
            "verdict": "NOT_RUN",
            "status": status,
            "failure_code": "REAL_LLM_RUNTIME_ONLY" if status != "BLOCKED" else "REAL_LLM_RUNTIME_BLOCKED",
            "sealed_status": "not_accessed",
        },
        "citations": {"items": []},
        "shadow_portfolio": {"status": "NOT_RUN", "items": []},
        "nav_attribution": {"attribution": []},
        "drift_alerts": {"alerts": critic_findings},
    }
    return publish_workbench_run(
        root=_workbench_artifact_root(),
        run_id=campaign_id,
        status=status,
        updated_at=DEFAULT_DECISION_TIME.isoformat(),
        artifacts=artifacts,
    )


def _completed_run_view(campaign_id: str, result: dict[str, Any], runtime_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = runtime_payload or _latest_runtime_json(campaign_id)
    if _runtime_blocks_offline_projection(runtime):
        return _runtime_blocked_run_view(campaign_id, runtime or {}, None)
    runtime_mode = str(runtime.get("runtime_mode", "DETERMINISTIC")) if isinstance(runtime, dict) else "DETERMINISTIC"
    runtime_desks = _desks_from_runtime(runtime) if runtime is not None else []
    desk_payload = result.get("research_desks", {})
    completed = [str(item) for item in desk_payload.get("completed", [])]
    incomplete = [str(item) for item in desk_payload.get("failed_or_incomplete", [])]
    desks: list[dict[str, Any]] = list(runtime_desks)
    if desks:
        completed = [desk["id"] for desk in desks if desk["status"] == "COMPLETED"]
        incomplete = [desk["id"] for desk in desks if desk["status"] != "COMPLETED"]
    if not desks:
        desks = []
    for desk_id in completed:
        if runtime_desks:
            continue
        desks.append(
            {
                "id": desk_id,
                "title": DESK_LABELS.get(desk_id, desk_id),
                "status": "COMPLETED",
                "runtimeMode": "DETERMINISTIC",
                "budgetUsed": 1,
                "budgetLimit": 1,
                "stages": [
                    _completed_stage("hypothesis"),
                    _completed_stage("FactorDAG/PIT"),
                    _completed_stage("Evidence"),
                    _completed_stage("redacted_feedback"),
                ],
            }
        )
    for desk_id in incomplete:
        if runtime_desks:
            continue
        desks.append(
            {
                "id": desk_id,
                "title": DESK_LABELS.get(desk_id, desk_id),
                "status": "FAILED",
                "runtimeMode": "DETERMINISTIC",
                "budgetUsed": 1,
                "budgetLimit": 1,
                "failure": "offline acceptance fixture intentionally keeps at least one branch incomplete",
                "stages": [
                    {"id": "hypothesis", "title": "hypothesis", "status": "FAILED", "detail": "branch did not pass the deterministic fixture gates"},
                ],
            }
        )
    artifact_hashes = result.get("artifact_hashes", {})
    shadow = result.get("shadow", {})
    report = _campaign_report(campaign_id, result, runtime)
    return {
        "campaignId": campaign_id,
        "state": "COMPLETED" if result.get("status") == "PASSED" else "FAILED",
        "runtimeMode": runtime_mode,
        "selectedDesks": desks,
        "budget": {"factorFamilies": max(1, len(desks)), "trials": max(1, len(desks)), "modelCalls": 0, "runtimeMinutes": 1},
        "trialsUsed": len(desks),
        "currentCheckpoint": "离线验收链已生成 Workbench 报告",
        "checkpointState": "REACHED",
        "messages": [
            f"已先执行 Campaign-routed deterministic runtime：Router 选中的 {len(desks)} 个 Desk 拥有独立 artifact namespace。",
            "随后执行 deterministic offline A-F acceptance：冻结数据、FactorDAG/PIT、Evidence、统计门、Shadow 和 Workbench 发布。",
            "official report 仍来自本地离线验收链；real_30x180 与 real_llm smoke 保持 disabled/optional，成功后再接入。",
        ],
        "reportPath": str(_campaign_report_markdown_path(campaign_id)),
        "workbenchPath": "/veriquant/workbench",
        "artifactHash": report.get("artifact_hash") or result.get("artifact_hash"),
        "factorDagHash": artifact_hashes.get("factor_dag"),
        "workbenchDetailHash": artifact_hashes.get("workbench_detail"),
        "shadowStatus": shadow.get("status"),
    }


def _runtime_only_run_view(campaign_id: str, runtime_payload: dict[str, Any], campaign: ResearchCampaign) -> dict[str, Any]:
    desks = _desks_from_runtime(runtime_payload)
    runtime_mode = str(runtime_payload.get("runtime_mode", "DETERMINISTIC"))
    projection_status = "DRAFT" if runtime_payload.get("status") in {"COMPLETED", "PARTIAL", "AWAITING_CHECKPOINT"} else "BLOCKED"
    publication = _publish_runtime_workbench_projection(campaign_id, runtime_payload, status=projection_status)
    messages = (
        [
            "真实 LLM Desk 已完成受控 DRAFT 运行；页面展示结构化假设、FactorDAG 草案和脱敏反馈。",
            "当前编译与评价仍是集成 smoke，不是生产 PIT 或 official Evidence；因此不会启动 Promotion 或 Shadow。",
        ]
        if runtime_mode == "REAL_LLM"
        else [
            "QUICK_EXPLORATION 只执行 Campaign-routed DRAFT runtime；不启动 official Evidence、Promotion 或 Shadow。",
            "如需验证，请把研究深度改为标准研究或严格验证后重新确认 Campaign。",
        ]
    )
    return {
        "campaignId": campaign_id,
        "state": "COMPLETED" if runtime_payload.get("status") in {"COMPLETED", "PARTIAL", "AWAITING_CHECKPOINT"} else "BLOCKED",
        "runtimeMode": runtime_mode,
        "selectedDesks": desks,
        "researchOutputs": list(runtime_payload.get("research_outputs", [])),
        "budget": _budget_view(campaign),
        "trialsUsed": len(desks),
        "currentCheckpoint": "真实 LLM 草案运行已完成" if runtime_mode == "REAL_LLM" else "快速探索已完成草案运行",
        "checkpointState": "REACHED",
        "messages": messages,
        "reportPath": _workbench_run_path(campaign_id),
        "workbenchPath": _workbench_run_path(campaign_id),
        "artifactHash": runtime_payload.get("artifact_hash") or _stable_payload_hash(runtime_payload),
        "workbenchDetailHash": publication.get("detail_hash"),
    }


def _runtime_blocked_run_view(campaign_id: str, runtime_payload: dict[str, Any], campaign: ResearchCampaign | None) -> dict[str, Any]:
    desks = _desks_from_runtime(runtime_payload)
    runtime_mode = str(runtime_payload.get("runtime_mode", "REAL_LLM"))
    publication = _publish_runtime_workbench_projection(campaign_id, runtime_payload, status="BLOCKED")
    return {
        "campaignId": campaign_id,
        "state": "BLOCKED",
        "runtimeMode": runtime_mode,
        "selectedDesks": desks,
        "researchOutputs": list(runtime_payload.get("research_outputs", [])),
        "budget": _budget_view(campaign) if campaign is not None else {"factorFamilies": max(1, len(desks)), "trials": max(1, len(desks)), "modelCalls": 0, "runtimeMinutes": 1},
        "trialsUsed": len(desks),
        "currentCheckpoint": "真实 LLM 分支未产生可继续评价的候选，已 fail-closed",
        "checkpointState": "REACHED",
        "messages": [
            "本次页面只展示 Campaign Router 选中的 REAL_LLM Desk 结果。",
            "不会用 deterministic offline acceptance fixture 冒充真实 Desk 运行成功或失败。",
            "LLM 输出仍只允许 DRAFT/解释；未通过 schema、预算或 FactorDAG/PIT 的分支不会进入 official Evidence 或 Promotion。",
        ],
        "reportPath": _workbench_run_path(campaign_id),
        "workbenchPath": _workbench_run_path(campaign_id),
        "artifactHash": runtime_payload.get("artifact_hash") or _stable_payload_hash(runtime_payload),
        "workbenchDetailHash": publication.get("detail_hash"),
    }


def _runtime_detached_run_view(campaign_id: str, runtime_payload: dict[str, Any], campaign: ResearchCampaign | None) -> dict[str, Any]:
    desks = _desks_from_runtime(runtime_payload)
    runtime_mode = str(runtime_payload.get("runtime_mode", "DETERMINISTIC"))
    runtime_status = str(runtime_payload.get("status", "COMPLETED"))
    state = "AWAITING_CHECKPOINT" if runtime_status == "AWAITING_CHECKPOINT" else "COMPLETED"
    projection_status = "DRAFT" if state == "COMPLETED" else "INCOMPLETE"
    publication = _publish_runtime_workbench_projection(campaign_id, runtime_payload, status=projection_status)
    return {
        "campaignId": campaign_id,
        "state": state,
        "runtimeMode": runtime_mode,
        "selectedDesks": desks,
        "researchOutputs": list(runtime_payload.get("research_outputs", [])),
        "budget": _budget_view(campaign) if campaign is not None else {"factorFamilies": max(1, len(desks)), "trials": max(1, len(desks)), "modelCalls": 0, "runtimeMinutes": 1},
        "trialsUsed": len(desks),
        "currentCheckpoint": "Campaign runtime 已完成；尚未绑定离线验收报告" if state == "COMPLETED" else "Campaign runtime 等待检查点确认",
        "checkpointState": "REACHED",
        "messages": [
            "这是 Campaign runtime 的可恢复视图，只展示 Router 选中的 Desk。",
            "当前没有 result.json，因此不会附加 deterministic offline A-F acceptance、Promotion、Shadow 或 Workbench 报告。",
            "LLM 输出仍只允许 DRAFT/解释；official Evidence 与 Promotion 需由后续确定性链路单独生成。",
        ],
        "reportPath": _workbench_run_path(campaign_id),
        "workbenchPath": _workbench_run_path(campaign_id),
        "artifactHash": runtime_payload.get("artifact_hash") or _stable_payload_hash(runtime_payload),
        "workbenchDetailHash": publication.get("detail_hash"),
    }


def _parallel_status(package_id: str) -> dict[str, Any]:
    path = _repo_root() / ".autobuild" / "veriquant" / "parallel" / package_id / "result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"package": package_id, "status": "MISSING", "path": str(path.relative_to(_repo_root()))}
    except json.JSONDecodeError:
        return {"package": package_id, "status": "PARSE_ERROR", "path": str(path.relative_to(_repo_root()))}
    return {
        "package": package_id,
        "status": str(payload.get("status", "UNKNOWN")),
        "test_result": payload.get("test_result"),
        "operational_smoke_status": payload.get("operational_smoke_status"),
    }


def _catalog() -> FactorLibraryCatalog:
    if _state.factor_catalog is None:
        _state.factor_catalog = FactorLibraryCatalogBuilder().build(generated_at="UNSPECIFIED")
    return _state.factor_catalog


def _source_family(record: Any) -> str:
    value = str(getattr(record, "zoo", "") or getattr(record, "family", "")).lower()
    for key, mapped in FAMILY_MAP.items():
        if key in value:
            return mapped
    return "academic"


def _eligibility(record: Any) -> str:
    compatibility = getattr(record, "compatibility", None)
    status = getattr(record, "qualification_status", None)
    if compatibility is FactorDagCompatibility.DIRECT_FACTOR_DAG:
        return "DAG_PORTED"
    return str(getattr(status, "value", status or "METADATA_VALIDATED"))


def _compatibility_class(record: Any) -> str:
    compatibility = getattr(record, "compatibility", None)
    if compatibility is FactorDagCompatibility.DIRECT_FACTOR_DAG:
        return "directly_representable"
    if compatibility is FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR:
        return "needs_deterministic_operator"
    if compatibility is FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY:
        return "auxiliary_only"
    return "rejected"


def _qualification_status(record: Any) -> str:
    compatibility = getattr(record, "compatibility", None)
    if compatibility is FactorDagCompatibility.DIRECT_FACTOR_DAG:
        return "DIRECT_FACTOR_DAG"
    if compatibility is FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR:
        return "NEEDS_DETERMINISTIC_OPERATOR"
    if compatibility is FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY:
        return "UNSAFE_AUXILIARY_ONLY"
    return "REJECTED"


def _factor_item(record: Any) -> dict[str, Any]:
    themes = tuple(getattr(record, "theme", ()) or ())
    theme = themes[0] if themes else "未分类"
    qualification = _qualification_status(record)
    if qualification == "DIRECT_FACTOR_DAG":
        summary = "可作为受限 FactorDAG 草案种子；仍需 Campaign、PIT、Trial Ledger 和官方 Evidence。"
    elif qualification == "NEEDS_DETERMINISTIC_OPERATOR":
        summary = "静态元数据可浏览；需要新增经审计的确定性算子后才能进入 FactorDAG。"
    else:
        summary = "仅可作为辅助研究背景；旧 Python、bench 或 Qlib 结果不能伪装为官方 Evidence。"
    return {
        "factorId": getattr(record, "factor_id"),
        "displayName": f"{getattr(record, 'zoo', 'factor')} / {getattr(record, 'factor_id')}",
        "theme": theme,
        "eligibility": _eligibility(record),
        "qualificationStatus": qualification,
        "sourceFamily": _source_family(record),
        "compatibilityClass": _compatibility_class(record),
        "lineage": f"static_catalog -> {qualification.lower()}",
        "visibleSummary": summary,
        "actions": ["validate", "improve", "diagnose", "gap"],
    }


alphaforge_campaign_router = APIRouter()


@alphaforge_campaign_router.get("/veriquant/campaign/catalog")
async def get_campaign_catalog() -> dict[str, Any]:
    return {"status": "ok", **_state.service.catalog().to_dict()}


@alphaforge_campaign_router.get("/veriquant/alphaforge/status")
async def get_alphaforge_status() -> dict[str, Any]:
    admitted = (
        "alphaforge_chinese_ui",
        "campaign_api",
        "research_campaign",
        "factor_library_qualification",
        "workbench",
    )
    optional_disabled = ("real_ashare_extended", "real_llm_campaign_smoke")
    return {
        "status": "ok",
        "product": "AlphaForge（千因研究院）",
        "internal_kernel": "VeriQuant",
        "runtime_mode": "DETERMINISTIC",
        "authority": "campaign_draft_confirmation_and_read_only_factor_catalog",
        "admitted_ready_packages": [_parallel_status(package) for package in admitted],
        "optional_disabled_packages": [_parallel_status(package) for package in optional_disabled],
        "real_30x180_smoke": "DISABLED_OPTIONAL",
        "real_llm_smoke": "DISABLED_OPTIONAL",
        "routes": route_paths(),
        "trading_routes_registered": False,
    }


@alphaforge_campaign_router.get("/veriquant/campaign/llm/provider-status")
async def get_campaign_llm_provider_status() -> dict[str, Any]:
    return _llm_configuration_view()


@alphaforge_campaign_router.post("/veriquant/campaign/recommendations")
async def create_campaign_recommendation(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _draft_request_from_payload(_require_mapping(payload))
        response, draft = _state.service.create_draft(request)
        return _recommendation_view(draft, response.message, response.clarification_questions)
    except Exception as exc:  # noqa: BLE001 - converted to typed HTTP failure.
        raise _route_error(exc) from exc


@alphaforge_campaign_router.post("/veriquant/campaign/drafts")
async def create_campaign_draft(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _draft_request_from_payload(_require_mapping(payload))
        response, draft = _state.service.create_draft(request)
        if draft is None:
            raise HTTPException(status_code=422, detail=response.message)
        view = _draft_view(draft)
        _state.drafts[view["draftId"]] = draft
        return view
    except Exception as exc:  # noqa: BLE001 - converted to typed HTTP failure.
        raise _route_error(exc) from exc


@alphaforge_campaign_router.post("/veriquant/campaign/confirmations")
async def confirm_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        body = _require_mapping(payload)
        draft = _stored_draft(body)
        answers = body.get("answers", {})
        if not isinstance(answers, dict):
            raise HTTPException(status_code=400, detail="answers must be an object")
        _, campaign = _state.service.confirm(
            CampaignConfirmationRequest(
                draft=draft,
                confirmed_at=DEFAULT_DECISION_TIME,
                confirmed_by=Component.OPERATOR,
                answers=answers,
            )
        )
        _, intent, _ = _state.service.plan_run(campaign)
        campaign_id = _campaign_id(campaign)
        _state.campaigns[campaign_id] = campaign
        selected_desks = intent.selected_desks if intent is not None else campaign.candidate_agents[:3]
        run_view = _run_view(campaign, selected_desks)
        _state.runs[campaign_id] = run_view
        _state.runs[_draft_id(draft)] = run_view
        return _draft_view(draft, state=CampaignState.CONFIRMED.value, campaign_id=campaign_id)
    except Exception as exc:  # noqa: BLE001 - converted to typed HTTP failure.
        raise _route_error(exc) from exc


@alphaforge_campaign_router.get("/veriquant/campaign/runs/{campaign_id}")
async def get_campaign_run(campaign_id: str) -> dict[str, Any]:
    safe_id = _safe_campaign_id(campaign_id)
    stored = _result_json(safe_id)
    if stored is not None:
        run = _completed_run_view(safe_id, stored)
        _state.runs[safe_id] = run
        return run
    cached = _state.runs.get(safe_id)
    if cached is not None:
        return cached
    runtime = _latest_runtime_json(safe_id)
    if runtime is not None:
        run = (
            _runtime_blocked_run_view(safe_id, runtime, None)
            if _runtime_blocks_offline_projection(runtime)
            else _runtime_detached_run_view(safe_id, runtime, None)
        )
        _state.runs[safe_id] = run
        return run
    raise HTTPException(status_code=404, detail="campaign run not found")


@alphaforge_campaign_router.post("/veriquant/campaign/runs/{campaign_id}/start")
async def start_campaign_run(campaign_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    safe_id = _safe_campaign_id(campaign_id)
    runtime_mode = _runtime_mode_from_payload(payload)
    if safe_id not in _state.campaigns and safe_id not in _state.runs and _result_json(safe_id) is None:
        raise HTTPException(status_code=404, detail="campaign must be confirmed before start")
    campaign = _state.campaigns.get(safe_id)
    runtime_payload = _runtime_json(safe_id, runtime_mode)
    if campaign is not None:
        try:
            runtime_payload = _run_campaign_runtime(safe_id, campaign, runtime_mode=runtime_mode)
        except Exception as exc:  # noqa: BLE001 - fail closed without leaking local paths to the UI.
            failed = dict(_state.runs.get(safe_id, {"campaignId": safe_id, "selectedDesks": []}))
            failed.update(
                {
                    "state": "FAILED",
                    "runtimeMode": runtime_mode,
                    "currentCheckpoint": "Campaign 路由运行时失败",
                    "checkpointState": "REACHED",
                    "messages": [f"campaign routed runtime failed closed: {exc.__class__.__name__}"],
                }
            )
            _state.runs[safe_id] = failed
            return failed
        if _runtime_blocks_offline_projection(runtime_payload):
            run = _runtime_blocked_run_view(safe_id, runtime_payload or {}, campaign)
            _state.runs[safe_id] = run
            return run
        if runtime_mode == "REAL_LLM" or campaign.research_depth is ResearchDepth.QUICK_EXPLORATION:
            run = _runtime_only_run_view(safe_id, runtime_payload, campaign)
            _state.runs[safe_id] = run
            return run
    if _runtime_blocks_offline_projection(runtime_payload):
        run = _runtime_blocked_run_view(safe_id, runtime_payload or {}, campaign)
        _state.runs[safe_id] = run
        return run
    stored = _result_json(safe_id)
    if stored is None:
        try:
            stored = run_offline_acceptance(
                output_root=_campaign_run_output_root(safe_id),
                seed=7,
                workbench_root=_workbench_artifact_root(),
                run_id=safe_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed without leaking local paths to the UI.
            failed = dict(_state.runs.get(safe_id, {"campaignId": safe_id, "selectedDesks": []}))
            failed.update(
                {
                    "state": "FAILED",
                    "runtimeMode": runtime_mode,
                    "currentCheckpoint": "离线验收链失败",
                    "checkpointState": "REACHED",
                    "messages": [f"offline deterministic acceptance failed closed: {exc.__class__.__name__}"],
                }
            )
            _state.runs[safe_id] = failed
            return failed
    run = _completed_run_view(safe_id, stored, runtime_payload)
    _state.runs[safe_id] = run
    return run


@alphaforge_campaign_router.get("/veriquant/factor-library")
async def list_factor_library(limit: int = 461) -> dict[str, Any]:
    safe_limit = 461 if not isinstance(limit, int) or isinstance(limit, bool) else max(1, min(limit, 461))
    catalog = _catalog()
    report = FactorLibraryCatalogBuilder.report(catalog)
    items = [_factor_item(record) for record in catalog.entries[:safe_limit]]
    return {
        "schemaVersion": "alphaforge.factor-library-ui/v1",
        "status": "ok",
        "total": len(catalog.entries),
        "generatedFrom": "static_factor_library_qualification",
        "catalogHash": catalog.catalog_hash,
        "qualificationHash": report.report_hash,
        "items": items,
    }


def route_paths() -> list[str]:
    return [str(getattr(route, "path", route.get("path") if isinstance(route, dict) else "")) for route in getattr(alphaforge_campaign_router, "routes", [])]


def summarize_routes(paths: Iterable[str] | None = None) -> dict[str, Any]:
    selected = list(paths) if paths is not None else route_paths()
    return {
        "router_symbol": "alphaforge_campaign_router",
        "registered": False,
        "paths": selected,
        "authority": "campaign_draft_confirmation_and_read_only_factor_catalog",
        "real_30x180_smoke": "DEFERRED_UNTIL_STANDALONE_SUCCESS",
        "real_llm_smoke": "DEFERRED_UNTIL_STANDALONE_SUCCESS",
    }


__all__ = [
    "alphaforge_campaign_router",
    "create_campaign_draft",
    "create_campaign_recommendation",
    "confirm_campaign",
    "get_campaign_catalog",
    "get_campaign_run",
    "start_campaign_run",
    "list_factor_library",
    "reset_alphaforge_campaign_state",
    "route_paths",
    "summarize_routes",
]
