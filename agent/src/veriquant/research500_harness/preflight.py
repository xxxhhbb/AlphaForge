"""Execution-surface preflight for the 25 representative research cases.

Static source coverage is not enough to run an experiment.  A source may have
been frozen successfully while the actual sandbox input does not expose the
fields or pathless handles needed by the generated program.  This module keeps
those two claims separate and writes a stable, fail-closed preflight artifact.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..research_toolbox.contracts import CoverageStatus, canonical_hash
from ..research_toolbox.coverage import DataCoverageResolver
from ..research_toolbox.registry import REGISTERED_CONTEXT_DATA_IDS
from .harness import CoverageInputs


SCHEMA_VERSION = "alphaforge.research500-representative-preflight/v1"


# The catalog records the data family implied by the factor family.  A research
# dimension can add further requirements (for example, industry neutralisation
# needs a dated classification even when the base factor is pure price data).
DIMENSION_DATA_ADDITIONS: Mapping[str, tuple[str, ...]] = {
    "T01": (),
    "T02": (),
    "T03": (),
    "T04": (),
    "T05": ("stock_basic",),
    "T06": ("daily_basic", "registered_factor_summary"),
    "T07": ("trade_cal",),
    "T08": ("daily_basic",),
    "T09": ("stock_basic",),
    "T10": (),
    "T11": ("trade_cal",),
    "T12": ("daily", "trade_cal"),
    "T13": ("daily", "daily_basic"),
    "T14": ("daily", "daily_basic", "trade_cal"),
    "T15": ("daily", "daily_basic"),
    "T16": ("registered_evidence", "registered_factor_summary"),
    "T17": ("registered_evidence", "registered_factor_summary"),
    "T18": ("daily", "trade_cal"),
    "T19": (),
    "T20": ("registered_evidence",),
}


DIMENSION_FEATURE_ADDITIONS: Mapping[str, tuple[str, ...]] = {
    "T05": ("industry",),
}


# Exact fields required by the selected family representative.  These are
# input-contract requirements, not hard-coded answers or formulas.
FAMILY_FEATURE_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "F01": ("adj_factor", "close"),
    "F02": ("adj_factor", "close", "turnover_rate", "volume"),
    "F03": ("adj_factor", "close"),
    "F04": ("close", "high", "low", "open"),
    "F05": ("close",),
    "F06": ("benchmark_return", "close", "market_cap"),
    "F07": ("benchmark_return", "close"),
    "F08": ("amount", "close", "market_cap", "turnover_rate", "volume"),
    "F09": ("market_cap", "turnover_rate"),
    "F10": ("amount", "close", "market_cap"),
    "F11": ("amount", "close", "high", "low", "open", "turnover_rate"),
    "F12": ("close", "pb", "pe_ttm"),
    "F13": ("assets_turn", "grossprofit_margin", "roa", "roe"),
    "F14": ("current_ratio", "debt_to_assets"),
    "F15": ("n_cashflow_act", "n_income"),
    "F16": ("n_income", "netprofit_yoy", "or_yoy", "revenue"),
    "F17": ("earnings_revision", "forecast_available_at", "forecast_value"),
    "F18": ("event_available_at", "event_type", "first_tradable_time"),
    "F19": ("buy_elg_amount", "buy_lg_amount", "net_mf_amount", "sell_elg_amount", "sell_lg_amount"),
    "F20": (),
    "F21": (),
    "F22": ("benchmark_return", "shibor_3m"),
    "F23": ("amount", "close"),
    "F24": ("close",),
    "F25": (),
}


# The current real canary supplies a frozen row panel.  Other inputs need an
# explicit pathless handle before they may be used in the sandbox.
FAMILY_RUNTIME_SURFACES: Mapping[str, tuple[str, ...]] = {
    "F06": ("registered_context",),
    "F16": ("registered_context",),
    "F17": ("event_table", "registered_context"),
    "F18": ("event_table", "auxiliary_documents"),
    "F20": ("auxiliary_documents", "registered_context"),
    "F21": ("auxiliary_documents",),
    "F25": ("registered_context",),
}


FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "assets_turn": ("assets_turn", "asset_turnover"),
    "benchmark_return": ("benchmark_return", "index_pct_change"),
    "buy_elg_amount": ("buy_elg_amount", "buy_extra_large_amount"),
    "buy_lg_amount": ("buy_lg_amount", "buy_large_amount"),
    "earnings_revision": ("earnings_revision", "net_income_yoy", "revenue_yoy"),
    "event_available_at": ("event_available_at", "source_available_at"),
    "event_type": ("event_type", "net_income_yoy", "revenue_yoy"),
    "first_tradable_time": ("first_tradable_time", "decision_time"),
    "forecast_available_at": ("forecast_available_at", "source_available_at"),
    "forecast_value": ("forecast_value", "earnings_yield", "net_income_yoy"),
    "grossprofit_margin": ("grossprofit_margin", "gross_margin"),
    "n_cashflow_act": ("n_cashflow_act", "operating_cashflow"),
    "n_income": ("n_income", "net_income"),
    "net_mf_amount": ("net_mf_amount", "net_moneyflow_amount"),
    "netprofit_yoy": ("netprofit_yoy", "net_income_yoy"),
    "or_yoy": ("or_yoy", "revenue_yoy"),
    "sell_elg_amount": ("sell_elg_amount", "sell_extra_large_amount"),
    "sell_lg_amount": ("sell_lg_amount", "sell_large_amount"),
}


_AUXILIARY_PROXY_FAMILIES = frozenset({"F17", "F18", "F20", "F21"})
_AUXILIARY_PROXY_OPTIONAL_SURFACES = frozenset({"auxiliary_documents", "event_table"})


class RepresentativePreflight:
    """Prove that a representative can reach the sandbox input boundary."""

    def __init__(self, *, resolver: DataCoverageResolver | None = None) -> None:
        self._resolver = resolver or DataCoverageResolver()

    def run(
        self,
        *,
        plan_path: str | Path,
        frozen_data_path: str | Path,
        coverage_inputs_path: str | Path,
        output_path: str | Path,
        available_runtime_surfaces: tuple[str, ...] = ("frozen_panel",),
    ) -> dict[str, Any]:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        frozen = json.loads(Path(frozen_data_path).read_text(encoding="utf-8"))
        inputs = CoverageInputs.from_mapping(
            json.loads(Path(coverage_inputs_path).read_text(encoding="utf-8"))
        )
        snapshot = frozen.get("snapshot", {})
        fields = tuple(sorted(set(snapshot.get("fields", ()))))
        raw_coverage = snapshot.get("coverage_by_field")
        if not isinstance(raw_coverage, Mapping) or set(fields) - set(raw_coverage):
            raise ValueError("frozen panel requires coverage for every declared field")
        usable_fields = tuple(sorted(
            field for field in fields
            if isinstance(raw_coverage.get(field), (int, float))
            and not isinstance(raw_coverage.get(field), bool)
            and float(raw_coverage[field]) > 0.0
        ))
        surfaces = tuple(sorted(set(available_runtime_surfaces)))
        if "frozen_panel" not in surfaces:
            raise ValueError("representative preflight requires the frozen-panel surface")
        rows = [self._row(item, fields=usable_fields, surfaces=surfaces, inputs=inputs) for item in plan["rows"]]
        value = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": "representative-preflight",
            "plan_hash": plan["artifact_hash"],
            "frozen_data_hash": canonical_hash(frozen),
            "frozen_panel_fields": list(fields),
            "usable_frozen_panel_fields": list(usable_fields),
            "available_runtime_surfaces": list(surfaces),
            "status_counts": _counts(row["status"] for row in rows),
            "failure_code_counts": _counts(
                code for row in rows for code in row["failure_codes"]
            ),
            "rows": rows,
        }
        value["artifact_hash"] = canonical_hash(value)
        _atomic_json(Path(output_path), value)
        return value

    def _row(
        self,
        row: Mapping[str, Any],
        *,
        fields: tuple[str, ...],
        surfaces: tuple[str, ...],
        inputs: CoverageInputs,
    ) -> dict[str, Any]:
        family_id = str(row["family_id"])
        test_id = str(row["test_id"])
        effective_data = tuple(sorted(set(row["required_data"]) | set(DIMENSION_DATA_ADDITIONS[test_id])))
        coverage = self._resolver.resolve(
            required_data=effective_data,
            capability_report=inputs.capability_report,
            gateway_manifest=inputs.gateway_manifest,
            auxiliary_receipts=inputs.auxiliary_receipts,
            registered_context_receipts=inputs.registered_context_receipts,
            external_approval_needs=inputs.external_approval_needs,
        )
        official_required_data = tuple(
            item for item in effective_data if item not in REGISTERED_CONTEXT_DATA_IDS
        )
        official_coverage = self._resolver.resolve(
            required_data=official_required_data,
            capability_report=inputs.capability_report,
            gateway_manifest=inputs.gateway_manifest,
            auxiliary_receipts=inputs.auxiliary_receipts,
            registered_context_receipts=inputs.registered_context_receipts,
            external_approval_needs=inputs.external_approval_needs,
        )
        required_fields = tuple(sorted(set(FAMILY_FEATURE_REQUIREMENTS[family_id]) | set(DIMENSION_FEATURE_ADDITIONS.get(test_id, ()))))
        required_surfaces = tuple(sorted({"frozen_panel", *FAMILY_RUNTIME_SURFACES.get(family_id, ())}))
        # Decision/source clocks are runner-owned columns rather than feature
        # fields, but auxiliary event proxies may use them without inventing
        # an event timestamp.
        field_set = set(fields) | {"decision_time", "source_available_at"}
        missing_fields = tuple(sorted(
            name for name in required_fields
            if not field_set.intersection(FIELD_ALIASES.get(name, (name,)))
        ))
        missing_surfaces = tuple(sorted(set(required_surfaces) - set(surfaces)))
        proxy_mode = (
            family_id in _AUXILIARY_PROXY_FAMILIES
            and row["required_output_kind"] != "ScoreTable"
            and not missing_fields
            and "frozen_panel" in surfaces
        )
        if proxy_mode:
            missing_surfaces = tuple(
                name for name in missing_surfaces
                if name not in _AUXILIARY_PROXY_OPTIONAL_SURFACES
            )
        failures: list[str] = []
        if coverage.status in {CoverageStatus.UNRESOLVED, CoverageStatus.NEEDS_EXTERNAL_APPROVAL} and not proxy_mode:
            failures.append("DATA_SOURCE_UNRESOLVED")
        if row["required_output_kind"] == "ScoreTable" and official_coverage.status is not CoverageStatus.OFFICIAL_READY:
            failures.append("OFFICIAL_SOURCE_REQUIRED")
        if missing_fields:
            failures.append("PROJECTED_FIELD_MISSING")
        if missing_surfaces:
            failures.append("RUNTIME_SURFACE_MISSING")
        output = {
            "question_id": row["question_id"],
            "question_hash": row["question_hash"],
            "family_id": family_id,
            "test_id": test_id,
            "required_output_kind": row["required_output_kind"],
            "catalog_required_data": list(row["required_data"]),
            "effective_required_data": list(effective_data),
            "required_feature_fields": list(required_fields),
            "missing_feature_fields": list(missing_fields),
            "required_runtime_surfaces": list(required_surfaces),
            "missing_runtime_surfaces": list(missing_surfaces),
            "source_coverage_status": coverage.status.value,
            "source_coverage_hash": coverage.artifact_hash,
            "source_failure_codes": [item.value for item in coverage.failure_codes],
            "official_required_data": list(official_required_data),
            "official_source_coverage_status": official_coverage.status.value,
            "official_source_coverage_hash": official_coverage.artifact_hash,
            "official_source_failure_codes": [
                item.value for item in official_coverage.failure_codes
            ],
            "research_input_mode": "AUXILIARY_PROXY" if proxy_mode else "DIRECT",
            "proxy_disclosure_required": proxy_mode,
            "status": "READY" if not failures else "BLOCKED",
            "failure_codes": sorted(set(failures)),
        }
        output["row_hash"] = canonical_hash(output)
        return output


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight representative E2E research cases")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--frozen-data", required=True)
    parser.add_argument("--coverage-inputs", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-surfaces", default="frozen_panel")
    args = parser.parse_args(argv)
    result = RepresentativePreflight().run(
        plan_path=args.plan,
        frozen_data_path=args.frozen_data,
        coverage_inputs_path=args.coverage_inputs,
        output_path=args.output,
        available_runtime_surfaces=tuple(
            item.strip() for item in args.runtime_surfaces.split(",") if item.strip()
        ),
    )
    print(json.dumps({
        "status_counts": result["status_counts"],
        "failure_code_counts": result["failure_code_counts"],
        "artifact_hash": result["artifact_hash"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status_counts"].get("BLOCKED", 0) == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DIMENSION_DATA_ADDITIONS",
    "FAMILY_FEATURE_REQUIREMENTS",
    "FAMILY_RUNTIME_SURFACES",
    "FIELD_ALIASES",
    "RepresentativePreflight",
]
