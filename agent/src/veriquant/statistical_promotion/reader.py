"""EvidenceReader protocol and sealed-response envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import (
    CandidateEvidenceView,
    FactorFamilyManifest,
    PromotionContract,
    StatisticalPromotionFailureCode,
    _require_hash,
    _require_identifier,
)


@dataclass(frozen=True, kw_only=True)
class SealedAggregateHandle(PromotionContract):
    """Opaque sealed handle metadata.

    The handle id is a capability token.  It is never copied into promotion
    decisions or audit manifests; only its hash is recorded.
    """

    handle_id: str
    handle_hash: str
    candidate_id: str
    candidate_version: str
    input_hash: str
    policy_hash: str
    issued_by: str = "SEALED_PROMOTION_READER"

    def __post_init__(self) -> None:
        for field_name in ("handle_id", "candidate_id", "candidate_version", "issued_by"):
            _require_identifier(getattr(self, field_name), field_name)
        for field_name in ("handle_hash", "input_hash", "policy_hash"):
            _require_hash(getattr(self, field_name), field_name)


@dataclass(frozen=True, kw_only=True)
class SealedDecisionResponse(PromotionContract):
    """Redacted terminal response from the sealed authority."""

    request_hash: str
    passed: bool
    failure_code: StatisticalPromotionFailureCode | None = None
    produced_by: str = "SEALED_PROMOTION_READER"

    def __post_init__(self) -> None:
        _require_hash(self.request_hash, "request_hash")
        if type(self.passed) is not bool:
            raise TypeError("passed must be bool")
        if self.produced_by != "SEALED_PROMOTION_READER":
            raise ValueError("sealed decision response must come from sealed reader")
        if self.passed and self.failure_code is not None:
            raise ValueError("passing sealed response cannot include a failure code")


class EvidenceReader(Protocol):
    """Read-only bridge to a future Evidence Service.

    Implementations must not expose raw sealed rows, sealed ranks, other
    candidates' sealed metrics, or private promotion reasons.
    """

    @property
    def reader_id(self) -> str:
        ...

    def read_visible_evidence(
        self,
        *,
        candidate_id: str,
        candidate_version: str,
    ) -> CandidateEvidenceView:
        ...

    def read_family_manifest(
        self,
        *,
        family_id: str,
        manifest_version: str,
    ) -> FactorFamilyManifest:
        ...

    def read_family_visible_evidence(
        self,
        *,
        manifest: FactorFamilyManifest,
    ) -> tuple[CandidateEvidenceView, ...]:
        ...

    def read_sealed_handle(
        self,
        *,
        candidate: CandidateEvidenceView,
        input_hash: str,
        policy_hash: str,
    ) -> SealedAggregateHandle:
        ...

    def read_authorized_sealed_decision(
        self,
        *,
        handle: SealedAggregateHandle,
        request_hash: str,
    ) -> SealedDecisionResponse:
        ...

