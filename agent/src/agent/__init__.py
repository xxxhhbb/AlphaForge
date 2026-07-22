"""Agent core package with lazy public exports.

Importing a lightweight contract such as :mod:`src.agent.tools` must not load
providers, embeddings, HuggingFace integrations, or other optional runtime
dependencies.  Public compatibility is retained through PEP 562 lazy exports.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agent.loop import AgentLoop
    from src.agent.memory import WorkspaceMemory
    from src.agent.skills import SkillsLoader
    from src.agent.tools import BaseTool, ToolRegistry


_EXPORTS = {
    "AgentLoop": ("src.agent.loop", "AgentLoop"),
    "WorkspaceMemory": ("src.agent.memory", "WorkspaceMemory"),
    "SkillsLoader": ("src.agent.skills", "SkillsLoader"),
    "BaseTool": ("src.agent.tools", "BaseTool"),
    "ToolRegistry": ("src.agent.tools", "ToolRegistry"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
