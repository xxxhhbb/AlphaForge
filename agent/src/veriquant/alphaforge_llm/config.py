"""Fail-closed local configuration for AlphaForge's draft-only LLM adapter.

The loader intentionally has a small, fixed environment surface.  It neither
logs credentials nor serializes them into public configuration objects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlparse

from ..contracts import ContractValidationError, FailureCode
from ..real_llm_provider.client import RealLLMProviderSettings


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def _value(values: Mapping[str, str], *names: str) -> str | None:
    """Return the first non-blank declared setting; AlphaForge names win."""
    for name in names:
        candidate = values.get(name)
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return None


def _dotenv_values(path: Path) -> Mapping[str, str]:
    """Read simple KEY=VALUE lines without exporting or expanding values."""
    if not path.is_file():
        return MappingProxyType({})
    selected: dict[str, str] = {}
    allowed = {
        "ENABLE_LLM_RESEARCH",
        "ALPHAFORGE_LLM_ENDPOINT_URL",
        "ALPHAFORGE_LLM_API_KEY",
        "ALPHAFORGE_LLM_MODEL",
        "ALPHAFORGE_LLM_APPROVED_HOSTS",
        "ALPHAFORGE_LLM_TIMEOUT_SECONDS",
        "ALPHAFORGE_LLM_MAX_TOKENS",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "LANGCHAIN_MODEL_NAME",
    }
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        selected[key] = value
    return MappingProxyType(selected)


def _endpoint(value: str) -> str:
    """Accept an explicit completion URL or a conventional OpenAI base URL."""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        raise ContractValidationError(FailureCode.INVALID_VALUE, "LLM endpoint must be an absolute URL")
    if parsed.username or parsed.password:
        raise ContractValidationError(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM endpoint must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ContractValidationError(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM endpoint must not contain query or fragment")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return value.rstrip("/")
    return value.rstrip("/") + "/chat/completions"


@dataclass(frozen=True, kw_only=True)
class AlphaForgeLLMConfiguration:
    """Runtime config whose repr and public view exclude the credential."""

    enabled: bool
    settings: RealLLMProviderSettings | None
    _runtime_credential: str | None = field(default=None, repr=False, compare=False)

    @property
    def runtime_credential(self) -> str | None:
        """Credential for immediate provider construction only; never persist it."""
        return self._runtime_credential

    def public_view(self) -> Mapping[str, object]:
        if self.settings is None:
            return MappingProxyType({"enabled": self.enabled, "available_config": False})
        host = urlparse(self.settings.endpoint_url).hostname or "unknown"
        return MappingProxyType(
            {
                "enabled": self.enabled,
                "available_config": bool(self._runtime_credential) and self.enabled,
                "endpoint_host": host,
                "model": self.settings.model,
                "approved_hosts": self.settings.approved_hosts,
            }
        )


class AlphaForgeLLMConfigLoader:
    """Load exactly the documented AlphaForge/OpenAI-compatible variables."""

    def __init__(self, *, environ: Mapping[str, str] | None = None, dotenv_path: Path | None = None) -> None:
        self._environ = environ if environ is not None else os.environ
        self._dotenv_path = dotenv_path

    def load(self) -> AlphaForgeLLMConfiguration:
        dot_env = _dotenv_values(self._dotenv_path) if self._dotenv_path is not None else MappingProxyType({})
        # Process environment overrides local .env without mutating either source.
        values = {**dot_env, **{name: value for name, value in self._environ.items() if name in dot_env or name.startswith("ALPHAFORGE_") or name in {"ENABLE_LLM_RESEARCH", "OPENAI_BASE_URL", "OPENAI_API_KEY", "LANGCHAIN_MODEL_NAME"}}}
        enabled_raw = values.get("ENABLE_LLM_RESEARCH", "").strip().lower()
        if enabled_raw not in _TRUE | _FALSE:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "ENABLE_LLM_RESEARCH must be a boolean-like value")
        enabled = enabled_raw in _TRUE
        endpoint_raw = _value(values, "ALPHAFORGE_LLM_ENDPOINT_URL", "OPENAI_BASE_URL")
        model = _value(values, "ALPHAFORGE_LLM_MODEL", "LANGCHAIN_MODEL_NAME")
        credential = _value(values, "ALPHAFORGE_LLM_API_KEY", "OPENAI_API_KEY")
        if not enabled:
            return AlphaForgeLLMConfiguration(enabled=False, settings=None)
        if endpoint_raw is None or model is None:
            return AlphaForgeLLMConfiguration(enabled=True, settings=None, _runtime_credential=credential)
        endpoint_url = _endpoint(endpoint_raw)
        explicit_hosts = _value(values, "ALPHAFORGE_LLM_APPROVED_HOSTS")
        timeout_seconds = float(_value(values, "ALPHAFORGE_LLM_TIMEOUT_SECONDS") or "30.0")
        max_tokens = int(_value(values, "ALPHAFORGE_LLM_MAX_TOKENS") or "2048")
        endpoint_host = urlparse(endpoint_url).hostname
        assert endpoint_host is not None
        approved_hosts = (
            tuple(sorted({part.strip().lower() for part in explicit_hosts.split(",") if part.strip()}))
            if explicit_hosts
            else (endpoint_host.lower(),)
        )
        return AlphaForgeLLMConfiguration(
            enabled=True,
            settings=RealLLMProviderSettings(
                endpoint_url=endpoint_url,
                model=model,
                approved_hosts=approved_hosts,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
            ),
            _runtime_credential=credential,
        )
