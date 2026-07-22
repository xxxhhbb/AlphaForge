"""Deterministic qualification of the six real Vibe-AgentLoop specialists."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..agent_research_product.contracts import ProductRunResult
from ..agent_research_sandbox.report import ChineseResearchReportComposer, CustomerResearchReport
from ..research_data_gateway.models import canonical_hash


_GROUPS = (
    "market_structure",
    "fundamental",
    "expectations_events",
    "alternative_data",
    "macro_regime",
    "meta_alpha",
)
_NATIVE_FIELDS: Mapping[str, frozenset[str]] = {
    "market_structure": frozenset({
        "open", "high", "low", "close", "volume", "amount", "turnover_rate",
        "pct_chg", "adj_factor", "amihud", "volatility",
    }),
    "fundamental": frozenset({
        "pe", "pe_ttm", "pb", "ps", "dv_ratio", "market_cap", "total_mv",
        "roe", "roa", "gross_margin", "revenue", "net_income",
        "debt_to_assets", "operating_cashflow", "ocf_to_profit",
    }),
    "expectations_events": frozenset({
        "express_revenue", "express_net_income", "express_net_income_yoy",
        "express_diluted_eps", "revenue_yoy", "net_income_yoy", "forecast_type",
        "forecast_change_min", "forecast_change_max",
    }),
    "macro_regime": frozenset({
        "index_close", "index_pct_change", "index_return", "shibor_overnight",
        "shibor_1w", "shibor_1m", "macro_regime", "market_volatility",
    }),
    "meta_alpha": frozenset({
        "registered_momentum_20d", "registered_size_zscore", "market_cap",
        "industry", "portfolio_residual", "registered_factor_score",
    }),
    "alternative_data": frozenset({
        "news_count_20_sessions", "research_report_count_60_sessions",
        "news_sentiment", "research_report_count", "stock_news_count",
    }),
}


class SpecialistQualificationHarness:
    def __init__(self, *, runs_root: str | Path, output_root: str | Path) -> None:
        self.runs_root = Path(runs_root).resolve()
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    def run(self) -> Mapping[str, Any]:
        groups: dict[str, Any] = {}
        for group in _GROUPS:
            candidates = self._candidates(group)
            groups[group] = self._qualify(group, candidates)
        official_count = sum(
            item["qualification_status"] == "OFFICIAL_RESEARCH_QUALIFIED"
            for item in groups.values()
        )
        auxiliary_count = sum(
            item["qualification_status"] == "AUXILIARY_RESEARCH_QUALIFIED"
            for item in groups.values()
        )
        blocked = sorted(
            group for group, item in groups.items()
            if item["qualification_status"] == "BLOCKED"
        )
        status = (
            "QUALIFIED_FOR_RESEARCH_PRODUCT"
            if official_count >= 5 and auxiliary_count >= 1 and not blocked
            else "PARTIAL"
        )
        payload: dict[str, Any] = {
            "schema_version": "alphaforge.specialist-qualification/v2",
            "status": status,
            "official_research_qualified_count": official_count,
            "auxiliary_research_qualified_count": auxiliary_count,
            "blocked_groups": blocked,
            "groups": groups,
            "policy": {
                "official_requires": "real AgentLoop + native frozen field + OS sandbox + independent Evidence",
                "auxiliary_requires": "real AgentLoop + real read-only Vibe tool receipt + OS sandbox + complete report",
                "negative_result_is_capability_pass": True,
                "auxiliary_never_enters_evidence_court": True,
            },
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.output_root / "specialist_qualification_matrix.json", payload)
        return payload

    def _candidates(self, group: str) -> list[Path]:
        values: list[Path] = []
        for path in self.runs_root.rglob("product_result.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            experiments = raw.get("experiments") if isinstance(raw, Mapping) else None
            if isinstance(experiments, list) and any(
                isinstance(item, Mapping) and item.get("specialist_id") == group
                for item in experiments
            ):
                values.append(path)
        return sorted(values)

    def _qualify(self, group: str, candidates: list[Path]) -> Mapping[str, Any]:
        inspected = [self._inspect(group, path) for path in candidates]
        inspected.sort(key=lambda item: (int(item["score"]), str(item["product_result_path"])))
        selected = inspected[-1] if inspected else None
        if selected is None:
            return {
                "qualification_status": "BLOCKED",
                "failure_codes": ["REAL_SPECIALIST_RECEIPT_MISSING"],
                "superseded_runs": [],
            }
        superseded = [
            {
                "product_result_path": item["product_result_path"],
                "failure_codes": item["failure_codes"],
            }
            for item in inspected[:-1]
        ]
        result = dict(selected)
        result.pop("score", None)
        result["superseded_runs"] = superseded
        return result

    def _inspect(self, group: str, path: Path) -> dict[str, Any]:
        failures: list[str] = []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            result = ProductRunResult.from_dict(raw)
        except Exception:
            return {
                "score": 0,
                "qualification_status": "BLOCKED",
                "failure_codes": ["PRODUCT_RESULT_CORRUPT"],
                "product_result_path": str(path),
            }
        experiments = [item for item in result.experiments if item.specialist_id == group]
        if len(experiments) != 1:
            failures.append("SPECIALIST_EXPERIMENT_IDENTITY_INVALID")
            experiment = None
        else:
            experiment = experiments[0]
        run_dir = path.parent
        canary_root = path.parents[2]
        source_fields: tuple[str, ...] = ()
        source_path: Path | None = None
        if experiment is not None:
            digest = experiment.source_hash[7:]
            source_path = canary_root / "draft_sources" / digest[:2] / digest[2:]
            if not source_path.is_file() or _sha256_text(source_path) != experiment.source_hash:
                failures.append("SOURCE_IDENTITY_INVALID")
            else:
                source_fields = _source_string_literals(source_path)
            review = _read_json(run_dir / "experiments" / experiment.experiment_id / "review.json")
            runner = _read_json(run_dir / "experiments" / experiment.experiment_id / "runner.json")
            if review.get("status") != "ACCEPTED" or review.get("source_hash") != experiment.source_hash:
                failures.append("MECHANICAL_REVIEW_INVALID")
            if runner.get("status") != "COMPLETED_AUXILIARY" or runner.get("isolation_level") != "OS_SANDBOXED":
                failures.append("OS_SANDBOX_RECEIPT_INVALID")
        report_path = Path(result.report_path)
        report_view_path: Path | None = None
        report_view_hash: str | None = None
        if not report_path.is_file():
            failures.append("REPORT_MISSING")
        else:
            try:
                report_contract = CustomerResearchReport.from_dict(
                    _read_json(report_path.with_name("report.json"))
                )
                text = ChineseResearchReportComposer().render(report_contract)
                report_view_path = (
                    self.output_root / "customer_report_views" / group
                    / report_contract.artifact_hash.removeprefix("sha256:") / "report.md"
                )
                _atomic_text(report_view_path, text)
                report_view_hash = _sha256_text(report_view_path)
            except Exception:
                failures.append("REPORT_CONTRACT_OR_VIEW_INVALID")
                text = report_path.read_text(encoding="utf-8")
            if not all(token in text for token in ("研究问题", "结论", "数据", "复现")):
                failures.append("REPORT_NOT_CUSTOMER_COMPLETE")
        native_used = sorted(set(source_fields) & _NATIVE_FIELDS[group])
        dynamic_tools: tuple[str, ...] = ()
        outcome_path = canary_root / result.run_id / "branches" / f"task_{group}" / "outcome.json"
        outcome = _read_json(outcome_path)
        outcome_payload = outcome.get("payload") if isinstance(outcome.get("payload"), Mapping) else {}
        receipts = outcome_payload.get("investigation_receipts") if isinstance(outcome_payload, Mapping) else []
        if isinstance(receipts, list):
            dynamic_tools = tuple(sorted({
                str(item.get("tool_name"))
                for item in receipts if isinstance(item, Mapping)
                and str(item.get("tool_name", "")).startswith("get_")
            }))
        authority = "NONE" if experiment is None else (
            "OFFICIAL_EVIDENCE" if experiment.official_evidence_hash else
            "AUXILIARY_ONLY" if experiment.research_evidence_hash else "NONE"
        )
        if result.lifecycle != "COMPLETED":
            failures.append("REAL_RUN_NOT_COMPLETED")
        if group == "alternative_data":
            if not {"get_stock_news", "get_research_reports"}.issubset(dynamic_tools):
                failures.append("NATIVE_VIBE_TOOLS_NOT_USED")
            if authority != "AUXILIARY_ONLY":
                failures.append("ALTERNATIVE_AUTHORITY_NOT_AUXILIARY")
            if experiment is not None and experiment.court_outcome is not None:
                failures.append("AUXILIARY_ENTERED_EVIDENCE_COURT")
            status = "AUXILIARY_RESEARCH_QUALIFIED" if not failures else "BLOCKED"
        else:
            if not native_used:
                failures.append("NATIVE_FROZEN_FIELD_NOT_USED")
            if authority != "OFFICIAL_EVIDENCE":
                failures.append("OFFICIAL_EVIDENCE_MISSING")
            status = "OFFICIAL_RESEARCH_QUALIFIED" if not failures else "BLOCKED"
        score = (
            (100 if result.lifecycle == "COMPLETED" else 0)
            + (60 if status == "OFFICIAL_RESEARCH_QUALIFIED" else 50 if status == "AUXILIARY_RESEARCH_QUALIFIED" else 0)
            - 10 * len(failures)
        )
        return {
            "score": score,
            "qualification_status": status,
            "failure_codes": sorted(set(failures)),
            "run_id": result.run_id,
            "result_kind": result.result_kind.value,
            "evidence_authority": authority,
            "court_outcome": None if experiment is None else experiment.court_outcome,
            "source_hash": None if experiment is None else experiment.source_hash,
            "native_fields_used": native_used,
            "dynamic_vibe_tools_used": list(dynamic_tools),
            "report_hash": result.report_hash,
            "report_path": result.report_path,
            "customer_report_view_path": None if report_view_path is None else str(report_view_path),
            "customer_report_view_hash": report_view_hash,
            "product_result_path": str(path),
            "outcome_path": str(outcome_path),
        }


def _source_string_literals(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return tuple(sorted({
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }))


def _sha256_text(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify six real Vibe-AgentLoop specialists.")
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = SpecialistQualificationHarness(
        runs_root=args.runs_root,
        output_root=args.output_root,
    ).run()
    print(json.dumps(dict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "QUALIFIED_FOR_RESEARCH_PRODUCT" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SpecialistQualificationHarness"]
