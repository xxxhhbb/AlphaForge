"""AlphaForge-specific context strategy for a reused Vibe AgentLoop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, freeze_mapping, require_identifier, require_text
from .contracts import ResearchAgentTask


@dataclass(frozen=True)
class SpecialistPromptPolicy(CanonicalContract):
    policy_id: str = "alphaforge-specialist-draft-v1"
    output_contract: str = "ALPHAFORGE_RESEARCH_DRAFT_V1"
    prohibited_capabilities: tuple[str, ...] = (
        "broker",
        "ledger",
        "official_metrics",
        "orders",
        "promotion",
        "risk_approval",
        "sealed",
    )
    SCHEMA_VERSION: ClassVar[str] = "alphaforge.specialist-prompt-policy/v1"

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        require_identifier(self.output_contract, "output_contract")
        if tuple(sorted(self.prohibited_capabilities)) != self.prohibited_capabilities:
            fail(FailureCode.INVALID_VALUE, "prohibited_capabilities must be stable-sorted")
        for item in self.prohibited_capabilities:
            require_identifier(item, "prohibited_capabilities")


class VeriQuantResearchContextBuilder:
    """Build a deterministic policy envelope passed to the injected AgentLoop."""

    def __init__(self, policy: SpecialistPromptPolicy | None = None) -> None:
        self.policy = policy or SpecialistPromptPolicy()

    def build(
        self,
        task: ResearchAgentTask,
        *,
        tool_definitions: Sequence[Mapping[str, object]],
        upstream_context: Mapping[str, str],
    ) -> str:
        allowed = tuple(sorted(str(item.get("name", "")) for item in tool_definitions))
        if allowed != task.allowed_tools:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "runtime tools differ from task whitelist")
        frozen_upstream = freeze_mapping(upstream_context, "upstream_context")
        for key in frozen_upstream:
            require_identifier(key, "upstream_context")
        payload = {
            "contract": self.policy.output_contract,
            "policy_id": self.policy.policy_id,
            "authority": "DRAFT_ONLY",
            "specialist_id": task.specialist_id,
            "question": task.question,
            "decision_time": task.decision_time.isoformat(),
            "allowed_tools": allowed,
            "upstream_context": dict(sorted(frozen_upstream.items())),
            "required_process": [
                "plan",
                "investigate_market_and_schema",
                "investigate_specialist_sources",
                "record_data_gaps",
                "select_transparent_proxies_when_reasonable",
                "draft_multiple_experiments",
                "self_review",
            ],
            "prohibited_capabilities": self.policy.prohibited_capabilities,
            "required_output": {
                "plan": (
                    "customer-facing candidate definition with exact fields, transformations, direction, "
                    "observation window, forecast horizon, normalization, rationale, and proxy limitations"
                ),
                "investigations": "array of tool receipt objects",
                "data_gaps": (
                    "array of {concept,reason_code,requested_fields,attempted_tools,detail}; "
                    "use [] when no gap remains"
                ),
                "proxy_specs": (
                    "array of {requested_concept,proxy_name,source_fields,rationale,limitations}; "
                    "a proxy must be disclosed and remains DRAFT"
                ),
                "code_drafts": (
                    f"array of {task.minimum_experiments} to {task.max_experiments} complete inert source strings; prefer compact programs under "
                    "200 lines each so the provider can finish the JSON response"
                ),
                "status": "COMPLETED or PARTIAL",
                "authority": "DRAFT_ONLY",
            },
            "answer_requirements": {
                "requirement_ids": task.answer_requirement_ids,
                "required_semantics": task.required_answer_semantics,
                "comparison_required": task.comparison_required,
            },
        }
        require_text(task.question, "question")
        return (
            "You are an AlphaForge specialist research agent running inside the real Vibe AgentLoop. "
            "Plan first, use the available read-only tools as needed, investigate missing concepts, "
            "choose and disclose reasonable proxies when exact data is unavailable, and submit multiple "
            "independent experiment drafts when the budget permits. Obey this machine-owned policy envelope. "
            "Never claim official evidence or promotion. Return one JSON object only.\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )


class AgentLoopResearchContext:
    """Minimal ``AgentLoop`` context that never loads Vibe's global prompt."""

    def __init__(self, system_prompt: str) -> None:
        require_text(system_prompt, "system_prompt")
        self.system_prompt = system_prompt

    def build_messages(
        self,
        user_message: str,
        history: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        require_text(user_message, "user_message")
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for item in history or ():
            role = str(item.get("role") or "")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                fail(FailureCode.INVALID_VALUE, "research history contains an invalid message")
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages


def research_context_factory(system_prompt: str):
    """Return the injectable factory expected by Vibe ``AgentLoop``."""

    def factory(_registry, _memory, *, persistent_memory=None):
        del _registry, _memory, persistent_memory
        return AgentLoopResearchContext(system_prompt)

    return factory


__all__ = [
    "AgentLoopResearchContext",
    "SpecialistPromptPolicy",
    "VeriQuantResearchContextBuilder",
    "research_context_factory",
]
