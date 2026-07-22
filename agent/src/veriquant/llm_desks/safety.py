"""Fail-closed guards for bounded LLM Research Desk payloads."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize


FORBIDDEN_FIELD_TOKENS: tuple[str, ...] = (
    "authorization",
    "bearer",
    "cookie",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "sealed",
    "holdout",
    "official_metric",
    "official_metrics",
    "promotion",
    "portfolio",
    "portfolio_weight",
    "target_weight",
    "target_weights",
    "risk_approval",
    "riskapproval",
    "order",
    "orders",
    "fill",
    "fills",
    "ledger",
    "court",
    "execution",
    "broker",
)

FORBIDDEN_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsealed\b",
        r"\bholdout\b",
        r"\bsecret\b",
        r"\bauthorization\b",
        r"\bbearer\s+[A-Za-z0-9._-]+",
        r"\bapi[_ -]?key\b",
        r"\bprivate[_ -]?key\b",
        r"\bofficial[_ -]?metrics?\b",
        r"\bpromotion\b",
        r"\bportfolio\b",
        r"\btarget[_ -]?weights?\b",
        r"\brisk[_ -]?approval\b",
        r"\border(s| plan)?\b",
        r"\bfills?\b",
        r"\bledger\b",
        r"\bcourt\b",
        r"\bexecution\b",
        r"\bbroker\b",
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"\bimport\s+[A-Za-z_]",
        r"\bfactor\.py\b",
        r"\bpython\b",
        r"\bshell\b",
        r"\bsql\b",
    )
)

FORBIDDEN_TOOL_TOKENS: tuple[str, ...] = (
    "shell",
    "python",
    "sql",
    "url",
    "http",
    "file",
    "write",
    "env",
    "account",
    "ledger",
    "court",
    "promotion",
    "portfolio",
    "risk",
    "execution",
    "broker",
    "order",
)

SAFE_SCALAR_TYPES = (str, int, float, bool, type(None))


def fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def assert_no_forbidden_content(value: Any, *, where: str) -> None:
    """Reject privileged fields or sensitive strings before they reach artifacts."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "mapping keys must be non-empty strings")
            lowered = key.lower()
            if any(token in lowered for token in FORBIDDEN_FIELD_TOKENS):
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "payload contains a forbidden authority or secret field",
                    where=where,
                    field=key,
                )
            assert_no_forbidden_content(item, where=f"{where}.{key}")
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            assert_no_forbidden_content(item, where=f"{where}[{index}]")
        return
    if isinstance(value, str):
        for pattern in FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(value):
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "payload contains forbidden authority, execution, sealed, or secret text",
                    where=where,
                    pattern=pattern.pattern,
                )
        return
    if value is None or isinstance(value, (datetime, int, float, bool)):
        return
    fail(
        FailureCode.PROVIDER_SCHEMA_REJECTED,
        "payload contains an unsupported value type",
        where=where,
        type=type(value).__name__,
    )


def assert_safe_tool_name(name: str) -> None:
    lowered = name.lower()
    if any(token in lowered for token in FORBIDDEN_TOOL_TOKENS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "tool name requests a forbidden capability",
            tool=name,
        )


def canonical_hash(value: Any) -> str:
    payload = canonicalize(value)
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def opaque_hash(value: Any) -> str:
    """Hash possibly rejected payloads without retaining their plaintext."""

    try:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except TypeError:
        rendered = repr(type(value).__name__)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def summarize_mapping(
    value: Mapping[str, Any],
    *,
    allowed_keys: tuple[str, ...],
    where: str,
    max_text_chars: int = 160,
) -> dict[str, Any]:
    """Return a small allowlisted, redaction-checked mapping for tools and traces."""

    unknown = set(value) - set(allowed_keys)
    if unknown:
        fail(
            FailureCode.PROVIDER_SCHEMA_REJECTED,
            "tool result contains non-allowlisted fields",
            where=where,
            fields=tuple(sorted(unknown)),
        )
    summary: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            item = item[:max_text_chars]
        elif isinstance(item, (tuple, list)):
            item = tuple(item[:8])
        if not isinstance(item, SAFE_SCALAR_TYPES) and not isinstance(item, (tuple, list)):
            fail(
                FailureCode.PROVIDER_SCHEMA_REJECTED,
                "tool result summary contains unsupported value type",
                where=f"{where}.{key}",
            )
        assert_no_forbidden_content({key: item}, where=where)
        summary[key] = item
    return summary
