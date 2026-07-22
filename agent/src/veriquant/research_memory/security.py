from __future__ import annotations

import re
from typing import Any

from .models import FailureCode, ResearchMemoryError


PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.I)),
    ("read_secrets", re.compile(r"\b(read|print|exfiltrate|dump)\b.{0,40}\b(secret|key|token|cookie|credential)", re.I)),
    ("call_shell", re.compile(r"\b(shell|bash|powershell|cmd\.exe|subprocess|system\()", re.I)),
    ("write_files", re.compile(r"\b(write|modify|overwrite|delete)\b.{0,30}\b(file|disk|state\.json)", re.I)),
    ("network_download", re.compile(r"\b(download|curl|wget|fetch)\b.{0,30}\b(network|http|url)", re.I)),
    ("place_order", re.compile(r"\b(place|submit|send|cancel)\b.{0,30}\b(order|trade)", re.I)),
    ("risk_or_promotion", re.compile(r"\b(approve|override|bypass|promote)\b.{0,40}\b(risk|promotion|verdict)", re.I)),
)

SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"\b(api[_-]?key|token|cookie|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.I),
)

FORBIDDEN_MEMORY_KEYS = {
    "sealed",
    "sealed_rows",
    "sealed_metrics",
    "sealed_ranking",
    "test_rows",
    "test_metrics",
    "test_ranking",
    "promotion_private_reason",
    "private_promotion_reason",
    "api_key",
    "apikey",
    "token",
    "cookie",
    "authorization",
    "raw_content",
    "unfrozen_web_content",
    "orders",
    "target_weights",
    "risk_approval",
}


def detect_prompt_injection(text: str) -> tuple[str, ...]:
    hits = [name for name, pattern in PROMPT_INJECTION_PATTERNS if pattern.search(text or "")]
    return tuple(sorted(set(hits)))


def redact_sensitive(text: str) -> str:
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def assert_no_forbidden_memory_payload(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if key_text in FORBIDDEN_MEMORY_KEYS:
                raise ResearchMemoryError(
                    FailureCode.FORBIDDEN_MEMORY_PAYLOAD.value,
                    f"forbidden memory key at {path}.{key_text}",
                )
            assert_no_forbidden_memory_payload(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            assert_no_forbidden_memory_payload(item, path=f"{path}[{idx}]")
        return
    if isinstance(value, str):
        lower = value.lower()
        if "sealed metric" in lower or "sealed ranking" in lower or "test row" in lower:
            raise ResearchMemoryError(
                FailureCode.FORBIDDEN_MEMORY_PAYLOAD.value,
                f"forbidden sealed/test text at {path}",
            )
        if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
            raise ResearchMemoryError(
                FailureCode.FORBIDDEN_MEMORY_PAYLOAD.value,
                f"secret-like text at {path}",
            )
