"""U6 Campaign controls plus read-only Research OS V3 result projection."""

from __future__ import annotations

import json
import atexit
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from src.veriquant.contracts import ContractValidationError
from src.veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from src.veriquant.integration.campaign_evidence_dataset import (
    CampaignEvidenceDatasetRepository,
)
from src.veriquant.integration.campaign_data_preparation import (
    CampaignDataPreparationService,
)
from src.veriquant.integration.capture_research_os_v2_data import _load_proxy_settings
from src.veriquant.approved_proxy_scale_adapter_v2 import (
    AllowlistedProxyScaleClient,
    ApprovedProxyScaleAdapterV2,
)
from src.veriquant.dynamic_acquisition import ResourceBudget
from src.veriquant.tushare_sdk_proxy import ProxySdkConfig, create_proxy_client
from src.veriquant.llm_desks.safety import canonical_hash
from src.veriquant.research_os_publication.models import require_safe_run_id
from src.veriquant.research_os_v3 import ResearchOSV3FormalReport, ResearchOSV3ReportReceipt
from src.veriquant.research_os_v3.product_service import ResearchOSV3ProductService
from src.veriquant.research_os_v3.tools import FrozenResearchContext
from src.veriquant.research_run_controller import ResearchRunManifest


REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "research_os_v3_u4"
REPORT_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "research_os_v3_u5" / "reports"
PRODUCT_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "research_os_v3_u6_final"
_V2_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "integration" / "research_os_v2"
_STRICT_PRODUCT_DATA = FrozenValidationDataRepository(
    REPO_ROOT / ".autobuild" / "veriquant" / "product_acceptance" / "repair24" / "real_capture_v2" / "data"
)
_ACTIVE_DATA = FrozenValidationDataRepository(_V2_ROOT / "data_active")
_BASE_DATA = FrozenValidationDataRepository(_V2_ROOT / "data")
_GATEWAY_DATA = CampaignEvidenceDatasetRepository(
    REPO_ROOT / ".autobuild" / "veriquant" / "research_data_gateway" / "campaign_datasets"
)
_CAMPAIGN_DATA = CampaignEvidenceDatasetRepository(
    REPO_ROOT / ".autobuild" / "veriquant" / "research_os_v3_campaign_data"
)
_CAMPAIGN_PREPARATION_ROOT = (
    REPO_ROOT / ".autobuild" / "veriquant" / "research_os_v3_data_preparation"
)


def _load_frozen_data():
    gateway = _GATEWAY_DATA.load("gateway-default")
    return (
        (gateway.frozen_data if gateway is not None else None)
        or _STRICT_PRODUCT_DATA.load(require_strict_provenance=True)
        or _ACTIVE_DATA.load()
        or _BASE_DATA.load()
    )


research_os_v3_product_service = ResearchOSV3ProductService(
    root=PRODUCT_ROOT,
    data_loader=_load_frozen_data,
    dotenv_path=REPO_ROOT / "agent" / ".env",
    campaign_data_repository=_CAMPAIGN_DATA,
)
atexit.register(research_os_v3_product_service.control.close)
research_os_v3_router = APIRouter(prefix="/api/veriquant/research-os/v3", tags=["AlphaForge Research OS V3"])
research_os_v3_control_router = APIRouter(prefix="/api/veriquant/research-os/v3", tags=["AlphaForge Research OS V3 Controls"])


def configure_research_os_v3_roots(*, run_root: str | Path, report_root: str | Path) -> None:
    global RUN_ROOT, REPORT_ROOT
    RUN_ROOT = Path(run_root).resolve()
    REPORT_ROOT = Path(report_root).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="研究产物不存在") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="研究产物损坏，已关闭读取") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="研究产物格式无效")
    return payload


def _safe_run_dir(root: Path, run_id: str) -> Path:
    try:
        require_safe_run_id(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="运行标识无效") from exc
    resolved = root.resolve()
    target = (resolved / run_id).resolve()
    if target == resolved or resolved not in target.parents:
        raise HTTPException(status_code=400, detail="运行标识无效")
    return target


def _public_report(
    report: ResearchOSV3FormalReport,
    *,
    verified_report_hash: str | None = None,
) -> dict[str, Any]:
    outcome = {
        "EVALUATED_RESEARCH_RESULT": "部分结果",
        "DRAFT_RESEARCH_RESULT": "部分结果",
        "NEGATIVE_RESEARCH_RESULT": "负向结果",
        "DATA_OR_CAPABILITY_GAP_REPORT": "数据或能力缺口",
        "PARTIAL_RESEARCH_RESULT": "部分结果",
        "SYSTEM_FAILURE_REPORT": "系统故障",
        "CANCELLED_RESEARCH_REPORT": "已取消",
    }[report.outcome.value]
    report_hash = verified_report_hash or report.artifact_hash
    return {
        "title": "AlphaForge 多专业研究报告",
        "question": report.question,
        "conclusion": report.executive_answer,
        "executive_answer": report.executive_answer,
        "outcome": outcome,
        "canonical_outcome": report.outcome.value,
        "data_summary": f"{report.source_label}；{report.data_start} 至 {report.data_end}",
        "source_label": report.source_label,
        "data_start": report.data_start,
        "data_end": report.data_end,
        "model_id": report.model_id,
        "machine_conclusion": "官方可见机器证据已由确定性服务生成。" if report.official_evidence_hash else "候选未形成可公开的官方机器证据，原因已记录。" if report.machine_evidence_status != "NOT_RUN" else "尚未执行正式机器证据计算；当前只完成受限因子表达式预检。",
        "machine_evidence_status": report.machine_evidence_status,
        "candidate_count": report.candidate_count,
        "accepted_preflight_count": report.accepted_preflight_count,
        "evaluated_candidate_count": report.evaluated_candidate_count,
        "rejected_candidate_count": report.rejected_candidate_count,
        "best_candidate_hash": report.best_candidate_hash,
        "selected_candidate_hashes": list(report.selected_candidate_hashes),
        "candidates": [item.to_dict() for item in report.candidates],
        "limitations": report.limitations,
        "next_actions": report.next_actions,
        "report_hash": report_hash,
    }


_REPORT_SCHEMA_V1 = "alphaforge.research-os-v3-report/v1"
_REPORT_SCHEMA_V2 = "alphaforge.research-os-v3-report/v2"


def _upgrade_legacy_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project immutable v1 reports into the current read-only v2 contract.

    The upgrade adds only fields whose v2 defaults explicitly mean "not run" or
    "legacy FactorDAG".  It never invents Evidence, selection, or Promotion.
    """

    if payload.get("schema_version") != _REPORT_SCHEMA_V1:
        return payload
    upgraded = dict(payload)
    upgraded["schema_version"] = _REPORT_SCHEMA_V2
    upgraded.setdefault("selection_hash", None)
    upgraded.setdefault("best_candidate_hash", None)
    upgraded.setdefault("selected_candidate_hashes", [])
    upgraded.setdefault("evaluated_candidate_count", 0)
    upgraded.setdefault("rejected_candidate_count", 0)
    groups: list[dict[str, Any]] = []
    for raw_group in payload.get("groups", []):
        if not isinstance(raw_group, dict):
            raise HTTPException(status_code=409, detail="旧研究报告分组格式无效")
        group = dict(raw_group)
        group["schema_version"] = _REPORT_SCHEMA_V2
        groups.append(group)
    candidates: list[dict[str, Any]] = []
    for raw_candidate in payload.get("candidates", []):
        if not isinstance(raw_candidate, dict):
            raise HTTPException(status_code=409, detail="旧研究报告候选格式无效")
        candidate = dict(raw_candidate)
        candidate["schema_version"] = _REPORT_SCHEMA_V2
        candidate.setdefault("formula_program_hash", None)
        candidate.setdefault("formula_program_kind", "LEGACY_FACTOR_DAG")
        candidate.setdefault("formula_manifest_hash", None)
        candidate.setdefault("selection_verdict", "NOT_EVALUATED")
        candidate.setdefault("selection_score", None)
        candidate.setdefault("selection_reason_codes", [])
        metrics: list[dict[str, Any]] = []
        for raw_metric in candidate.get("official_metrics", []):
            if not isinstance(raw_metric, dict):
                raise HTTPException(status_code=409, detail="旧研究报告指标格式无效")
            metric = dict(raw_metric)
            metric["schema_version"] = _REPORT_SCHEMA_V2
            metrics.append(metric)
        candidate["official_metrics"] = metrics
        candidates.append(candidate)
    upgraded["groups"] = groups
    upgraded["candidates"] = candidates
    return upgraded


def _parse_report(payload: dict[str, Any]) -> ResearchOSV3FormalReport:
    return ResearchOSV3FormalReport.from_payload(_upgrade_legacy_report_payload(payload))


def _parse_receipt(payload: dict[str, Any]) -> ResearchOSV3ReportReceipt:
    if payload.get("schema_version") == _REPORT_SCHEMA_V1:
        payload = {**payload, "schema_version": _REPORT_SCHEMA_V2}
    return ResearchOSV3ReportReceipt.from_payload(payload)


def _legacy_run_envelope(run_id: str) -> dict[str, Any]:
    publication = _safe_run_dir(REPORT_ROOT, run_id)
    raw_report = _read_json(publication / "machine_report.json")
    report = _parse_report(raw_report)
    verified_report_hash = canonical_hash(raw_report)
    raw_receipt = _read_json(publication / "receipt.json")
    receipt = _parse_receipt(raw_receipt)
    manifest_path = _safe_run_dir(RUN_ROOT / "research_runs", run_id) / "manifest.json"
    manifest = ResearchRunManifest.from_payload(_read_json(manifest_path))
    smoke = _read_json(RUN_ROOT / "u4_smoke_result.json")
    raw_result = smoke.get("result")
    if not isinstance(raw_result, dict) or raw_result.get("run_id") != run_id:
        raise HTTPException(status_code=409, detail="运行结果与报告身份不一致")
    if manifest.report_hash != verified_report_hash or receipt.report_hash != verified_report_hash:
        raise HTTPException(status_code=409, detail="报告与运行清单身份不一致")
    result = dict(raw_result)
    result["manifest_hash"] = manifest.artifact_hash
    return {
        "schema_version": "alphaforge.research-os-v3-public/v1",
        "result": result,
        "source_label": smoke.get("source_label"),
        "provider": smoke.get("provider"),
        "model": smoke.get("model"),
        "report": _public_report(report, verified_report_hash=verified_report_hash),
        "report_receipt_hash": canonical_hash(raw_receipt),
        "canonical_manifest_hash": manifest.artifact_hash,
        "authority": "DRAFT_ONLY",
    }


def _product_run_envelope(run_id: str) -> dict[str, Any]:
    publication = _safe_run_dir(research_os_v3_product_service.report_root, run_id)
    raw_report = _read_json(publication / "machine_report.json")
    report = _parse_report(raw_report)
    verified_report_hash = canonical_hash(raw_report)
    raw_receipt = _read_json(publication / "receipt.json")
    receipt = _parse_receipt(raw_receipt)
    manifest = research_os_v3_product_service.status(run_id)
    result = research_os_v3_product_service.load_result(run_id)
    context = FrozenResearchContext.from_payload(_read_json(_safe_run_dir(PRODUCT_ROOT / "products", run_id) / "context.json"))
    if manifest.report_hash != verified_report_hash or receipt.report_hash != verified_report_hash:
        raise HTTPException(status_code=409, detail="报告与运行清单身份不一致")
    raw_result = result.to_dict()
    raw_result["manifest_hash"] = manifest.artifact_hash
    raw_result["official_evidence_hash"] = manifest.official_evidence_hash
    return {
        "schema_version": "alphaforge.research-os-v3-public/v1",
        "result": raw_result,
        "source_label": context.source_label,
        "provider": manifest.provider_id,
        "model": manifest.model_id,
        "report": _public_report(report, verified_report_hash=verified_report_hash),
        "report_receipt_hash": canonical_hash(raw_receipt),
        "canonical_manifest_hash": manifest.artifact_hash,
        "authority": "OFFICIAL_VISIBLE_EVIDENCE" if manifest.official_evidence_hash else "DRAFT_ONLY",
    }


def _run_envelope(run_id: str) -> dict[str, Any]:
    product_publication = _safe_run_dir(research_os_v3_product_service.report_root, run_id)
    product_manifest = _safe_run_dir(
        research_os_v3_product_service.run_root / "research_runs", run_id
    ) / "manifest.json"
    if product_publication.exists() or product_manifest.exists():
        return _product_run_envelope(run_id)
    return _legacy_run_envelope(run_id)


def _control_call(call):
    try:
        return call()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="研究运行不存在") from exc
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=f"{exc.code.value}: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _specialist_status_summaries(manifest: ResearchRunManifest) -> list[dict[str, object]]:
    run_root = research_os_v3_product_service.run_root / "research_runs" / manifest.run_id
    summaries: list[dict[str, object]] = []
    messages = {
        "DATA_GAP": "当前批准数据或已登记可见证据不足，本组未启动模型。",
        "BUDGET_BLOCKED": "本组预算不足以完成最低研究步骤，未启动模型。",
        "CANCELLED": "本组已按用户请求取消。",
        "FAILED": "本组触发受控能力边界或运行异常，已安全停止。",
    }
    for specialist_id in manifest.selected_group_ids:
        commit_path = run_root / "branches" / specialist_id / "commit.json"
        if not commit_path.exists():
            summaries.append({
                "specialist_id": specialist_id,
                "status": "RUNNING" if manifest.current_stage.value == "RESEARCHING" else "WAITING",
                "candidate_count": 0,
                "typed_failures": [],
                "missing_capabilities": [],
                "message": "本组正在执行有界研究步骤。" if manifest.current_stage.value == "RESEARCHING" else "本组尚未启动。",
            })
            continue
        envelope = _read_json(commit_path)
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or envelope.get("payload_hash") != canonical_hash(payload):
            raise HTTPException(status_code=409, detail="研究组状态产物损坏，已关闭读取")
        status = str(payload.get("status") or "FAILED")
        refs = payload.get("candidate_refs")
        failures = payload.get("typed_failures")
        missing = payload.get("missing_capabilities")
        candidate_count = len(refs) if isinstance(refs, list) else 0
        if status == "COMPLETED_DRAFT":
            message = f"本组已提交 {candidate_count} 个候选，明细已进入正式报告。"
        elif status == "DATA_GAP" and isinstance(missing, list) and "visible_evidence" in missing:
            message = "当前没有可供组合增量研究使用的已登记可见证据，本组未启动模型。"
        elif status == "FAILED" and isinstance(failures, list) and "UNAUTHORIZED_CAPABILITY" in failures:
            message = "研究输出触发能力边界，已安全停止；未授予额外权限。"
        else:
            message = messages.get(status, "本组状态已记录，可在正式报告中查看明细。")
        summaries.append({
            "specialist_id": specialist_id,
            "status": status,
            "candidate_count": candidate_count,
            "typed_failures": failures if isinstance(failures, list) else [],
            "missing_capabilities": missing if isinstance(missing, list) else [],
            "message": message,
        })
    return summaries


@research_os_v3_control_router.post("/plans")
async def create_research_os_v3_plan(payload: dict[str, Any]) -> dict[str, object]:
    return _control_call(lambda: research_os_v3_product_service.create_plan(payload))


@research_os_v3_control_router.post("/plans/{plan_id}/confirm")
async def confirm_research_os_v3_plan(
    plan_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, object]:
    raw_answers = (payload or {}).get("answers")
    if raw_answers is not None and not isinstance(raw_answers, dict):
        raise HTTPException(status_code=422, detail="answers 必须是字段到显式确认值的对象")
    answers = None if raw_answers is None else {
        str(key): str(value) for key, value in raw_answers.items()
    }
    return _control_call(
        lambda: research_os_v3_product_service.confirm_plan(plan_id, answers=answers)
    )


@research_os_v3_control_router.post("/plans/{plan_id}/prepare-data")
def prepare_research_plan_data(plan_id: str, payload: dict[str, Any]):
    """Explicit operator action; default requests cannot access the network."""

    if payload.get("allowNetwork") is not True:
        raise HTTPException(
            status_code=400,
            detail="准备真实研究数据必须显式确认 allowNetwork=true",
        )
    end_date = str(payload.get("endDate") or "").strip()
    if not end_date:
        raise HTTPException(status_code=400, detail="缺少 endDate")

    def prepare():
        settings = _load_proxy_settings(REPO_ROOT / "agent" / ".env")
        sdk = create_proxy_client(ProxySdkConfig(
            endpoint=settings["TUSHARE_PROXY_URL"],
            token=settings["TUSHARE_TOKEN"],
        ))
        client = AllowlistedProxyScaleClient(sdk)
        result = CampaignDataPreparationService(
            root=_CAMPAIGN_PREPARATION_ROOT,
            scale_adapter=ApprovedProxyScaleAdapterV2(client),
            dynamic_transport=client,
        ).prepare(
            product_service=research_os_v3_product_service,
            plan_id=plan_id,
            end_date=end_date,
            capture_time=datetime.now(timezone.utc),
            index_code=(
                str(payload["indexCode"])
                if payload.get("indexCode") is not None
                else None
            ),
            target_symbols=(
                int(payload["targetSymbols"])
                if payload.get("targetSymbols") is not None
                else None
            ),
            max_acquisition_symbols=(
                int(payload["maxAcquisitionSymbols"])
                if payload.get("maxAcquisitionSymbols") is not None
                else None
            ),
            allow_network=True,
            resource_budget=ResourceBudget(
                max_api_calls=(
                    int(payload["maxApiCalls"])
                    if payload.get("maxApiCalls") is not None
                    else None
                ),
                max_rows=(
                    int(payload["maxRows"])
                    if payload.get("maxRows") is not None
                    else None
                ),
                max_storage_bytes=(
                    int(payload["maxStorageBytes"])
                    if payload.get("maxStorageBytes") is not None
                    else None
                ),
            ),
        )
        response = asdict(result)
        response["status"] = result.status.value
        response["plan"] = research_os_v3_product_service.plan_view(plan_id)
        return response

    return _control_call(prepare)


@research_os_v3_control_router.post("/runs")
async def start_research_os_v3_run(payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    return _control_call(lambda: research_os_v3_product_service.start(plan_id).to_dict())


@research_os_v3_control_router.get("/runs/{run_id}/status")
async def get_research_os_v3_status(run_id: str) -> dict[str, Any]:
    manifest = _control_call(lambda: research_os_v3_product_service.status(run_id))
    payload = manifest.to_dict()
    payload["specialist_summaries"] = _specialist_status_summaries(manifest)
    return payload


@research_os_v3_control_router.post("/runs/{run_id}/cancel")
async def cancel_research_os_v3_run(run_id: str) -> dict[str, Any]:
    return _control_call(lambda: research_os_v3_product_service.cancel(run_id).to_dict())


@research_os_v3_control_router.post("/runs/{run_id}/resume")
async def resume_research_os_v3_run(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    return _control_call(lambda: research_os_v3_product_service.resume(run_id, plan_id).to_dict())


@research_os_v3_router.get("/runs")
async def list_research_os_v3_runs() -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    roots = (research_os_v3_product_service.report_root, REPORT_ROOT)
    seen: set[str] = set()
    for root in roots:
      if root.exists():
        for item in sorted(root.iterdir(), key=lambda path: path.name):
            if not item.is_dir():
                continue
            if item.name in seen:
                continue
            try:
                envelope = _run_envelope(item.name)
            except HTTPException:
                continue
            seen.add(item.name)
            result = envelope["result"]
            runs.append({
                "run_id": result["run_id"],
                "status": result["status"],
                "candidate_count": result["candidate_count"],
                "accepted_preflight_count": result["accepted_preflight_count"],
                "report_hash": envelope["report"]["report_hash"],
                "manifest_hash": envelope["canonical_manifest_hash"],
            })
    return {"runs": runs}


@research_os_v3_router.get("/runs/{run_id}")
async def get_research_os_v3_run(run_id: str) -> dict[str, Any]:
    return _run_envelope(run_id)


@research_os_v3_router.get("/runs/{run_id}/report")
async def get_research_os_v3_report(run_id: str) -> dict[str, Any]:
    return _run_envelope(run_id)["report"]


@research_os_v3_router.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
async def get_research_os_v3_markdown(run_id: str) -> str:
    product_path = _safe_run_dir(research_os_v3_product_service.report_root, run_id) / "report.md"
    path = product_path if product_path.exists() else _safe_run_dir(REPORT_ROOT, run_id) / "report.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="研究报告不存在") from exc


def research_os_v3_route_paths() -> tuple[str, ...]:
    return tuple(str(route.path) for route in research_os_v3_router.routes)


def research_os_v3_control_route_paths() -> tuple[str, ...]:
    return tuple(str(route.path) for route in research_os_v3_control_router.routes)


__all__ = [
    "configure_research_os_v3_roots",
    "research_os_v3_route_paths",
    "research_os_v3_control_route_paths",
    "research_os_v3_control_router",
    "research_os_v3_router",
    "research_os_v3_product_service",
]
