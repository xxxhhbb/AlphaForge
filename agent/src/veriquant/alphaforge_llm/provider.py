"""Construction and deterministic availability checks for the AlphaForge LLM."""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import FailureCode
from ..llm_desks.providers import OpenAICompatibleConfig, OpenAICompatibleProvider, OpenAICompatibleResponse
from ..real_llm_provider.client import (
    OpenAICompatibleHTTPClient,
    ProviderAvailabilityProbe,
    ProviderFailure,
    ProviderProbeResult,
)
from .config import AlphaForgeLLMConfiguration


_FIELDS_BY_DESK: Mapping[str, str] = MappingProxyType(
    {
        "fundamental": "close, market_cap, book_value",
        "expectations_events": "close",
        "market_structure": "close, volume, market_cap",
        "alternative_data": "close, volume, activity_proxy",
        "macro_regime": "close, macro_rate",
        "meta_alpha": "close",
    }
)

_EXPRESSIONS_BY_DESK: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "fundamental": ("rank_cross_section(divide_safe(book_value, market_cap))",),
        "expectations_events": ("rank_cross_section(pct_change(close, 3))",),
        "market_structure": (
            "rank_cross_section(divide_safe(volume, market_cap))",
            "rank_cross_section(pct_change(close, 5))",
        ),
        "alternative_data": ("rank_cross_section(rolling_mean(activity_proxy, 5))",),
        "macro_regime": ("rank_cross_section(multiply(pct_change(close, 5), macro_rate))",),
        "meta_alpha": ("rank_cross_section(add(pct_change(close, 20), pct_change(close, 5)))",),
    }
)


class AlphaForgePromptingHTTPClient:
    """Prompt adapter that keeps OpenAI-compatible calls in DRAFT-only slots.

    ``OpenAICompatibleProvider`` records hash-only traces and validates exact
    output schemas.  This client enriches those hash-only messages just enough
    for a real model to return the expected JSON fields, while preserving the
    same narrow client protocol and without exposing secrets or local paths.
    """

    def __init__(self, inner: OpenAICompatibleHTTPClient) -> None:
        self.inner = inner

    def __repr__(self) -> str:
        return f"AlphaForgePromptingHTTPClient(inner={self.inner!r})"

    def complete_json(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
    ) -> OpenAICompatibleResponse:
        control = _control_payload(messages)
        slot = str(control.get("slot", "unknown"))
        desk = str(control.get("desk", "unknown"))
        prompt_messages = _prompt_messages(slot=slot, desk=desk, original_messages=messages)
        return self.inner.complete_json(
            messages=prompt_messages,
            model=model,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            response_format=response_format,
            extra=extra,
        )


def _control_payload(messages: tuple[Mapping[str, str], ...]) -> Mapping[str, Any]:
    try:
        first = messages[0]
        if first.get("role") != "system":
            raise ValueError("missing system message")
        payload = json.loads(first.get("content", ""))
    except Exception as exc:  # noqa: BLE001 - converted to typed provider failure.
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider control message is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider control message must be an object")
    return payload


def _prompt_messages(
    *,
    slot: str,
    desk: str,
    original_messages: tuple[Mapping[str, str], ...],
) -> tuple[Mapping[str, str], ...]:
    source_hashes = [item.get("content", "") for item in original_messages if item.get("role") == "user"]
    base = {
        "role": "system",
        "content": (
            "AlphaForge draft research JSON only. "
            "Return exactly the requested keys, no markdown, no extra keys, no executable code. "
            "Use inert FactorDAG expressions only. "
            "All list fields must be JSON arrays of strings, never a single string. "
            "Integer fields must be JSON integers."
        ),
    }
    fields = _FIELDS_BY_DESK.get(desk, "close, volume, market_cap")
    if slot == "hypothesis":
        instruction = {
            "role": "user",
            "content": json.dumps(
                {
                    "desk": desk,
                    "slot": slot,
                    "source_hashes": source_hashes,
                    "return_keys": ["statement", "economic_mechanism", "expected_sign", "falsification_tests"],
                    "field_types": {
                        "statement": "string",
                        "economic_mechanism": "string",
                        "expected_sign": "one of POSITIVE, NEGATIVE, NON_MONOTONIC",
                        "falsification_tests": "array<string>",
                    },
                    "expected_sign_values": ["POSITIVE", "NEGATIVE", "NON_MONOTONIC"],
                    "style": "concise finance research draft",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        return (base, instruction)
    if slot == "factor_draft":
        instruction = {
            "role": "user",
            "content": json.dumps(
                {
                    "desk": desk,
                    "slot": slot,
                    "source_hashes": source_hashes,
                    "return_keys": ["name", "description", "expression", "variables", "lookback_bars", "neutralization"],
                    "field_types": {
                        "name": "lowercase_identifier_string",
                        "description": "string",
                        "expression": "allowed FactorDAG expression string",
                        "variables": "array<string>",
                        "lookback_bars": "integer",
                        "neutralization": "array<string>, empty array allowed",
                    },
                    "name_rule": "lowercase_identifier",
                    "allowed_fields": fields,
                    "variables_rule": "must be a non-empty array listing every field used by expression",
                    "allowed_expressions": list(
                        _EXPRESSIONS_BY_DESK.get(desk, ("rank_cross_section(pct_change(close, 5))",))
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        return (base, instruction)
    if slot == "redacted_feedback":
        instruction = {
            "role": "user",
            "content": json.dumps(
                {
                    "desk": desk,
                    "slot": slot,
                    "source_hashes": source_hashes,
                    "return_keys": ["explanation"],
                    "style": "short visible-summary explanation",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        return (base, instruction)
    raise ProviderFailure(FailureCode.INVALID_STATE, "unknown AlphaForge LLM slot")


def probe_provider(configuration: AlphaForgeLLMConfiguration) -> ProviderProbeResult | None:
    """Probe configuration only; this makes no network call and exposes no secret."""
    if not configuration.enabled or configuration.settings is None:
        return None
    return ProviderAvailabilityProbe().probe_config(
        settings=configuration.settings,
        runtime_credential=configuration.runtime_credential,
    )


def build_openai_compatible_provider(configuration: AlphaForgeLLMConfiguration) -> OpenAICompatibleProvider:
    """Build a draft-only provider after the caller has checked availability."""
    if not configuration.enabled or configuration.settings is None or not configuration.runtime_credential:
        raise ProviderFailure(FailureCode.ADAPTER_UNAVAILABLE, "AlphaForge LLM configuration is unavailable")
    settings = configuration.settings
    return OpenAICompatibleProvider(
        client=AlphaForgePromptingHTTPClient(
            OpenAICompatibleHTTPClient(settings=settings, runtime_credential=configuration.runtime_credential)
        ),
        config=OpenAICompatibleConfig(
            model=settings.model,
            temperature=settings.temperature,
            top_p=settings.top_p,
            response_format=settings.response_format,
        ),
    )
