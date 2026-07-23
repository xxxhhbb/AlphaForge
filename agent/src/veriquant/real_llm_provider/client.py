"""OpenAI-compatible HTTP client for Real LLM Research Desks.

This module only implements a provider boundary.  It does not compile factors,
evaluate evidence, select candidates, approve capital actions, place trades, or
write ledgers.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlparse

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_finite_number, require_identifier, require_text
from ..llm_desks.providers import OpenAICompatibleResponse
from ..llm_desks.safety import assert_no_forbidden_content


REAL_LLM_PROVIDER_SCHEMA_VERSION = "veriquant.real-llm-provider/v1"


class ProviderStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderFailure(ContractValidationError):
    """Typed provider failure that never includes request headers or secrets."""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_details = dict(details or {})
        if status_code is not None:
            safe_details["status_code"] = status_code
        super().__init__(code, message, details=safe_details)
        self.status_code = status_code


def _is_loopback(host: str) -> bool:
    lowered = host.lower()
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return socket.gethostbyname(lowered).startswith("127.")
    except OSError:
        return False


@dataclass(frozen=True, kw_only=True)
class ApprovedEndpointPolicy(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_PROVIDER_SCHEMA_VERSION

    approved_hosts: tuple[str, ...]
    allow_loopback_http: bool = True

    def __post_init__(self) -> None:
        if not self.approved_hosts:
            raise ProviderFailure(FailureCode.MISSING_REQUIRED_FIELD, "approved_hosts is required")
        normalized: list[str] = []
        for host in self.approved_hosts:
            require_text(host, "approved_host")
            if "/" in host or "\\" in host or "@" in host:
                raise ProviderFailure(FailureCode.INVALID_VALUE, "approved host must be a bare host")
            normalized.append(host.lower())
        object.__setattr__(self, "approved_hosts", tuple(sorted(set(normalized))))

    def validate_url(self, endpoint_url: str) -> None:
        parsed = urlparse(endpoint_url)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password:
            raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "endpoint must not contain userinfo")
        if not host:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "endpoint host is required")
        if parsed.scheme == "https":
            if host not in self.approved_hosts:
                raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "endpoint host is not approved")
            return
        if parsed.scheme == "http" and self.allow_loopback_http and _is_loopback(host):
            return
        raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "endpoint scheme or host is not approved")

    def validate_redirect(self, *, original_url: str, location: str) -> None:
        target = urljoin(original_url, location)
        original = urlparse(original_url)
        redirected = urlparse(target)
        if (redirected.scheme, redirected.hostname) != (original.scheme, original.hostname):
            raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "cross-host provider redirect is rejected")


@dataclass(frozen=True, kw_only=True)
class RealLLMProviderSettings(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_PROVIDER_SCHEMA_VERSION

    endpoint_url: str
    model: str
    approved_hosts: tuple[str, ...]
    api_key_required: bool = True
    timeout_seconds: float = 10.0
    max_retries: int = 1
    max_tokens: int = 2048
    temperature: float = 0.0
    top_p: float = 1.0
    response_format: str = "json_object"
    non_sensitive_extra: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.endpoint_url, "endpoint_url")
        require_text(self.model, "model")
        ApprovedEndpointPolicy(approved_hosts=self.approved_hosts).validate_url(self.endpoint_url)
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "max_retries must be a non-negative integer")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "max_tokens must be positive")
        for name in ("timeout_seconds", "temperature", "top_p"):
            require_finite_number(getattr(self, name), name)
        if not 0.0 < float(self.timeout_seconds) <= 300.0:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "timeout_seconds must be in (0, 300]")
        if self.response_format != "json_object":
            raise ProviderFailure(FailureCode.INVALID_VALUE, "only json_object response format is supported")
        extra = dict(self.non_sensitive_extra or {})
        _validate_extra(extra)
        object.__setattr__(self, "non_sensitive_extra", MappingProxyType(extra))


@dataclass(frozen=True, kw_only=True)
class ProviderProbeResult(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_PROVIDER_SCHEMA_VERSION

    status: ProviderStatus
    model: str
    endpoint_host: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProviderStatus):
            object.__setattr__(self, "status", ProviderStatus(self.status))
        require_text(self.model, "model")
        require_identifier(self.endpoint_host, "endpoint_host")
        if self.reason_code is not None:
            require_identifier(self.reason_code, "reason_code")


@dataclass(frozen=True, kw_only=True)
class HTTPTransportResult:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, kw_only=True)
class ProviderToolCall:
    """One provider-requested call to an explicitly supplied research tool."""

    call_id: str
    name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_text(self.call_id, "tool_call.id")
        require_identifier(self.name, "tool_call.name")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, kw_only=True)
class OpenAICompatibleChatResponse:
    """Tool-aware response used only by the bounded research AgentLoop."""

    content: str | None
    payload: Mapping[str, Any] | None
    tool_calls: tuple[ProviderToolCall, ...]
    finish_reason: str
    input_tokens: int
    output_tokens: int


class HTTPTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HTTPTransportResult: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibHTTPTransport:
    """Small stdlib transport; tests inject fake transports instead."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post_json(
        self,
        *,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HTTPTransportResult:
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers=dict(headers))
        try:
            with self._opener.open(request, timeout=float(timeout_seconds)) as response:
                return HTTPTransportResult(
                    status_code=int(response.status),
                    headers={str(k): str(v) for k, v in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HTTPTransportResult(
                status_code=int(exc.code),
                headers={str(k): str(v) for k, v in exc.headers.items()},
                body=exc.read(),
            )
        except TimeoutError as exc:
            raise ProviderFailure(FailureCode.PROVIDER_TIMEOUT, "provider request timed out") from exc


class OpenAICompatibleHTTPClient:
    """Server-configured OpenAI-compatible JSON client."""

    def __init__(
        self,
        *,
        settings: RealLLMProviderSettings,
        runtime_credential: str | None = None,
        transport: HTTPTransport | None = None,
    ) -> None:
        self.settings = settings
        self._runtime_credential = runtime_credential
        self._transport = transport or UrllibHTTPTransport()
        self._policy = ApprovedEndpointPolicy(approved_hosts=settings.approved_hosts)

    def __repr__(self) -> str:
        host = urlparse(self.settings.endpoint_url).hostname or "<invalid>"
        return f"OpenAICompatibleHTTPClient(model={self.settings.model!r}, host={host!r})"

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
        response_safety_profile: str = "STRICT",
    ) -> OpenAICompatibleResponse:
        if self.settings.api_key_required and not self._runtime_credential:
            raise ProviderFailure(FailureCode.ADAPTER_UNAVAILABLE, "provider credential is unavailable")
        if model != self.settings.model:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "caller model does not match configured model")
        if response_format != "json_object":
            raise ProviderFailure(FailureCode.INVALID_VALUE, "provider requires json_object response format")
        if response_safety_profile not in {"STRICT", "RESEARCH_SOURCE_DRAFT"}:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "unsupported response safety profile")
        _validate_messages(messages, response_safety_profile=response_safety_profile)
        _validate_extra(extra)
        merged_extra = {**dict(self.settings.non_sensitive_extra or {}), **dict(extra)}
        body = {
            "model": self.settings.model,
            "messages": tuple(dict(item) for item in messages),
            "temperature": float(temperature),
            "top_p": float(top_p),
            "max_tokens": min(int(max_tokens), int(self.settings.max_tokens)),
            "response_format": {"type": "json_object"},
            "stream": False,
            **merged_extra,
        }
        result = self._post_body(body=body, timeout_seconds=timeout_seconds)
        return _parse_openai_body(result.body, response_safety_profile=response_safety_profile)

    def complete_chat(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
        response_safety_profile: str = "RESEARCH_SOURCE_DRAFT",
    ) -> OpenAICompatibleChatResponse:
        """Perform one OpenAI-compatible tool-aware research turn.

        This method is deliberately separate from :meth:`complete_json` so no
        existing JSON-only caller silently gains tool-call authority.  Tool
        definitions and every returned call are fenced to the caller-supplied
        names.  The model still receives no execution, Evidence, or Promotion
        authority; the host AgentLoop executes only its filtered registry.
        """

        if self.settings.api_key_required and not self._runtime_credential:
            raise ProviderFailure(FailureCode.ADAPTER_UNAVAILABLE, "provider credential is unavailable")
        if model != self.settings.model:
            raise ProviderFailure(FailureCode.INVALID_VALUE, "caller model does not match configured model")
        if response_format != "json_object":
            raise ProviderFailure(FailureCode.INVALID_VALUE, "provider requires json_object response format")
        if response_safety_profile != "RESEARCH_SOURCE_DRAFT":
            raise ProviderFailure(FailureCode.INVALID_VALUE, "tool-aware chat is restricted to research source drafts")
        normalized_messages = _validate_chat_messages(messages)
        normalized_tools, allowed_tool_names = _validate_tool_definitions(tools)
        _validate_extra(extra)
        merged_extra = {**dict(self.settings.non_sensitive_extra or {}), **dict(extra)}
        body = {
            "model": self.settings.model,
            "messages": normalized_messages,
            "tools": normalized_tools,
            "tool_choice": "auto",
            "temperature": float(temperature),
            "top_p": float(top_p),
            "max_tokens": min(int(max_tokens), int(self.settings.max_tokens)),
            "response_format": {"type": "json_object"},
            "stream": False,
            **merged_extra,
        }
        result = self._post_body(body=body, timeout_seconds=timeout_seconds)
        return _parse_openai_chat_body(result.body, allowed_tool_names=allowed_tool_names)

    def _post_body(
        self,
        *,
        body: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HTTPTransportResult:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._runtime_credential:
            headers["Authorization"] = f"Bearer {self._runtime_credential}"
        timeout = min(float(timeout_seconds), float(self.settings.timeout_seconds))
        last_failure: ProviderFailure | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                result = self._transport.post_json(
                    url=self.settings.endpoint_url,
                    body=body,
                    headers=headers,
                    timeout_seconds=timeout,
                )
            except ProviderFailure as exc:
                if exc.code in {FailureCode.PROVIDER_TIMEOUT, FailureCode.ADAPTER_EXECUTION_FAILED} and attempt < self.settings.max_retries:
                    last_failure = exc
                    continue
                raise
            except TimeoutError as exc:
                raise ProviderFailure(FailureCode.PROVIDER_TIMEOUT, "provider request timed out") from exc
            except Exception as exc:
                raise ProviderFailure(FailureCode.ADAPTER_EXECUTION_FAILED, "provider transport failed") from exc
            if 300 <= result.status_code < 400:
                location = result.headers.get("Location") or result.headers.get("location") or ""
                self._policy.validate_redirect(original_url=self.settings.endpoint_url, location=location)
                raise ProviderFailure(FailureCode.ADAPTER_EXECUTION_FAILED, "provider redirect is not followed", status_code=result.status_code)
            if result.status_code == 429 or result.status_code >= 500:
                last_failure = ProviderFailure(FailureCode.ADAPTER_EXECUTION_FAILED, "provider returned retryable error", status_code=result.status_code)
                if attempt < self.settings.max_retries:
                    continue
                raise last_failure
            if result.status_code < 200 or result.status_code >= 300:
                raise ProviderFailure(FailureCode.ADAPTER_EXECUTION_FAILED, "provider returned HTTP error", status_code=result.status_code)
            return result
        assert last_failure is not None
        raise last_failure


class ProviderAvailabilityProbe:
    """Fail-closed provider availability check."""

    def probe_config(self, *, settings: RealLLMProviderSettings, runtime_credential: str | None) -> ProviderProbeResult:
        host = urlparse(settings.endpoint_url).hostname or "unknown"
        try:
            ApprovedEndpointPolicy(approved_hosts=settings.approved_hosts).validate_url(settings.endpoint_url)
        except ProviderFailure as exc:
            return ProviderProbeResult(status=ProviderStatus.UNAVAILABLE, model=settings.model, endpoint_host=host, reason_code=exc.code.value)
        if settings.api_key_required and not runtime_credential:
            return ProviderProbeResult(
                status=ProviderStatus.UNAVAILABLE,
                model=settings.model,
                endpoint_host=host,
                reason_code=FailureCode.ADAPTER_UNAVAILABLE.value,
            )
        return ProviderProbeResult(status=ProviderStatus.AVAILABLE, model=settings.model, endpoint_host=host)


_DRAFT_MESSAGE_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bbearer\s+[A-Za-z0-9._-]{8,}",
        r"\b(?:api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]{8,}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{12,}",
    )
)


def _validate_messages(
    messages: tuple[Mapping[str, str], ...],
    *,
    response_safety_profile: str = "STRICT",
) -> None:
    if not isinstance(messages, tuple) or not messages:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "messages must be a non-empty tuple")
    for item in messages:
        if set(item) != {"role", "content"}:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "messages must contain only role and content")
        if item["role"] not in {"system", "user", "assistant"}:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "unsupported message role")
        require_text(item["content"], "message.content")
        if response_safety_profile == "STRICT":
            assert_no_forbidden_content(item["content"], where="provider.message")
        else:
            # A research-source request necessarily describes inert Python,
            # imports, execution contracts, and the authority boundary.  The
            # legacy Desk scanner treats those ordinary words as capabilities
            # and therefore rejected AlphaForge's own machine-authored prompt
            # during the JSON compatibility retry.  At this boundary the
            # caller supplies a fixed internal prompt; source still crosses
            # the AST review and no-network Docker runner.  Reject actual
            # credential material here while leaving authority fields and
            # unsafe source operations to the structural draft normalizer and
            # mechanical review gateway.
            for pattern in _DRAFT_MESSAGE_SECRET_PATTERNS:
                if pattern.search(item["content"]):
                    raise ProviderFailure(
                        FailureCode.UNAUTHORIZED_CAPABILITY,
                        "research draft message contains credential material",
                    )


def _validate_extra(extra: Mapping[str, Any]) -> None:
    forbidden = {"endpoint", "url", "base_url", "headers", "authorization", "api_key", "tools", "functions"}
    if set(extra) & forbidden:
        raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "caller cannot override provider endpoint, headers, or tools")
    assert_no_forbidden_content(dict(extra), where="provider.extra")


def _validate_chat_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Validate the OpenAI message subset emitted by Vibe's AgentLoop.

    Unlike the legacy JSON-only validator, this accepts assistant tool calls
    and matching tool results.  It never accepts arbitrary provider metadata,
    caller headers, paths, or binary values.
    """

    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "chat messages must be a non-empty sequence")
    normalized: list[dict[str, Any]] = []
    pending_call_ids: set[str] = set()
    for item in messages:
        if not isinstance(item, Mapping):
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "chat message must be an object")
        role = item.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "unsupported chat message role")
        allowed_keys = {
            "system": {"role", "content"},
            "user": {"role", "content"},
            "assistant": {"role", "content", "tool_calls", "reasoning_content"},
            "tool": {"role", "content", "tool_call_id", "name"},
        }[str(role)]
        if set(item) - allowed_keys:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "chat message contains unsupported fields")
        content = item.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "chat message content must be text or null")
        clean = {key: value for key, value in item.items() if key in allowed_keys and value is not None}
        if role == "assistant" and "tool_calls" in item:
            raw_calls = item.get("tool_calls")
            if not isinstance(raw_calls, list) or not raw_calls:
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool_calls must be a non-empty list")
            calls: list[dict[str, Any]] = []
            for raw in raw_calls:
                if not isinstance(raw, Mapping) or set(raw) - {"id", "type", "function", "extra_content"}:
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool call is invalid")
                call_id = raw.get("id")
                function = raw.get("function")
                if not isinstance(call_id, str) or not call_id or not isinstance(function, Mapping):
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool call identity is invalid")
                if set(function) - {"name", "arguments"} or not isinstance(function.get("name"), str):
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool function is invalid")
                arguments = function.get("arguments")
                if isinstance(arguments, Mapping):
                    arguments = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if not isinstance(arguments, str):
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool arguments must be JSON text")
                try:
                    decoded = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool arguments are invalid JSON") from exc
                if not isinstance(decoded, Mapping):
                    raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "assistant tool arguments must decode to an object")
                pending_call_ids.add(call_id)
                calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": function["name"], "arguments": arguments},
                })
            clean["tool_calls"] = calls
        if role == "tool":
            call_id = item.get("tool_call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or call_id not in pending_call_ids or not isinstance(name, str):
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool result does not match a prior call")
        normalized.append(clean)
    return tuple(normalized)


def _validate_tool_definitions(
    tools: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], frozenset[str]]:
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)) or not tools:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tools must be a non-empty sequence")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, Mapping) or set(item) != {"type", "function"} or item.get("type") != "function":
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool definition is invalid")
        function = item.get("function")
        if not isinstance(function, Mapping) or set(function) - {"name", "description", "parameters"}:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool function definition is invalid")
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(description, str) or not isinstance(parameters, Mapping):
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool definition is incomplete")
        require_identifier(name, "tool.name")
        if name in names:
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool names must be unique")
        if parameters.get("type") != "object":
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool parameters must be an object schema")
        names.add(name)
        normalized.append({
            "type": "function",
            "function": {"name": name, "description": description, "parameters": dict(parameters)},
        })
    normalized.sort(key=lambda item: item["function"]["name"])
    return tuple(normalized), frozenset(names)


def _parse_openai_chat_body(
    body: bytes,
    *,
    allowed_tool_names: frozenset[str],
) -> OpenAICompatibleChatResponse:
    try:
        raw = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider returned invalid JSON") from exc
    choices = raw.get("choices") if isinstance(raw, Mapping) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider response must contain exactly one choice")
    choice = choices[0]
    finish_reason = str(choice.get("finish_reason") or "stop")
    if finish_reason == "length":
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider response was truncated")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider choice message is missing")
    raw_calls = message.get("tool_calls")
    tool_calls: list[ProviderToolCall] = []
    if raw_calls:
        if not isinstance(raw_calls, list):
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider tool_calls must be a list")
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping) or raw_call.get("type", "function") != "function":
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider tool call is invalid")
            function = raw_call.get("function")
            call_id = raw_call.get("id")
            if not isinstance(function, Mapping) or not isinstance(call_id, str) or not call_id:
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider tool call identity is invalid")
            name = function.get("name")
            arguments_text = function.get("arguments")
            if not isinstance(name, str) or name not in allowed_tool_names or not isinstance(arguments_text, str):
                raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "provider requested an undeclared research tool")
            try:
                arguments = json.loads(arguments_text)
            except json.JSONDecodeError as exc:
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider tool arguments are invalid JSON") from exc
            if not isinstance(arguments, Mapping):
                raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider tool arguments must decode to an object")
            tool_calls.append(ProviderToolCall(call_id=call_id, name=name, arguments=arguments))
    usage = raw.get("usage") if isinstance(raw, Mapping) and isinstance(raw.get("usage"), Mapping) else {}
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    if tool_calls:
        return OpenAICompatibleChatResponse(
            content=message.get("content") if isinstance(message.get("content"), str) else "",
            payload=None,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason or "tool_calls",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider final content must be JSON text")
    normalized_content = _unwrap_json_fence(content)
    try:
        payload = json.loads(normalized_content)
    except json.JSONDecodeError:
        payload = _extract_single_json_object(content)
    if not isinstance(payload, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider final content must be a JSON object")
    return OpenAICompatibleChatResponse(
        content=content,
        payload=MappingProxyType(dict(payload)),
        tool_calls=(),
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_openai_body(body: bytes, *, response_safety_profile: str = "STRICT") -> OpenAICompatibleResponse:
    try:
        raw = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider response must be an object")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider response must contain exactly one choice")
    choice = choices[0]
    if isinstance(choice, Mapping) and choice.get("finish_reason") == "length":
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider JSON response was truncated")
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(message, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider choice message is missing")
    if "tool_calls" in message or "function_call" in message:
        raise ProviderFailure(FailureCode.UNAUTHORIZED_CAPABILITY, "provider tool calls are not accepted")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider content must be JSON text")
    if response_safety_profile == "STRICT":
        assert_no_forbidden_content(content, where="provider.content")
    normalized_content = _unwrap_json_fence(content)
    try:
        payload = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        if response_safety_profile != "RESEARCH_SOURCE_DRAFT":
            raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider content is not JSON") from exc
        payload = _extract_single_json_object(content)
    if not isinstance(payload, Mapping):
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider JSON content must be an object")
    if response_safety_profile == "STRICT":
        assert_no_forbidden_content(dict(payload), where="provider.payload")
    usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    return OpenAICompatibleResponse(payload=MappingProxyType(dict(payload)), input_tokens=input_tokens, output_tokens=output_tokens)


def _extract_single_json_object(content: str) -> Mapping[str, Any]:
    """Recover one JSON object from provider reasoning wrappers.

    Only the research-source profile calls this helper.  Surrounding prose is
    discarded and never persisted; the recovered object still passes the
    dedicated draft schema and the source still passes the AST gate.
    """

    decoder = json.JSONDecoder()
    candidates: list[Mapping[str, Any]] = []
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            candidates.append(value)
    unique = {json.dumps(dict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in candidates}
    if len(unique) != 1:
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider content does not contain exactly one JSON object")
    return dict(candidates[0])


def _unwrap_json_fence(content: str) -> str:
    """Accept exactly one complete JSON code fence and no surrounding prose."""

    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```" or lines[0].strip().lower() not in {"```", "```json"}:
        return stripped
    inner = "\n".join(lines[1:-1]).strip()
    return inner if inner and "```" not in inner else stripped
