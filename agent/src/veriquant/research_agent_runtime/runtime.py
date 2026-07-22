"""Adapter that reuses an injected Vibe AgentLoop without copying its ReAct loop."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Protocol

from ..llm_desks.safety import canonical_hash
from .checkpoint import ResearchCancellationToken
from .context import VeriQuantResearchContextBuilder
from .contracts import (
    ResearchDataGap,
    ResearchAgentFailureCode,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    ResearchAgentTask,
    ResearchDraftArtifact,
    ResearchDraftKind,
    ResearchProxySpec,
)
from .toolbox import ResearchToolboxProtocol


_FORBIDDEN_OUTPUT_KEYS = {
    "ledger",
    "official_evidence",
    "official_metrics",
    "orders",
    "portfolio",
    "promotion",
    "risk_approval",
    "sealed",
    "target_weights",
}


class VibeLoopProtocol(Protocol):
    def run(self, user_message: str, history=None, session_id: str = "") -> Mapping[str, Any]: ...

    def cancel(self) -> None: ...


class VibeLoopFactory(Protocol):
    def __call__(
        self,
        *,
        task: ResearchAgentTask,
        toolbox: ResearchToolboxProtocol,
        event_callback: Callable[[str, Mapping[str, Any]], None] | None,
    ) -> VibeLoopProtocol: ...


class DraftSourceStoreProtocol(Protocol):
    """Content-addressed sink for model-authored source."""

    def put_bytes(self, payload: bytes, *, media_type: str = "application/octet-stream") -> Any: ...


class ResearchAgentRuntimeProtocol(Protocol):
    def run(
        self,
        *,
        task: ResearchAgentTask,
        toolbox: ResearchToolboxProtocol,
        upstream_context: Mapping[str, str],
        cancellation: ResearchCancellationToken,
    ) -> ResearchAgentOutcome: ...


class VibeAgentLoopAdapter:
    """DRAFT-only boundary around a caller-supplied real Vibe ``AgentLoop``."""

    supports_review_revision = True

    def __init__(
        self,
        *,
        loop_factory: VibeLoopFactory,
        context_builder: VeriQuantResearchContextBuilder | None = None,
        event_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
        draft_store: DraftSourceStoreProtocol | None = None,
    ) -> None:
        self.loop_factory = loop_factory
        self.context_builder = context_builder or VeriQuantResearchContextBuilder()
        self.event_callback = event_callback
        self.draft_store = draft_store

    def run(
        self,
        *,
        task: ResearchAgentTask,
        toolbox: ResearchToolboxProtocol,
        upstream_context: Mapping[str, str],
        cancellation: ResearchCancellationToken,
    ) -> ResearchAgentOutcome:
        if cancellation.cancelled:
            return _failure(task, ResearchAgentStatus.CANCELLED, ResearchAgentFailureCode.CANCELLED, "CANCELLED_BEFORE_START")
        prompt = self.context_builder.build(
            task,
            tool_definitions=toolbox.definitions,
            upstream_context=upstream_context,
        )
        try:
            loop = self.loop_factory(task=task, toolbox=toolbox, event_callback=self.event_callback)
            cancellation.register(loop.cancel)
            raw = loop.run(prompt, history=None, session_id=f"{task.run_id}-{task.task_id}")
            if cancellation.cancelled or str(raw.get("status", "")).casefold() == "cancelled":
                return _failure(task, ResearchAgentStatus.CANCELLED, ResearchAgentFailureCode.CANCELLED, "COOPERATIVE_CANCEL")
            if str(raw.get("status", "")).casefold() not in {"success", "completed"}:
                return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.PROVIDER_FAILED, "AGENT_LOOP_TERMINAL")
            payload = json.loads(str(raw.get("content") or ""))
            if not isinstance(payload, dict) or _contains_forbidden_key(payload):
                return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.INVALID_MODEL_OUTPUT, "AUTHORITY_PAYLOAD")
            if payload.get("authority") != "DRAFT_ONLY":
                return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.INVALID_MODEL_OUTPUT, "AUTHORITY_PAYLOAD")
            plan = payload.get("plan")
            if not isinstance(plan, str) or not plan.strip():
                return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.INVALID_MODEL_OUTPUT, "MISSING_PLAN")
            if self.draft_store is None:
                return _failure(
                    task,
                    ResearchAgentStatus.FAILED,
                    ResearchAgentFailureCode.ARTIFACT_STORE_UNAVAILABLE,
                    "DRAFT_SOURCE_STORE_MISSING",
                )
            plan_handle = self.draft_store.put_json({"plan": plan.strip()})
            requested_status = str(payload.get("status") or "PARTIAL")
            status = ResearchAgentStatus.COMPLETED if requested_status == "COMPLETED" else ResearchAgentStatus.PARTIAL
            drafts = [
                ResearchDraftArtifact(
                    kind=ResearchDraftKind.PLAN,
                    content_hash=plan_handle.handle_id,
                    media_type="application/json",
                )
            ]
            data_gaps = _parse_data_gaps(payload.get("data_gaps", ()))
            proxy_specs = _parse_proxy_specs(payload.get("proxy_specs", ()))
            for source in payload.get("code_drafts", ()):
                if not isinstance(source, str):
                    return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.INVALID_MODEL_OUTPUT, "INVALID_CODE_DRAFT")
                handle = self.draft_store.put_bytes(source.encode("utf-8"), media_type="text/x-python-draft")
                handle_id = getattr(handle, "handle_id", None)
                if not isinstance(handle_id, str) or not handle_id.startswith("sha256:"):
                    return _failure(
                        task,
                        ResearchAgentStatus.FAILED,
                        ResearchAgentFailureCode.RUNTIME_FAILED,
                        "DRAFT_SOURCE_STORE_INVALID",
                    )
                drafts.append(
                    ResearchDraftArtifact(
                        kind=ResearchDraftKind.CODE,
                        content_hash=handle_id,
                        media_type="text/x-python-draft",
                    )
                )
            receipts = toolbox.recorded_receipts
            if not receipts and status is ResearchAgentStatus.COMPLETED:
                status = ResearchAgentStatus.PARTIAL
            return ResearchAgentOutcome(
                task_id=task.task_id,
                task_hash=task.artifact_hash,
                specialist_id=task.specialist_id,
                status=status,
                plan_hash=plan_handle.handle_id,
                investigation_receipts=receipts,
                draft_artifacts=tuple(sorted(drafts, key=lambda item: (item.kind.value, item.content_hash))),
                provider_calls=max(1, int(raw.get("iterations") or 1)),
                tool_calls=len(receipts),
                data_gaps=data_gaps,
                proxy_specs=proxy_specs,
            )
        except Exception:
            return _failure(task, ResearchAgentStatus.FAILED, ResearchAgentFailureCode.RUNTIME_FAILED, "RUNTIME_EXCEPTION")


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_OUTPUT_KEYS or _contains_forbidden_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _parse_data_gaps(value: object) -> tuple[ResearchDataGap, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("data_gaps must be an array")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("data gap must be an object")
        result.append(ResearchDataGap(
            concept=str(item.get("concept") or "").strip(),
            reason_code=str(item.get("reason_code") or "").strip().upper(),
            requested_fields=_identifiers(item.get("requested_fields", ())),
            attempted_tools=_identifiers(item.get("attempted_tools", ())),
            detail=str(item.get("detail") or "").strip(),
        ))
    return tuple(sorted(result, key=lambda item: (item.concept, item.reason_code)))


def _parse_proxy_specs(value: object) -> tuple[ResearchProxySpec, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("proxy_specs must be an array")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("proxy spec must be an object")
        limitations = item.get("limitations", ())
        if not isinstance(limitations, (list, tuple)):
            raise ValueError("proxy limitations must be an array")
        result.append(ResearchProxySpec(
            requested_concept=str(item.get("requested_concept") or "").strip(),
            proxy_name=str(item.get("proxy_name") or "").strip(),
            source_fields=_identifiers(item.get("source_fields", ())),
            rationale=str(item.get("rationale") or "").strip(),
            limitations=tuple(sorted(str(value).strip() for value in limitations if str(value).strip())),
        ))
    return tuple(sorted(result, key=lambda item: (item.requested_concept, item.proxy_name)))


def _identifiers(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("identifier collection must be an array")
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


def _failure(
    task: ResearchAgentTask,
    status: ResearchAgentStatus,
    code: ResearchAgentFailureCode,
    origin: str,
) -> ResearchAgentOutcome:
    return ResearchAgentOutcome(
        task_id=task.task_id,
        task_hash=task.artifact_hash,
        specialist_id=task.specialist_id,
        status=status,
        plan_hash=None,
        investigation_receipts=(),
        draft_artifacts=(),
        provider_calls=0,
        tool_calls=0,
        failure_code=code,
        failure_origin=origin,
    )


__all__ = [
    "DraftSourceStoreProtocol",
    "ResearchAgentRuntimeProtocol",
    "VibeAgentLoopAdapter",
    "VibeLoopFactory",
    "VibeLoopProtocol",
]
