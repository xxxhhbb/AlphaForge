from __future__ import annotations

from pathlib import Path

import pytest

from src.veriquant.contracts import FailureCode
from src.veriquant.real_llm_provider import ProviderFailure, load_real_llm_from_env


def test_env_loader_uses_allowlisted_dotenv_without_exposing_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ENABLE_LLM_RESEARCH=1",
                "OPENAI_API_KEY=sk-test-secret",
                "OPENAI_BASE_URL=https://llm.local.example/v1",
                "LANGCHAIN_MODEL_NAME=test-json-model",
                "IGNORED_SECRET=do-not-read",
            ]
        ),
        encoding="utf-8",
    )

    binding = load_real_llm_from_env(env_file=env_file, process_env={}, require_enabled=True)

    assert binding.config.model == "test-json-model"
    assert binding.credential_present is True
    assert "IGNORED_SECRET" not in binding.loaded_env_keys
    assert "sk-test-secret" not in repr(binding.client)


def test_env_loader_missing_model_fails_closed(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ENABLE_LLM_RESEARCH=1",
                "OPENAI_API_KEY=sk-test-secret",
                "OPENAI_BASE_URL=https://llm.local.example/v1",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderFailure) as exc:
        load_real_llm_from_env(env_file=env_file, process_env={}, require_enabled=True)

    assert exc.value.code is FailureCode.MISSING_REQUIRED_FIELD


def test_env_loader_disabled_fails_closed(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=sk-test-secret",
                "OPENAI_BASE_URL=https://llm.local.example/v1",
                "LANGCHAIN_MODEL_NAME=test-json-model",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderFailure) as exc:
        load_real_llm_from_env(env_file=env_file, process_env={}, require_enabled=True)

    assert exc.value.code is FailureCode.ADAPTER_UNAVAILABLE
