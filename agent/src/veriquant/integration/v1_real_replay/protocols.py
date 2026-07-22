"""Minimal ports; Promotion remains outside this package's authority."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import PromotionChallengerReceipt


@runtime_checkable
class IndependentPromotionReceiptProvider(Protocol):
    def challenger_for_official_run(self, official_run_hash: str) -> PromotionChallengerReceipt | None: ...

