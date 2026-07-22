from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_env_config():
    """Operator API tests do not import EnvConfig or need pydantic-backed setup."""

    yield

