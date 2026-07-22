"""Real-LLM schema and capability summary derived from the operator manifest."""

from __future__ import annotations

from ..data import canonical_content_hash
from ..qlib_expression.api import (
    STATIC_QLIB_OPERATOR_MANIFEST,
    QlibOperatorManifest,
    QlibOperatorTrust,
)
from .models import QlibCandidateLLMContract


def build_qlib_candidate_llm_contract(
    *,
    confirmed_field_whitelist: tuple[str, ...],
    requested_lookback_sessions: int | None,
    manifest: QlibOperatorManifest = STATIC_QLIB_OPERATOR_MANIFEST,
) -> QlibCandidateLLMContract:
    """Build a closed JSON schema without maintaining a second operator list."""

    operator_capabilities = tuple(
        {
            "name": item.name,
            "argument_count": item.argument_count,
            "category": item.category,
            "constraint": item.constraint,
            "trust": item.trust.value,
        }
        for item in manifest.operators
        if item.trust is not QlibOperatorTrust.EXCLUDED
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["candidate_id", "expression"],
        "properties": {
            "candidate_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$",
            },
            "expression": {"type": "string", "minLength": 1, "maxLength": 8192},
        },
    }
    capability_summary = {
        "language": "QLIB_EXPRESSION",
        "manifest_hash": manifest.manifest_hash,
        "confirmed_field_whitelist": list(sorted(confirmed_field_whitelist)),
        "requested_lookback_sessions": requested_lookback_sessions,
        "operators": list(operator_capabilities),
        "forbidden_capabilities": [
            "python_code",
            "factor.py",
            "custom_operator",
            "official_metrics",
            "promotion",
            "risk",
            "orders",
            "sealed_data",
        ],
    }
    contract_hash = canonical_content_hash(
        {
            "schema": schema,
            "capability_summary": capability_summary,
            "manifest_hash": manifest.manifest_hash,
        }
    )
    return QlibCandidateLLMContract(
        schema=schema,
        capability_summary=capability_summary,
        manifest_hash=manifest.manifest_hash,
        contract_hash=contract_hash,
    )


__all__ = ["build_qlib_candidate_llm_contract"]
