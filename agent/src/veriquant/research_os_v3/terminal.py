"""Compatibility command forwarding to the unique Agent research product.

Historical ResearchOS V3 services remain importable for artifact replay, but
new terminal research always uses Vibe AgentLoop, the single review gateway,
isolated execution and independent Evidence.
"""

from __future__ import annotations

from ..agent_research_product.terminal import main, run


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
