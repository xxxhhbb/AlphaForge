from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from src.veriquant.contracts import FailureCode
from src.veriquant.real_llm_provider.client import (
    ApprovedEndpointPolicy,
    HTTPTransportResult,
    OpenAICompatibleHTTPClient,
    ProviderAvailabilityProbe,
    ProviderFailure,
    ProviderStatus,
    RealLLMProviderSettings,
)


def settings(**overrides: object) -> RealLLMProviderSettings:
    data: dict[str, object] = {
        "endpoint_url": "https://llm.local.example/v1/chat/completions",
        "model": "desk-model",
        "approved_hosts": ("llm.local.example",),
        "max_retries": 0,
    }
    data.update(overrides)
    return RealLLMProviderSettings(**data)


class FakeTransport:
    def __init__(self, responses: list[HTTPTransportResult] | None = None, *, exc: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.exc = exc
        self.calls: list[Mapping[str, Any]] = []
        self.headers: list[Mapping[str, str]] = []

    def post_json(
        self,
        *,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HTTPTransportResult:
        self.calls.append(body)
        self.headers.append(headers)
        if self.exc is not None:
            raise self.exc
        return self.responses.pop(0)


def openai_body(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        }
    ).encode("utf-8")


def _research_tools() -> tuple[Mapping[str, Any], ...]:
    return ({
        "type": "function",
        "function": {
            "name": "describe_available_data",
            "description": "Read the frozen data catalog.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },)


def _chat_kwargs() -> dict[str, Any]:
    return {
        "messages": ({"role": "system", "content": "bounded research draft"},),
        "tools": _research_tools(),
        "model": "desk-model",
        "timeout_seconds": 2.0,
        "max_tokens": 64,
        "temperature": 0.0,
        "top_p": 1.0,
        "response_format": "json_object",
        "extra": {},
    }


def test_tool_aware_chat_returns_only_declared_research_call() -> None:
    body = json.dumps({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_data_1",
                    "type": "function",
                    "function": {"name": "describe_available_data", "arguments": "{}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }).encode("utf-8")
    transport = FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=body)])
    client = OpenAICompatibleHTTPClient(settings=settings(), runtime_credential="runtime-only", transport=transport)

    response = client.complete_chat(**_chat_kwargs())

    assert response.payload is None
    assert [(item.call_id, item.name, dict(item.arguments)) for item in response.tool_calls] == [
        ("call_data_1", "describe_available_data", {})
    ]
    assert transport.calls[0]["tool_choice"] == "auto"
    assert transport.calls[0]["tools"] == _research_tools()


def test_tool_aware_chat_rejects_undeclared_tool_and_bad_arguments() -> None:
    for name, arguments in (("bash", "{}"), ("describe_available_data", "[]")):
        body = json.dumps({
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                },
            }],
        }).encode("utf-8")
        client = OpenAICompatibleHTTPClient(
            settings=settings(), runtime_credential="runtime-only",
            transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=body)]),
        )
        with pytest.raises(ProviderFailure):
            client.complete_chat(**_chat_kwargs())


def test_successful_json_response_uses_configured_endpoint_and_redacts_secret() -> None:
    transport = FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=openai_body({"state": "DRAFT"}))])
    client = OpenAICompatibleHTTPClient(settings=settings(), runtime_credential="sk-test-secret", transport=transport)

    response = client.complete_json(
        messages=({"role": "system", "content": "draft only"},),
        model="desk-model",
        timeout_seconds=2.0,
        max_tokens=64,
        temperature=0.0,
        top_p=1.0,
        response_format="json_object",
        extra={},
    )

    assert response.payload == {"state": "DRAFT"}
    assert response.input_tokens == 3
    assert "sk-test-secret" not in repr(client)
    assert "sk-test-secret" not in str(transport.calls)


def test_complete_json_fence_is_accepted_but_truncation_remains_rejected() -> None:
    fenced = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "```json\n{\"state\":\"DRAFT\"}\n```"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        }
    ).encode("utf-8")
    client = OpenAICompatibleHTTPClient(
        settings=settings(),
        runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=fenced)]),
    )
    response = client.complete_json(
        messages=({"role": "system", "content": "draft only"},),
        model="desk-model",
        timeout_seconds=2.0,
        max_tokens=64,
        temperature=0.0,
        top_p=1.0,
        response_format="json_object",
        extra={},
    )
    assert response.payload == {"state": "DRAFT"}

    truncated = json.dumps(
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "{\"state\":"},
            }]
        }
    ).encode("utf-8")
    client = OpenAICompatibleHTTPClient(
        settings=settings(),
        runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=truncated)]),
    )
    with pytest.raises(ProviderFailure, match="truncated"):
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )


def test_research_source_profile_defers_inert_source_to_ast_gate_but_strict_default_does_not() -> None:
    payload = {
        "plan": "使用 Python 草案完成横截面研究。",
        "code_drafts": ["def run_research(rows, seed):\n    return {'rows': rows}"],
        "status": "COMPLETED",
    }
    strict = OpenAICompatibleHTTPClient(
        settings=settings(), runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=openai_body(payload))]),
    )
    with pytest.raises(Exception):
        strict.complete_json(
            messages=({"role": "system", "content": "draft only"},), model="desk-model",
            timeout_seconds=2.0, max_tokens=64, temperature=0.0, top_p=1.0,
            response_format="json_object", extra={},
        )
    draft = OpenAICompatibleHTTPClient(
        settings=settings(), runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=openai_body(payload))]),
    )
    response = draft.complete_json(
        messages=({"role": "system", "content": "draft only"},), model="desk-model",
        timeout_seconds=2.0, max_tokens=64, temperature=0.0, top_p=1.0,
        response_format="json_object", extra={}, response_safety_profile="RESEARCH_SOURCE_DRAFT",
    )
    assert response.payload["code_drafts"] == payload["code_drafts"]


def test_research_source_profile_extracts_one_wrapped_json_object_only() -> None:
    payload = {"plan": "研究计划", "code_drafts": ["def run_research(rows, seed):\n    return {'rows': rows}"], "status": "COMPLETED"}
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "analysis wrapper\n" + json.dumps(payload) + "\nend wrapper"}}]}).encode("utf-8")
    client = OpenAICompatibleHTTPClient(settings=settings(), runtime_credential="runtime-only", transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=body)]))
    response = client.complete_json(
        messages=({"role": "system", "content": "draft only"},), model="desk-model",
        timeout_seconds=2.0, max_tokens=64, temperature=0.0, top_p=1.0,
        response_format="json_object", extra={}, response_safety_profile="RESEARCH_SOURCE_DRAFT",
    )
    assert response.payload["plan"] == "研究计划"


def test_missing_key_is_typed_unavailable() -> None:
    client = OpenAICompatibleHTTPClient(settings=settings(), transport=FakeTransport())

    with pytest.raises(ProviderFailure) as exc:
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )

    assert exc.value.code is FailureCode.ADAPTER_UNAVAILABLE


def test_timeout_429_5xx_and_bad_json_fail_closed() -> None:
    for status in (429, 500):
        client = OpenAICompatibleHTTPClient(
            settings=settings(),
            runtime_credential="runtime-only",
            transport=FakeTransport([HTTPTransportResult(status_code=status, headers={}, body=b"{}")]),
        )
        with pytest.raises(ProviderFailure) as exc:
            client.complete_json(
                messages=({"role": "system", "content": "draft only"},),
                model="desk-model",
                timeout_seconds=2.0,
                max_tokens=64,
                temperature=0.0,
                top_p=1.0,
                response_format="json_object",
                extra={},
            )
        assert exc.value.code is FailureCode.ADAPTER_EXECUTION_FAILED

    client = OpenAICompatibleHTTPClient(
        settings=settings(),
        runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=b"{bad")]),
    )
    with pytest.raises(ProviderFailure) as exc:
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )
    assert exc.value.code is FailureCode.PROVIDER_SCHEMA_REJECTED


def test_forbidden_payload_tool_calls_ssrf_redirect_and_dynamic_headers_are_rejected() -> None:
    with pytest.raises(ProviderFailure):
        RealLLMProviderSettings(endpoint_url="https://169.254.169.254/v1/chat/completions", model="m", approved_hosts=("llm.local.example",))

    client = OpenAICompatibleHTTPClient(settings=settings(), runtime_credential="runtime-only", transport=FakeTransport())
    with pytest.raises(ProviderFailure) as exc:
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={"headers": {"X-Test": "bad"}},
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    body = json.dumps({"choices": [{"message": {"role": "assistant", "tool_calls": []}}]}).encode("utf-8")
    client = OpenAICompatibleHTTPClient(
        settings=settings(),
        runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=200, headers={}, body=body)]),
    )
    with pytest.raises(ProviderFailure) as exc:
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    client = OpenAICompatibleHTTPClient(
        settings=settings(),
        runtime_credential="runtime-only",
        transport=FakeTransport([HTTPTransportResult(status_code=302, headers={"Location": "https://evil.example/v1"}, body=b"")]),
    )
    with pytest.raises(ProviderFailure) as exc:
        client.complete_json(
            messages=({"role": "system", "content": "draft only"},),
            model="desk-model",
            timeout_seconds=2.0,
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_loopback_http_allowed_but_non_loopback_http_rejected_and_probe_is_deterministic() -> None:
    ApprovedEndpointPolicy(approved_hosts=("localhost",)).validate_url("http://127.0.0.1:8000/v1/chat/completions")
    with pytest.raises(ProviderFailure):
        ApprovedEndpointPolicy(approved_hosts=("llm.local.example",)).validate_url("http://llm.local.example/v1/chat/completions")

    probe = ProviderAvailabilityProbe().probe_config(settings=settings(), runtime_credential=None)
    assert probe.status is ProviderStatus.UNAVAILABLE
    assert probe.reason_code == FailureCode.ADAPTER_UNAVAILABLE.value
