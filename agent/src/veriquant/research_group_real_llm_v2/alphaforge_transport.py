"""Explicit opt-in AlphaForge OpenAI-compatible transport.

Construction may load ``agent/.env`` through the existing narrow loader, but
credentials never enter requests, traces, reprs, or serialized artifacts.
No network call occurs until ``complete_group_proposal`` is invoked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..alphaforge_llm import AlphaForgeLLMConfigLoader, AlphaForgeLLMConfiguration
from ..contracts import ContractValidationError, FailureCode
from ..real_llm_provider.client import OpenAICompatibleHTTPClient, ProviderFailure
from .models import GroupLLMCallRequest, GroupLLMCallResponse


class AlphaForgeRealLLMTransport:
    """Real smoke hook backed by the already-approved provider configuration."""

    def __init__(self, *, configuration: AlphaForgeLLMConfiguration, client: Any | None = None) -> None:
        self._configuration = configuration
        self.available = bool(
            configuration.enabled
            and configuration.settings is not None
            and configuration.runtime_credential
        )
        self._client = client
        if self.available and self._client is None:
            assert configuration.settings is not None
            self._client = OpenAICompatibleHTTPClient(
                settings=configuration.settings,
                runtime_credential=configuration.runtime_credential,
            )

    @classmethod
    def from_dotenv(cls, path: Path = Path("agent/.env"), *, client: Any | None = None) -> "AlphaForgeRealLLMTransport":
        configuration = AlphaForgeLLMConfigLoader(dotenv_path=path).load()
        return cls(configuration=configuration, client=client)

    @property
    def public_view(self) -> Mapping[str, object]:
        return self._configuration.public_view()

    def __repr__(self) -> str:
        public = self.public_view
        return f"AlphaForgeRealLLMTransport(enabled={public.get('enabled')!r}, available={self.available!r})"

    def complete_group_proposal(self, request: GroupLLMCallRequest) -> GroupLLMCallResponse:
        if not self.available or self._client is None or self._configuration.settings is None:
            raise ContractValidationError(FailureCode.ADAPTER_UNAVAILABLE, "AlphaForge real LLM is unavailable")
        prompt = _messages(request)
        settings = self._configuration.settings
        try:
            response = self._client.complete_json(
                messages=prompt,
                model=settings.model,
                timeout_seconds=min(float(request.policy.timeout_seconds), float(settings.timeout_seconds)),
                max_tokens=request.policy.max_tokens,
                temperature=float(settings.temperature),
                top_p=float(settings.top_p),
                response_format="json_object",
                extra={},
            )
        except ProviderFailure as exc:
            raise ContractValidationError(exc.code, "AlphaForge provider call failed") from exc
        return GroupLLMCallResponse(
            payload=response.payload,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )


def _messages(request: GroupLLMCallRequest) -> tuple[Mapping[str, str], ...]:
    expected_lookback = request.intent.factor_lookback_sessions or 20
    allowed_operators = (
        "field",
        "lag",
        "rolling_mean",
        "rolling_std",
        "pct_change",
        "subtract",
        "add",
        "multiply",
        "divide_safe",
        "winsorize",
        "rank_cross_section",
        "zscore_cross_section",
        "neutralize_by_group",
    )
    system = {
        "role": "system",
        "content": (
            "You are one selected AlphaForge specialist research group. Return strict JSON only. "
            "Produce only DRAFT hypotheses and inert FactorDAG expressions using the exact output schema. "
            "Use only the listed fields, operators, templates, and read-only capabilities. "
            "Never invent derived field names such as momentum, returns, volatility, adv, beta, "
            "industry, regime, or market_return unless that exact name is listed in allowed_fields. "
            "The variables array must contain exactly the raw allowed_fields referenced by the expression. "
            "Do not add fields or actions."
        ),
    }
    material = {
        "question": request.intent.question,
        "group_id": request.context.group_id,
        "state": "DRAFT",
        "required_candidate_count": {"minimum": 1, "maximum": request.policy.candidate_budget},
        "required_lookback_sessions": expected_lookback,
        "allowed_fields": request.policy.allowed_fields,
        "allowed_operators": allowed_operators,
        "safe_expression_templates": _safe_expression_templates(
            request.policy.allowed_fields,
            expected_lookback,
        ),
        "read_only_tools": request.context.tool_whitelist,
        "visible_context_keys": request.context.allowed_context_keys,
        "output_schema": {
            "state": "DRAFT",
            "group_id": request.context.group_id,
            "hypothesis": "string",
            "economic_mechanism": "string",
            "falsification_tests": ["string"],
            "candidates": [
                {
                    "name": "identifier",
                    "expression": "FactorDAG expression string",
                    "variables": ["allowed_field"],
                    "lookback_sessions": expected_lookback,
                    "rationale": "string",
                }
            ],
            "runtime_mode": "REAL_LLM",
        },
        "repair": None
        if request.attempt_number == 1
        else {
            "typed_failure_code": request.repair_code,
            "prior_payload_hash": request.prior_payload_hash,
            "instruction": (
                "Repair only schema or FactorDAG expression. Preserve hypothesis intent and "
                "requested lookback. Copy one safe_expression_template when possible. "
                "Remove every field/operator not present in the explicit allowlists."
            ),
        },
    }
    return (system, {"role": "user", "content": json.dumps(material, ensure_ascii=False, sort_keys=True)})


def _safe_expression_templates(
    allowed_fields: tuple[str, ...],
    lookback: int,
) -> tuple[str, ...]:
    fields = set(allowed_fields)
    templates: list[str] = []
    if "close" in fields:
        templates.extend([
            f"rank_cross_section(pct_change(close, {lookback}))",
            f"zscore_cross_section(pct_change(close, {lookback}))",
            (
                "rank_cross_section(divide_safe("
                f"pct_change(close, {lookback}),rolling_std(close, {lookback})))"
            ),
        ])
    if {"close", "volume"}.issubset(fields):
        templates.append(
            "rank_cross_section(add("
            f"pct_change(close, {lookback}),pct_change(volume, {lookback})))"
        )
    if "market_cap" in fields:
        templates.append("zscore_cross_section(market_cap)")
    return tuple(templates)
