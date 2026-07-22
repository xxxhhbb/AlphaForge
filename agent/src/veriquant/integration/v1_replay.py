"""Thin adapters for the durable 20-session shadow replay package."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import require_aware_datetime, require_hash, require_identifier
from veriquant.data import canonical_content_hash
from veriquant.real_ashare_data.reader import FrozenSnapshotHandle, MarketVintage
from veriquant.shadow_portfolio.api import DataVintage
from veriquant.shadow_replay.api import ReplayChallengerCandidate, ShadowReplayPolicy


@dataclass(frozen=True, kw_only=True)
class FrozenVintageBinding:
    snapshot_id: str
    manifest_path: str
    decision_time: datetime
    revision_id: str

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_identifier(self.revision_id, "revision_id")
        require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.manifest_path, str) or not self.manifest_path:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "manifest_path is required")


@dataclass(frozen=True, kw_only=True)
class FrozenVintageMetadata:
    corporate_action_available_time: datetime
    beta_by_symbol: Mapping[str, float]
    industry_by_symbol: Mapping[str, str]
    adv_by_symbol: Mapping[str, float]

    def __post_init__(self) -> None:
        require_aware_datetime(self.corporate_action_available_time, "corporate_action_available_time")
        object.__setattr__(self, "beta_by_symbol", MappingProxyType(dict(self.beta_by_symbol)))
        object.__setattr__(self, "industry_by_symbol", MappingProxyType(dict(self.industry_by_symbol)))
        object.__setattr__(self, "adv_by_symbol", MappingProxyType(dict(self.adv_by_symbol)))


class MarketVintageReader(Protocol):
    def open(self, manifest_path: str) -> FrozenSnapshotHandle: ...

    def market_vintage(self, handle: FrozenSnapshotHandle, *, decision_time: datetime) -> MarketVintage: ...


class VintageMetadataProvider(Protocol):
    """Read-only metadata authority; integration never invents missing values."""

    def metadata_for(self, *, binding: FrozenVintageBinding, vintage: MarketVintage) -> FrozenVintageMetadata: ...


class FrozenReaderHistoricalVintageProvider:
    """Map Agent-A PIT vintages to Agent-C replay vintages.

    Every daily vintage is re-read and hash-verified by Agent A.  Revisions use
    distinct bindings and therefore distinct content identities; prior records
    are never overwritten.
    """

    def __init__(
        self,
        *,
        reader: MarketVintageReader,
        bindings: Sequence[FrozenVintageBinding],
        metadata_provider: VintageMetadataProvider,
    ) -> None:
        if not bindings:
            raise ContractValidationError(FailureCode.MISSING_REQUIRED_FIELD, "vintage bindings are required")
        ordered = tuple(sorted(bindings, key=lambda item: (item.decision_time, item.snapshot_id, item.revision_id)))
        identities = {(item.snapshot_id, item.decision_time, item.revision_id) for item in ordered}
        if len(identities) != len(ordered):
            raise ContractValidationError(FailureCode.ARTIFACT_COLLISION, "duplicate vintage binding")
        self._reader = reader
        self._bindings = ordered
        self._metadata_provider = metadata_provider

    def list_vintages(self, policy: ShadowReplayPolicy) -> tuple[DataVintage, ...]:
        if not isinstance(policy, ShadowReplayPolicy):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "ShadowReplayPolicy is required")
        output: list[DataVintage] = []
        for binding in self._bindings:
            handle = self._reader.open(binding.manifest_path)
            vintage = self._reader.market_vintage(handle, decision_time=binding.decision_time)
            if vintage.snapshot_hash != handle.snapshot_hash:
                raise ContractValidationError(FailureCode.INVALID_HASH, "market vintage changed under verified handle")
            metadata = self._metadata_provider.metadata_for(binding=binding, vintage=vintage)
            if metadata.corporate_action_available_time > binding.decision_time:
                raise ContractValidationError(
                    FailureCode.INVALID_TIME_ORDER,
                    "corporate-action metadata is unavailable at decision time",
                )
            symbols = tuple(sorted({row.symbol for row in vintage.rows}))
            for name, values in (
                ("beta_by_symbol", metadata.beta_by_symbol),
                ("industry_by_symbol", metadata.industry_by_symbol),
                ("adv_by_symbol", metadata.adv_by_symbol),
            ):
                missing = set(symbols) - set(values)
                if missing:
                    raise ContractValidationError(
                        FailureCode.MISSING_REQUIRED_FIELD,
                        f"{name} missing PIT-visible symbols",
                        details={"symbols": tuple(sorted(missing))},
                    )
            coverage = {
                symbol: sum(
                    value is not None
                    for row in vintage.rows
                    if row.symbol == symbol
                    for value in row.values.values()
                )
                / max(1, sum(len(row.values) for row in vintage.rows if row.symbol == symbol))
                for symbol in symbols
            }
            available_time = max(row.available_at for row in vintage.rows)
            output.append(
                DataVintage(
                    vintage_id=_vintage_id(binding, vintage),
                    revision_id=binding.revision_id,
                    dataset_hash=vintage.market_state_hash,
                    universe=symbols,
                    data_available_time=available_time,
                    decision_time=binding.decision_time,
                    corporate_action_available_time=metadata.corporate_action_available_time,
                    coverage_by_symbol=coverage,
                    beta_by_symbol={symbol: metadata.beta_by_symbol[symbol] for symbol in symbols},
                    industry_by_symbol={symbol: metadata.industry_by_symbol[symbol] for symbol in symbols},
                    adv_by_symbol={symbol: metadata.adv_by_symbol[symbol] for symbol in symbols},
                )
            )
        if len(output) < policy.trading_days:
            raise ContractValidationError(
                FailureCode.INCOMPLETE_RESULT,
                "fewer frozen vintages than replay policy requires",
                details={"available": len(output), "required": policy.trading_days},
            )
        return tuple(output)


def _vintage_id(binding: FrozenVintageBinding, vintage: MarketVintage) -> str:
    digest = canonical_content_hash(
        {
            "snapshot_id": binding.snapshot_id,
            "revision_id": binding.revision_id,
            "decision_time": binding.decision_time,
            "market_state_hash": vintage.market_state_hash,
        }
    ).removeprefix("sha256:")[:16]
    return f"vintage-{binding.decision_time.date().isoformat()}-{digest}"


class PromotionChallengerProvider:
    """Read-only projection of independently promoted CHALLENGER records."""

    def __init__(self, candidates: Sequence[ReplayChallengerCandidate]) -> None:
        self._candidates = tuple(candidates)
        for item in self._candidates:
            if not isinstance(item, ReplayChallengerCandidate):
                raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid challenger projection")
            require_hash(item.promotion_decision_hash, "promotion_decision_hash")

    def get_candidates(
        self,
        policy: ShadowReplayPolicy,
        vintage: DataVintage,
    ) -> tuple[ReplayChallengerCandidate, ...]:
        return tuple(
            item
            for item in self._candidates
            if item.policy_version == policy.policy_version
            and item.policy_hash == policy.policy_hash
            and item.available_time <= vintage.decision_time
        )

