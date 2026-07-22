"""Agent D integration surface; not registered in the public package yet."""

from .gate import NoPromotionReceiptProvider, RealReplayReadinessGate
from .models import PromotionChallengerReceipt, RealReplayGateResult, RealReplayGateStatus
from .protocols import IndependentPromotionReceiptProvider

__all__ = [
    "IndependentPromotionReceiptProvider",
    "NoPromotionReceiptProvider",
    "PromotionChallengerReceipt",
    "RealReplayGateResult",
    "RealReplayGateStatus",
    "RealReplayReadinessGate",
]
