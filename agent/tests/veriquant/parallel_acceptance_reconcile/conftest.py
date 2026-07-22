"""Local isolation for acceptance-reconcile tests.

The repository-level test fixture imports ``src.config`` before every test.
This package is a pure stdlib read model and does not need runtime config, so
the local tests provide the tiny reset hook required by the shared fixture
without importing unrelated pydantic-backed configuration code.
"""

from __future__ import annotations

import sys
import types


config_module = types.ModuleType("src.config")
accessor_module = types.ModuleType("src.config.accessor")


def reset_env_config() -> None:
    return None


accessor_module.reset_env_config = reset_env_config
config_module.accessor = accessor_module
sys.modules.setdefault("src.config", config_module)
sys.modules.setdefault("src.config.accessor", accessor_module)
