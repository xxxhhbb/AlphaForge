"""Visible-only Qlib bridge adapter for ResearchDatasetBundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from veriquant.adapters.qlib import (
    AdapterFailure,
    OptionalPyQlibAdapter,
    QlibCostConfig,
    QlibDateSegment,
    QlibManifest,
    QlibVisibleResult,
    QlibVisibleRow,
)
from veriquant.contracts import FailureCode
from veriquant.contracts.base import fail, require_hash
from veriquant.data import canonical_content_hash
from veriquant.factors import CompiledFactorDAG

from .models import QlibBridgeRequest, QlibBridgeUnavailable, ResearchDatasetBundle


@dataclass(frozen=True, kw_only=True)
class QlibBridgeMaterialization:
    manifest: QlibManifest
    visible_rows: tuple[QlibVisibleRow, ...]
    request: QlibBridgeRequest


class QlibBridgeAdapter:
    """Convert bridge bundles to the existing visible-only Qlib adapter shape."""

    def build_visible_request(
        self,
        *,
        bundle: ResearchDatasetBundle,
        provider_uri: str | Path,
        score_field: str = "close",
        model: str = "LightGBM",
        strategy: str = "TopkDropoutStrategy",
        seed: int = 0,
        cost: QlibCostConfig | None = None,
    ) -> QlibBridgeMaterialization:
        if not isinstance(bundle, ResearchDatasetBundle):
            fail(FailureCode.INVALID_VALUE, "bundle must be ResearchDatasetBundle")
        if score_field not in bundle.feature_fields:
            fail(FailureCode.UNKNOWN_FIELD, "score_field must be an existing feature field")
        provider = Path(provider_uri).resolve()
        cost_config = cost or QlibCostConfig(
            open_cost_bps=5.0,
            close_cost_bps=5.0,
            linear_impact_bps=2.0,
            minimum_cost=0.0,
        )
        manifest = QlibManifest(
            provider_uri=str(provider),
            region="cn",
            universe=bundle.universe_manifest.symbols,
            train=QlibDateSegment(
                start=bundle.split_manifest.train_start,
                end=bundle.split_manifest.train_end,
            ),
            validation=QlibDateSegment(
                start=bundle.split_manifest.validation_start,
                end=bundle.split_manifest.validation_end,
            ),
            test=QlibDateSegment(
                start=bundle.split_manifest.sealed_start,
                end=bundle.split_manifest.sealed_end,
            ),
            label=bundle.label_specification.name,
            model=model,
            strategy=strategy,
            cost=cost_config,
            seed=seed,
        )
        labels = {
            (row.event_time, row.symbol): row
            for row in bundle.label_rows
            if row.partition in {"train", "validation"}
        }
        rows: list[QlibVisibleRow] = []
        for feature in bundle.feature_rows:
            if feature.partition not in {"train", "validation"}:
                continue
            label = labels.get((feature.event_time, feature.symbol))
            if label is None:
                fail(FailureCode.INCOMPLETE_RESULT, "visible feature row lacks a label")
            rows.append(
                QlibVisibleRow(
                    partition=feature.partition,
                    event_time=feature.event_time,
                    symbol=feature.symbol,
                    score=float(feature.values[score_field]),
                    label=label.value,
                )
            )
        train_rows = sum(1 for row in rows if row.partition == "train")
        validation_rows = sum(1 for row in rows if row.partition == "validation")
        if train_rows == 0 or validation_rows == 0:
            fail(FailureCode.INCOMPLETE_RESULT, "Qlib visible request needs train and validation rows")
        visible_rows = tuple(sorted(rows, key=lambda row: (row.partition, row.event_time, row.symbol)))
        request = QlibBridgeRequest(
            bundle_hash=bundle.artifact_hash,
            qlib_manifest_hash=manifest.artifact_hash,
            visible_rows_hash=canonical_content_hash(visible_rows),
            visible_train_rows=train_rows,
            visible_validation_rows=validation_rows,
            provider_uri=str(provider),
            status="AUXILIARY_VISIBLE_REQUEST",
        )
        return QlibBridgeMaterialization(
            manifest=manifest,
            visible_rows=visible_rows,
            request=request,
        )

    def run_optional_visible(
        self,
        *,
        bundle: ResearchDatasetBundle,
        provider_uri: str | Path,
        candidate: CompiledFactorDAG,
        score_field: str = "close",
        adapter: OptionalPyQlibAdapter | None = None,
    ) -> QlibVisibleResult | QlibBridgeUnavailable:
        if not isinstance(candidate, CompiledFactorDAG):
            fail(FailureCode.INVALID_FACTOR_DAG, "candidate must be a compiled FactorDAG")
        materialized = self.build_visible_request(
            bundle=bundle,
            provider_uri=provider_uri,
            score_field=score_field,
        )
        qlib_adapter = adapter or OptionalPyQlibAdapter()
        result = qlib_adapter.run_visible(
            manifest=materialized.manifest,
            candidate=candidate,
            rows=materialized.visible_rows,
            as_of=bundle.decision_time,
        )
        if isinstance(result, AdapterFailure):
            code = result.code
            if code not in {FailureCode.ADAPTER_UNAVAILABLE, FailureCode.QLIB_DATA_UNAVAILABLE}:
                code = FailureCode.ADAPTER_UNAVAILABLE
            return QlibBridgeUnavailable(
                adapter=result.adapter,
                status="UNAVAILABLE",
                code=code,
                message=result.message,
                bundle_hash=bundle.artifact_hash,
            )
        return result


def visible_rows_by_partition(rows: tuple[QlibVisibleRow, ...]) -> Mapping[str, int]:
    """Small deterministic helper used by tests and integration adapters."""

    counts = {"train": 0, "validation": 0}
    for row in rows:
        if row.partition not in counts:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed/test rows are not visible to Qlib")
        counts[row.partition] += 1
    return counts
