"""Server-side environment loader for the Real LLM provider.

Only a tiny allowlist of variables is read, values are never logged or returned,
and browser/user payloads cannot override endpoint, headers, or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlparse

from ..contracts import FailureCode
from .client import (
    OpenAICompatibleHTTPClient,
    ProviderAvailabilityProbe,
    ProviderFailure,
    ProviderProbeResult,
    ProviderStatus,
    RealLLMProviderSettings,
)
from ..llm_desks.providers import OpenAICompatibleConfig


_ALLOWED_ENV_KEYS = frozenset(
    {
        "ALPHAFORGE_LLM_API_KEY",
        "ALPHAFORGE_LLM_BASE_URL",
        "ALPHAFORGE_LLM_ENDPOINT_URL",
        "ALPHAFORGE_LLM_MODEL",
        "ALPHAFORGE_LLM_APPROVED_HOSTS",
        "ALPHAFORGE_LLM_TIMEOUT_SECONDS",
        "ALPHAFORGE_LLM_MAX_TOKENS",
        "ALPHAFORGE_LLM_MAX_RETRIES",
        "ALPHAFORGE_LLM_TEMPERATURE",
        "ALPHAFORGE_LLM_TOP_P",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "LANGCHAIN_MODEL_NAME",
        "ENABLE_LLM_RESEARCH",
        "ENABLE_REAL_LLM_CAMPAIGN",
    }
)


@dataclass(frozen=True, kw_only=True)
class RealLLMEnvBinding:
    """Non-secret provider binding returned to backend integration code."""

    client: OpenAICompatibleHTTPClient
    config: OpenAICompatibleConfig
    settings: RealLLMProviderSettings
    probe: ProviderProbeResult
    loaded_env_keys: tuple[str, ...]
    credential_present: bool


def load_real_llm_from_env(
    *,
    env_file: str | Path | None = None,
    process_env: Mapping[str, str] | None = None,
    require_enabled: bool = False,
) -> RealLLMEnvBinding:
    """Load an OpenAI-compatible provider from process env plus optional `.env`.

    Precedence is process environment first, then the provided dotenv file.
    The runtime credential is passed directly to the client and is never stored
    in a canonical artifact or returned in a mapping.
    """

    values = _read_dotenv(Path(env_file)) if env_file is not None else {}
    merged = dict(values)
    if process_env is not None:
        merged.update({key: value for key, value in process_env.items() if key in _ALLOWED_ENV_KEYS})
    else:
        import os

        merged.update({key: value for key, value in os.environ.items() if key in _ALLOWED_ENV_KEYS})

    if require_enabled and not _enabled(merged):
        raise ProviderFailure(FailureCode.ADAPTER_UNAVAILABLE, "real LLM campaign provider is disabled")

    endpoint_url = _endpoint_url(merged)
    model = _first_nonempty(merged, "ALPHAFORGE_LLM_MODEL", "OPENAI_MODEL", "LANGCHAIN_MODEL_NAME")
    if not model:
        raise ProviderFailure(FailureCode.MISSING_REQUIRED_FIELD, "real LLM model is not configured")
    credential = _first_nonempty(merged, "ALPHAFORGE_LLM_API_KEY", "OPENAI_API_KEY")
    approved_hosts = _approved_hosts(merged, endpoint_url)
    settings = RealLLMProviderSettings(
        endpoint_url=endpoint_url,
        model=model,
        approved_hosts=approved_hosts,
        timeout_seconds=_float_value(merged, "ALPHAFORGE_LLM_TIMEOUT_SECONDS", default=45.0),
        max_tokens=_int_value(merged, "ALPHAFORGE_LLM_MAX_TOKENS", default=2048),
        max_retries=_int_value(merged, "ALPHAFORGE_LLM_MAX_RETRIES", default=1),
        temperature=_float_value(merged, "ALPHAFORGE_LLM_TEMPERATURE", default=0.0),
        top_p=_float_value(merged, "ALPHAFORGE_LLM_TOP_P", default=1.0),
    )
    probe = ProviderAvailabilityProbe().probe_config(settings=settings, runtime_credential=credential)
    if probe.status is not ProviderStatus.AVAILABLE:
        raise ProviderFailure(FailureCode.ADAPTER_UNAVAILABLE, "real LLM provider probe failed", details={"reason_code": probe.reason_code})
    config = OpenAICompatibleConfig(
        model=settings.model,
        temperature=settings.temperature,
        top_p=settings.top_p,
        response_format=settings.response_format,
    )
    return RealLLMEnvBinding(
        client=OpenAICompatibleHTTPClient(settings=settings, runtime_credential=credential),
        config=config,
        settings=settings,
        probe=probe,
        loaded_env_keys=tuple(sorted(key for key in merged if key in _ALLOWED_ENV_KEYS and merged.get(key))),
        credential_present=bool(credential),
    )


def _read_dotenv(path: Path) -> Mapping[str, str]:
    if not path.exists():
        return MappingProxyType({})
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in _ALLOWED_ENV_KEYS:
            continue
        values[key] = _clean_env_value(value)
    return MappingProxyType(values)


def _clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def _enabled(values: Mapping[str, str]) -> bool:
    return values.get("ENABLE_REAL_LLM_CAMPAIGN") == "1" or values.get("ENABLE_LLM_RESEARCH") == "1"


def _first_nonempty(values: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = values.get(key, "").strip()
        if value:
            return value
    return ""


def _endpoint_url(values: Mapping[str, str]) -> str:
    endpoint = _first_nonempty(values, "ALPHAFORGE_LLM_ENDPOINT_URL")
    if endpoint:
        return endpoint
    base = _first_nonempty(values, "ALPHAFORGE_LLM_BASE_URL", "OPENAI_BASE_URL")
    if not base:
        raise ProviderFailure(FailureCode.MISSING_REQUIRED_FIELD, "real LLM endpoint/base URL is not configured")
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _approved_hosts(values: Mapping[str, str], endpoint_url: str) -> tuple[str, ...]:
    configured = values.get("ALPHAFORGE_LLM_APPROVED_HOSTS", "").strip()
    if configured:
        return tuple(item.strip().lower() for item in configured.split(",") if item.strip())
    host = (urlparse(endpoint_url).hostname or "").lower()
    if not host:
        raise ProviderFailure(FailureCode.INVALID_VALUE, "real LLM endpoint host is missing")
    return (host,)


def _int_value(values: Mapping[str, str], key: str, *, default: int) -> int:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ProviderFailure(FailureCode.INVALID_VALUE, f"{key} must be an integer") from exc


def _float_value(values: Mapping[str, str], key: str, *, default: float) -> float:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ProviderFailure(FailureCode.INVALID_VALUE, f"{key} must be numeric") from exc


__all__ = ["RealLLMEnvBinding", "load_real_llm_from_env"]
