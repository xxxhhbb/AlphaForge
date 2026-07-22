"""Narrow allowlisted importer for existing AlphaForge workbench artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import (
    FactorCostTurnoverRecord,
    ImportAudit,
    PortfolioReturnPoint,
    PortfolioScope,
    RegisteredFactorMetadata,
    RegisteredResearchContext,
    VisibleEvidenceRecord,
    VisiblePartition,
)


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("artifact timestamp must be timezone-aware")
    return parsed


def _safe_json(root: Path, name: str) -> tuple[Mapping[str, Any], str]:
    if name not in {"evidence.json", "shadow_portfolio.json"}:
        raise ValueError("artifact filename is not allowlisted")
    target = (root / name).resolve()
    if target.parent != root or not target.is_file():
        raise ValueError("allowlisted artifact is unavailable")
    payload = target.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise ValueError("artifact root must be an object")
    return value, _hash_bytes(payload)


class WorkbenchContextImporter:
    """Import facts from one explicit run directory; never scans arbitrary paths."""

    def __init__(self, run_root: str | Path, *, registered_available_at: datetime) -> None:
        self._root = Path(run_root).resolve()
        if not self._root.is_dir():
            raise ValueError("run_root must be an existing directory")
        if not isinstance(registered_available_at, datetime) or registered_available_at.tzinfo is None:
            raise ValueError("registered_available_at must be an explicit timezone-aware datetime")
        self._registered_available_at = registered_available_at

    def import_available(self) -> tuple[RegisteredResearchContext, ImportAudit]:
        evidence, evidence_source_hash = _safe_json(self._root, "evidence.json")
        official = evidence.get("official_run")
        if not isinstance(official, Mapping):
            raise ValueError("evidence.json lacks official_run")
        decision_time = _dt(str(official["decision_time"]))
        factors: list[RegisteredFactorMetadata] = []
        visible: list[VisibleEvidenceRecord] = []
        costs: list[FactorCostTurnoverRecord] = []
        for raw in official.get("factors", []):
            if not isinstance(raw, Mapping):
                raise ValueError("factor record must be an object")
            factor_id = str(raw["factor_id"])
            factor_hash = str(raw["dag_hash"])
            factors.append(RegisteredFactorMetadata(
                factor_id=factor_id, version=1, family="reference_family",
                factor_hash=factor_hash, source_hash=str(raw["factor_output_hash"]), status="REFERENCE",
                registered_at=decision_time, available_at=self._registered_available_at, source_artifact_hash=evidence_source_hash,
            ))
            for prefix, partition in (("train", VisiblePartition.TRAIN), ("validation", VisiblePartition.VALIDATION)):
                metrics = raw.get(f"{prefix}_metrics")
                evidence_hash = raw.get(f"{prefix}_evidence_hash")
                if isinstance(metrics, Mapping) and isinstance(evidence_hash, str):
                    visible.append(VisibleEvidenceRecord(
                        factor_id=factor_id, version=1, identity_hash=str(raw["identity_hash"]),
                        evidence_hash=evidence_hash, partition=partition,
                        metrics={str(k): float(v) for k, v in metrics.items()}, computed_at=self._registered_available_at,
                        available_at=self._registered_available_at, source_artifact_hash=evidence_source_hash,
                    ))
                    costs.append(FactorCostTurnoverRecord(
                        factor_id=factor_id, version=1, turnover=float(metrics.get("turnover", 0.0)),
                        spread_cost=float(metrics.get("spread_cost", 0.0)),
                        linear_impact_cost=float(metrics.get("linear_impact_cost", 0.0)),
                        as_of=decision_time, available_at=self._registered_available_at, source_artifact_hash=evidence_source_hash,
                    ))

        returns: list[PortfolioReturnPoint] = []
        source_hashes = [evidence_source_hash]
        notes = ["Imported only explicit allowlisted fields; promotion fields were not exposed."]
        shadow_path = self._root / "shadow_portfolio.json"
        if shadow_path.is_file():
            shadow, shadow_source_hash = _safe_json(self._root, "shadow_portfolio.json")
            source_hashes.append(shadow_source_hash)
            records = shadow.get("daily_records", [])
            for raw in records:
                if not isinstance(raw, Mapping) or raw.get("status") != "READY":
                    continue
                attribution = raw.get("attribution")
                if not isinstance(attribution, Mapping):
                    continue
                event_time = _dt(str(raw["decision_time"]))
                original_available_at = _dt(str(raw.get("valuation_time", raw["decision_time"])))
                available_at = max(original_available_at, self._registered_available_at)
                returns.append(PortfolioReturnPoint(
                    portfolio_id="reference-c8-shadow-replay",
                    portfolio_scope=PortfolioScope.REFERENCE_RESEARCH_PORTFOLIO,
                    event_time=event_time, available_at=available_at,
                    return_value=float(attribution["return_pct"]), nav=float(raw["nav_end"]),
                    turnover=float(raw["turnover"]), source_artifact_hash=shadow_source_hash,
                ))
            notes.append("Shadow replay is labeled REFERENCE_RESEARCH_PORTFOLIO; it is not a user portfolio.")

        context = RegisteredResearchContext(
            factors=tuple(sorted(factors, key=lambda x: (x.factor_id, x.version))),
            visible_evidence=tuple(sorted(visible, key=lambda x: (x.factor_id, x.partition.value))),
            cost_turnover=tuple(sorted(costs, key=lambda x: (x.factor_id, x.as_of, x.turnover))),
            portfolio_returns=tuple(sorted(returns, key=lambda x: x.event_time)),
        )
        counts = {
            "factors": len(context.factors), "score_history": 0,
            "visible_evidence": len(context.visible_evidence), "correlations": 0,
            "exposures": 0, "cost_turnover": len(context.cost_turnover),
            "portfolio_membership": 0, "portfolio_returns": len(context.portfolio_returns),
        }
        missing = tuple(key for key in ("score_history", "correlations", "exposures", "portfolio_membership") if counts[key] == 0)
        return context, ImportAudit(
            source_artifact_hashes=tuple(sorted(source_hashes)), imported_counts=counts,
            missing_capabilities=missing, notes=tuple(notes),
        )


__all__ = ["WorkbenchContextImporter"]
