from __future__ import annotations

from pathlib import Path

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.real_llm_provider.client import ProviderStatus
from src.veriquant.alphaforge_llm import (
    AlphaForgeLLMConfigLoader,
    AlphaForgePromptingHTTPClient,
    build_openai_compatible_provider,
    probe_provider,
)
from src.veriquant.llm_desks.providers import OpenAICompatibleResponse


def test_loads_existing_openai_names_from_dotenv_without_exposing_key(tmp_path: Path) -> None:
    secret = "test-key-must-not-appear"
    path = tmp_path / ".env"
    path.write_text(
        "ENABLE_LLM_RESEARCH=1\nOPENAI_BASE_URL=https://llm.example/v1\nOPENAI_API_KEY=" + secret + "\nLANGCHAIN_MODEL_NAME=alpha-model\n",
        encoding="utf-8",
    )
    configuration = AlphaForgeLLMConfigLoader(environ={}, dotenv_path=path).load()

    assert configuration.settings is not None
    assert configuration.settings.endpoint_url == "https://llm.example/v1/chat/completions"
    assert configuration.settings.approved_hosts == ("llm.example",)
    assert configuration.runtime_credential == secret
    assert secret not in repr(configuration)
    assert secret not in str(configuration.public_view())
    probe = probe_provider(configuration)
    assert probe is not None and probe.status is ProviderStatus.AVAILABLE


def test_alphaforge_values_override_legacy_and_environment_overrides_dotenv(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("ENABLE_LLM_RESEARCH=0\nOPENAI_BASE_URL=https://old.example/v1\n", encoding="utf-8")
    configuration = AlphaForgeLLMConfigLoader(
        dotenv_path=path,
        environ={
            "ENABLE_LLM_RESEARCH": "true",
            "ALPHAFORGE_LLM_ENDPOINT_URL": "https://new.example/v1/chat/completions",
            "ALPHAFORGE_LLM_MODEL": "new-model",
            "ALPHAFORGE_LLM_API_KEY": "runtime-secret",
            "ALPHAFORGE_LLM_APPROVED_HOSTS": "new.example, backup.example",
            "ALPHAFORGE_LLM_TIMEOUT_SECONDS": "120",
            "ALPHAFORGE_LLM_MAX_TOKENS": "4096",
        },
    ).load()
    assert configuration.settings is not None
    assert configuration.settings.model == "new-model"
    assert configuration.settings.approved_hosts == ("backup.example", "new.example")
    assert configuration.settings.timeout_seconds == 120
    assert configuration.settings.max_tokens == 4096


def test_disabled_or_incomplete_configuration_fails_closed() -> None:
    disabled = AlphaForgeLLMConfigLoader(environ={"ENABLE_LLM_RESEARCH": "0"}).load()
    assert disabled.settings is None
    assert probe_provider(disabled) is None
    with pytest.raises(ContractValidationError) as disabled_error:
        build_openai_compatible_provider(disabled)
    assert disabled_error.value.code is FailureCode.ADAPTER_UNAVAILABLE

    incomplete = AlphaForgeLLMConfigLoader(environ={"ENABLE_LLM_RESEARCH": "1", "OPENAI_API_KEY": "secret"}).load()
    assert incomplete.settings is None
    with pytest.raises(ContractValidationError) as incomplete_error:
        build_openai_compatible_provider(incomplete)
    assert incomplete_error.value.code is FailureCode.ADAPTER_UNAVAILABLE


def test_invalid_enable_value_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        AlphaForgeLLMConfigLoader(environ={"ENABLE_LLM_RESEARCH": "perhaps"}).load()


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://user:pass@llm.example/v1",
        "https://llm.example/v1?token=hidden",
        "https://llm.example/v1#fragment",
    ),
)
def test_endpoint_cannot_embed_credentials_query_or_fragment(endpoint: str) -> None:
    with pytest.raises(ContractValidationError):
        AlphaForgeLLMConfigLoader(
            environ={
                "ENABLE_LLM_RESEARCH": "1",
                "OPENAI_BASE_URL": endpoint,
                "OPENAI_API_KEY": "runtime-secret",
                "LANGCHAIN_MODEL_NAME": "alpha-model",
            }
        ).load()


def test_built_provider_has_only_configured_model_and_safe_repr() -> None:
    configuration = AlphaForgeLLMConfigLoader(
        environ={
            "ENABLE_LLM_RESEARCH": "1",
            "OPENAI_BASE_URL": "https://llm.example/v1",
            "OPENAI_API_KEY": "do-not-log-this",
            "LANGCHAIN_MODEL_NAME": "alpha-model",
        }
    ).load()
    provider = build_openai_compatible_provider(configuration)
    assert provider.config.model == "alpha-model"
    assert "do-not-log-this" not in repr(provider.client)


def test_prompting_client_builds_exact_stage_prompt_without_secret_terms() -> None:
    captured: dict[str, object] = {}

    class Inner:
        def complete_json(self, **kwargs):
            captured.update(kwargs)
            return OpenAICompatibleResponse(
                payload={
                    "name": "safe_factor",
                    "description": "short draft",
                    "expression": "rank_cross_section(pct_change(close, 3))",
                    "variables": ["close"],
                    "lookback_bars": 3,
                    "neutralization": [],
                },
                input_tokens=10,
                output_tokens=12,
            )

    response = AlphaForgePromptingHTTPClient(Inner()).complete_json(
        messages=(
            {
                "role": "system",
                "content": '{"authority":"draft_only","desk":"market_structure","fixed_stage_machine":true,"schema":"strict_json","slot":"factor_draft"}',
            },
            {"role": "user", "content": '{"hypothesis_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}'},
        ),
        model="alpha-model",
        timeout_seconds=1,
        max_tokens=128,
        temperature=0.0,
        top_p=1.0,
        response_format="json_object",
        extra={},
    )

    rendered = str(captured["messages"]).lower()
    assert response.payload["name"] == "safe_factor"
    for forbidden in ("api_key", "authorization", "sealed", "promotion", "broker", "ledger", "factor.py"):
        assert forbidden not in rendered
