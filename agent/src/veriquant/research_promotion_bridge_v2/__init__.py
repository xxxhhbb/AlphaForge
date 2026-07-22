"""Independent sealed promotion boundary for AlphaForge Research OS."""

from .bridge import ResearchPromotionBridgeV2
from .contracts import (
    OpaqueTerminalPromotionReceipt,
    PromotionBridgeRequest,
    PromotionBridgeTerminalState,
)

__all__ = [
    "OpaqueTerminalPromotionReceipt",
    "PromotionBridgeRequest",
    "PromotionBridgeTerminalState",
    "ResearchPromotionBridgeV2",
]
