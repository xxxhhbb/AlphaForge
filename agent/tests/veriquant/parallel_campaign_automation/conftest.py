"""Local isolation for Campaign automation tests.

The shared test fixture imports ``src.config`` before every test.  This package
does not need runtime configuration, and the local environment may carry
Pydantic v1 while the broader application config expects v2.
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

