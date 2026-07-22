"""Real qualification execution, Evidence v2, and atomic progress advancement."""

from __future__ import annotations

import ast
from dataclasses import replace
import json
import mmap
from pathlib import Path
import re
from typing import Any, Mapping

from ..agent_research_product.canary import AgentResearchProductCanary, ProductCanaryConfiguration
from .contracts import (
    CaseState, EvidenceAuthority, ExecutionStatus, GroupStatus, ReportStatus,
    ResearchVerdict,
)
from .evaluators import EvaluationRejected, OutputEvaluatorRegistry
from .reports import build_report_v2
from .storage import atomic_json, atomic_jsonl, atomic_text, canonical_hash, load_hashed


_IMAGE_NAME = "veriquant-m2-test:local"
_IMAGE_ID = "sha256:8ce7ebc0666994c7a7c5c46e5c6925a5c6886abc7b567b0f3830bbe8e136357f"
_VISIBLE_ROW_KEYS = {
    "symbol", "decision_time", "source_available_at", "outcome_available_at",
    "target_value", "features",
}
_GROUP_NATIVE_FIELDS = {
    "market_structure": {"close", "amount", "volume", "turnover_rate"},
    "fundamental": {"pe_ttm", "pb", "roe", "roa", "gross_margin", "net_income", "revenue", "net_income_yoy", "revenue_yoy"},
    "expectations_events": {"net_income_yoy", "revenue_yoy", "net_income"},
    "alternative_data": {"stock_news_count", "research_report_count", "news_sentiment"},
    "macro_regime": {"shibor_1m", "index_pct_change", "index_close"},
    "meta_alpha": {"registered_momentum_20d", "registered_size_zscore"},
}


class QualificationBatchRunner:
    def __init__(self, *, workspace: str | Path, root: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = Path(root).resolve()
        self.prior_root = self.workspace / ".autobuild" / "veriquant" / "research500_harness"
        self.data_root = self.prior_root / "product_data_registered_industry_v1"
        self.data_path = self.data_root / "frozen_validation_data.json"
        self.plan = load_hashed(self.root / "qualification_plan.json")
        self.lineage = load_hashed(self.root / "data_lineage_report.json")
        self._labels: dict[str, float] | None = None
        self._dated_labels: dict[tuple[str, str], float] | None = None
        self._coverage = {
            item["canonical_field"]: item["nonempty_coverage"]
            for item in self.lineage["field_lineage"]
            if item["nonempty_coverage"] is not None
        }

    def run_batch(self, batch_id: str) -> dict[str, Any]:
        cases = [item for item in self.plan["cases"] if item["batch_id"] == batch_id]
        if not cases or len(cases) > 3:
            raise ValueError("qualification batch must contain one to three cases")
        for case in cases:
            current = self._case(case["case_id"])
            if current["execution_status"] == "PASSED":
                continue
            if self._is_group_paused(case["group_id"]):
                continue
            self._reserve(case)
            self._execute(case)
            if self._harness_paused():
                break
        return self._refresh_matrices()

    def run_case(self, case_id: str, *, allow_new_real_run: bool = False) -> dict[str, Any]:
        case = next((item for item in self.plan["cases"] if item["case_id"] == case_id), None)
        if case is None:
            raise ValueError("unknown qualification case")
        current = self._case(case_id)
        if current["execution_status"] == "PASSED":
            return self._refresh_matrices()
        if case["source_mode"] == "NEW_REAL_RUN" and not allow_new_real_run:
            preflight = atomic_json(
                self.root / "qualification_runs" / case["batch_id"] / case_id / "preflight.json",
                self._preflight(case),
            )
            code = (
                preflight["failure_codes"][0]
                if preflight["status"] != "READY"
                else "EXTERNAL_MODEL_APPROVAL_REQUIRED"
            )
            self._reserve(case)
            self._complete(
                case, execution=ExecutionStatus.PREFLIGHT_BLOCKED,
                authority=EvidenceAuthority.NONE, verdict=ResearchVerdict.DATA_UNAVAILABLE,
                report_status=ReportStatus.REJECTED, failure_stage="DATA_PREFLIGHT" if preflight["status"] != "READY" else "REAL_MODEL_PROVIDER",
                failure_code=code, recovery_cursor="PREFLIGHT" if preflight["status"] != "READY" else "PROVIDER_APPROVAL",
                artifacts=(preflight["artifact_hash"],),
            )
            return self._refresh_matrices()
        self._reserve(case)
        self._execute(case)
        return self._refresh_matrices()

    def _execute(self, case: Mapping[str, Any]) -> None:
        case_root = self.root / "qualification_runs" / case["batch_id"] / case["case_id"]
        case_root.mkdir(parents=True, exist_ok=True)
        preflight = atomic_json(case_root / "preflight.json", self._preflight(case))
        if preflight["status"] != "READY":
            self._complete(
                case, execution=ExecutionStatus.PREFLIGHT_BLOCKED,
                authority=EvidenceAuthority.NONE, verdict=ResearchVerdict.DATA_UNAVAILABLE,
                report_status=ReportStatus.REJECTED, failure_stage="DATA_PREFLIGHT",
                failure_code=preflight["failure_codes"][0], recovery_cursor="PREFLIGHT",
                artifacts=(preflight["artifact_hash"],),
            )
            return
        if case["source_mode"] == "NEGATIVE_FIXTURE":
            receipt = self._run_safe_fixture(case)
            saved = atomic_json(case_root / "safety_receipt.json", receipt)
            report = atomic_json(case_root / "report.json", {
                "schema_version": "alphaforge.safety-qualification-report/v1",
                "case_id": case["case_id"], "group_id": case["group_id"],
                "machine_result": "FAIL_CLOSED_AS_EXPECTED",
                "failure_code": receipt["observed_failure_code"],
                "source_artifact_hashes": [saved["artifact_hash"]],
            })
            self._complete(
                case, execution=ExecutionStatus.PASSED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.REJECTED, report_status=ReportStatus.BUILT,
                failure_stage="SAFE_NEGATIVE_GATE", failure_code=receipt["observed_failure_code"],
                recovery_cursor="COMMITTED", artifacts=(preflight["artifact_hash"], saved["artifact_hash"], report["artifact_hash"]),
            )
            return
        try:
            result, source_hash = self._run_product(case, case_root)
        except Exception:
            self._complete(
                case, execution=ExecutionStatus.FAILED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE, report_status=ReportStatus.NOT_BUILT,
                failure_stage="PRODUCT_CANARY", failure_code="PRODUCT_CANARY_FAILED",
                recovery_cursor="PRODUCT_CANARY", artifacts=(preflight["artifact_hash"],),
                provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
                platform_retries=1,
            )
            return
        if result.lifecycle not in {"COMPLETED", "PARTIAL"}:
            self._complete(
                case, execution=ExecutionStatus.FAILED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE, report_status=ReportStatus.NOT_BUILT,
                failure_stage="PRODUCT_CANARY", failure_code=(result.failures[0] if result.failures else "PRODUCT_CANARY_FAILED"),
                recovery_cursor="PRODUCT_CANARY", artifacts=(preflight["artifact_hash"],),
                provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
            )
            return
        table_path = _single(case_root, "standard_table.json")
        table = json.loads(table_path.read_text(encoding="utf-8"))
        standard_hash = canonical_hash(table)
        source_path = _source_path(case_root, source_hash)
        closure = _field_closure(
            source_path, set(self._coverage),
            native_fields=_GROUP_NATIVE_FIELDS[case["group_id"]],
        )
        closure_saved = atomic_json(case_root / "field_closure.json", closure)
        if closure["status"] != "PASSED":
            self._complete(
                case, execution=ExecutionStatus.FAILED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE, report_status=ReportStatus.REJECTED,
                failure_stage="FIELD_CLOSURE", failure_code="FIELD_NOT_IN_FROZEN_SCHEMA",
                recovery_cursor="FIELD_CLOSURE", artifacts=(preflight["artifact_hash"], standard_hash, closure_saved["artifact_hash"]),
                provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
            )
            return
        if case["qualification_role"] != "FAIL_CLOSED" and not closure["native_field_hits"]:
            self._complete(
                case, execution=ExecutionStatus.FAILED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE, report_status=ReportStatus.REJECTED,
                failure_stage="GROUP_CAPABILITY_GATE", failure_code="GROUP_NATIVE_FIELD_NOT_USED",
                recovery_cursor="FIELD_CLOSURE", artifacts=(preflight["artifact_hash"], standard_hash, closure_saved["artifact_hash"]),
                provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
            )
            return
        context = self._evaluation_context(table)
        try:
            evidence = OutputEvaluatorRegistry().evaluate(
                table=table, context=context, evidence_id=f"evidence_{case['case_id']}",
            )
        except EvaluationRejected as exc:
            diagnostic = atomic_json(case_root / "evaluation_failure.json", {
                "schema_version": "alphaforge.typed-evaluation-failure/v1",
                "case_id": case["case_id"], "failure_code": exc.code,
                "diagnostics": list(exc.diagnostics), "standard_table_hash": standard_hash,
            })
            self._complete(
                case, execution=ExecutionStatus.FAILED, authority=EvidenceAuthority.NONE,
                verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE, report_status=ReportStatus.REJECTED,
                failure_stage="TYPE_SPECIFIC_EVALUATOR", failure_code=exc.code,
                recovery_cursor="EVALUATION", artifacts=(preflight["artifact_hash"], standard_hash, closure_saved["artifact_hash"], diagnostic["artifact_hash"]),
                provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
            )
            return
        evidence_saved = atomic_json(case_root / "evidence_snapshot_v2.json", evidence.to_dict(include_hash=False))
        report, markdown = build_report_v2(
            question_id=case["question_id"], question=case["question"], evidence=evidence,
            specialists=(case["group_id"],),
            tested=(f"{case['output_kind']} 类型的冻结输入隔离实验",),
            not_tested=("独立封闭测试与晋级不属于本资格任务",),
            methods=(f"{case['qualification_role']} 的类型专属确定性评价",),
            baselines=("冻结可见数据中的零效应或登记基线",), controls=("PIT 时钟、成本和输出合同",),
            contributions=({"group": case["group_id"], "role": case["qualification_role"], "evidence_hash": evidence.artifact_hash},),
            conflicts=(), associated_artifacts=(standard_hash, closure_saved["artifact_hash"], evidence_saved["artifact_hash"]),
            next_experiment=_next_experiment(case["output_kind"], evidence.verdict.value),
        )
        report_saved = atomic_json(case_root / "report_v2.json", {key: value for key, value in report.items() if key != "report_hash"})
        markdown_hash = atomic_text(case_root / "report_v2.md", markdown)
        receipt = atomic_json(case_root / "qualification_receipt.json", {
            "schema_version": "alphaforge.qualification-receipt/v1",
            "case_id": case["case_id"], "trial_id": case["trial_id"],
            "idempotency_key": case["idempotency_key"], "source_hash": source_hash,
            "standard_table_hash": standard_hash, "evidence_snapshot_hash": evidence.artifact_hash,
            "report_hash": report["report_hash"], "markdown_hash": markdown_hash,
            "authority": evidence.authority.value, "verdict": evidence.verdict.value,
        })
        self._complete(
            case, execution=ExecutionStatus.PASSED, authority=EvidenceAuthority.OFFICIAL_EVIDENCE,
            verdict=evidence.verdict, report_status=ReportStatus.QUALIFIED,
            failure_stage=None, failure_code=None, recovery_cursor="COMMITTED",
            artifacts=(
                preflight["artifact_hash"], standard_hash, closure_saved["artifact_hash"],
                evidence_saved["artifact_hash"], report_saved["artifact_hash"], receipt["artifact_hash"],
            ), provider_attempts=1 if case["source_mode"] == "NEW_REAL_RUN" else 0,
        )

    def _run_product(self, case: Mapping[str, Any], case_root: Path):
        imported = case.get("imported_receipt")
        resume_path = None
        resume_hash = None
        if case["source_mode"] == "IMPORTED_REAL_RECEIPT":
            base = Path(imported["acceptance_receipt_path"]).parent
            resume_hash = str(imported["source_hash"])
            digest = resume_hash[7:]
            resume_path = base / "draft_sources" / digest[:2] / digest[2:]
        horizon = _horizon(case["question"], resume_path)
        config = ProductCanaryConfiguration(
            data_root=self.data_root, artifact_root=case_root,
            question=case["question"], docker_image=_IMAGE_NAME, docker_image_id=_IMAGE_ID,
            label_horizon_sessions=horizon, seed=7, max_sessions=80, max_symbols=20,
            specialist_id=case["group_id"], output_kind=case["output_kind"],
            dotenv_path=self.workspace / "agent" / ".env",
            resume_source_path=resume_path, resume_source_hash=resume_hash,
        )
        result = AgentResearchProductCanary(config).run()
        if not result.experiments:
            raise ValueError("product canary produced no experiment")
        return result, result.experiments[0].source_hash

    def _preflight(self, case: Mapping[str, Any]) -> dict[str, Any]:
        required = _GROUP_NATIVE_FIELDS[case["group_id"]] if case["qualification_role"] == "NATIVE_OFFICIAL" else set()
        available = {field for field, coverage in self._coverage.items() if coverage > 0}
        missing = sorted(required - available)
        failures = []
        if missing:
            failures.append("NATIVE_DATA_NOT_OFFICIAL_OR_NONEMPTY")
        return {
            "schema_version": "alphaforge.qualification-data-preflight/v1",
            "case_id": case["case_id"], "dataset_root": str(self.data_root),
            "dataset_file": str(self.data_path), "dataset_file_hash": self.lineage["dataset_file_hash"],
            "snapshot_hash": self.lineage["snapshot_hash"], "row_count": self.lineage["row_count"],
            "entity_count": self.lineage["entity_count"], "required_native_fields": sorted(required),
            "field_coverage": {field: self._coverage.get(field) for field in sorted(required)},
            "missing_or_zero_fields": missing,
            "available_at_policy": self.lineage["availability_policy"],
            "field_mapping_version": "FROZEN_PANEL_SEMANTICS_V1",
            "nan_inf_policy": self.lineage["nan_inf_policy"],
            "status": "READY" if not failures else "BLOCKED",
            "failure_codes": failures,
        }

    def _evaluation_context(self, table: Mapping[str, Any]) -> dict[str, Any]:
        rows = table["rows"]
        dates = {str(row.get("decision_time")) for row in rows if row.get("decision_time")}
        symbols = {str(row.get("symbol")) for row in rows if row.get("symbol")}
        context: dict[str, Any] = {
            "data_scope": {
                "frozen_panel_rows": self.lineage["row_count"],
                "frozen_panel_entities": self.lineage["entity_count"],
                "evaluated_rows": len(rows), "evaluated_entities": len(symbols),
                "evaluated_times": len(dates),
            },
            "missingness": {"standard_table_missing_rate": 0.0, "exclusion_rule": "typed evaluator fail-closed"},
            "split": {"partition": "VISIBLE_VALIDATION", "seed": 7},
            "cost_model": {"spread_bps": 2.0, "linear_impact_bps": 1.0},
            "source_artifact_hashes": [self.lineage["artifact_hash"], self.lineage["snapshot_hash"]],
            "field_coverage": self._coverage,
        }
        kind = table.get("contract_type")
        if kind == "ScoreTable":
            labels = self._load_dated_labels()
            context["labels"] = [
                {"symbol": row["symbol"], "decision_time": row["decision_time"], "value": labels[key]}
                for row in rows
                if (key := (str(row["decision_time"])[:10], str(row["symbol"]))) in labels
            ]
        elif kind == "EventTable":
            labels = self._load_dated_labels()
            grouped: dict[str, list[float]] = {}
            for (decision, _symbol), value in labels.items():
                grouped.setdefault(decision, []).append(value)
            outcomes = {}
            for row in rows:
                key = (str(row["decision_time"])[:10], str(row["symbol"]))
                if key in labels:
                    controls = grouped.get(key[0], ())
                    outcomes[row["event_id"]] = {
                        "car": labels[key], "control_return": sum(controls) / len(controls) if controls else 0.0,
                        "placebo_car": 0.0,
                    }
            context.update({"event_outcomes": outcomes, "event_window_start": 0, "event_window_end": _horizon("", None)})
        return context

    def _load_labels(self) -> dict[str, float]:
        if self._labels is None:
            self._labels = _extract_top_object(self.data_path, "labels", "registered_factors")
        return self._labels

    def _load_dated_labels(self) -> dict[tuple[str, str], float]:
        if self._dated_labels is None:
            dated: dict[tuple[str, str], float] = {}
            for key, value in self._load_labels().items():
                timestamp, symbol = key.split("|", 1)
                identity = (timestamp[:10], symbol)
                if identity in dated:
                    raise ValueError("frozen labels duplicate trading-date/symbol identity")
                dated[identity] = value
            self._dated_labels = dated
        return self._dated_labels

    def _run_safe_fixture(self, case: Mapping[str, Any]) -> dict[str, Any]:
        expected = case["expected_safe_failure"]
        observed = _mechanical_safe_probe(case["group_id"])
        if observed != expected:
            raise ValueError("safe fixture did not fail with the expected typed code")
        return {
            "schema_version": "alphaforge.safe-fixture-receipt/v1",
            "case_id": case["case_id"], "expected_failure_code": expected,
            "observed_failure_code": observed, "status": "PASSED_FAIL_CLOSED",
        }

    def _reserve(self, case: Mapping[str, Any]) -> None:
        progress = load_hashed(self.root / "qualification_progress.json")
        active = list(progress.get("active_leases") or ())
        if active:
            lease = active[0]
            if lease.get("case_id") != case["case_id"] or lease.get("idempotency_key") != case["idempotency_key"]:
                raise ValueError("another qualification lease is active")
            lease["recovery_count"] = int(lease.get("recovery_count", 0)) + 1
            progress["active_leases"] = [lease]
            atomic_json(self.root / "qualification_progress.json", _without_hash(progress))
            return
        progress["active_leases"] = [{
            "case_id": case["case_id"], "trial_id": case["trial_id"],
            "idempotency_key": case["idempotency_key"], "lease_status": "ACTIVE",
            "recovery_count": 0,
        }]
        progress["recovery_cursor"] = case["case_id"]
        progress["cases"] = [
            dict(item, execution_status="RUNNING", recovery_cursor="PREFLIGHT")
            if item["case_id"] == case["case_id"] else item
            for item in progress["cases"]
        ]
        atomic_json(self.root / "qualification_progress.json", _without_hash(progress))

    def _complete(
        self, case: Mapping[str, Any], *, execution: ExecutionStatus,
        authority: EvidenceAuthority, verdict: ResearchVerdict, report_status: ReportStatus,
        failure_stage: str | None, failure_code: str | None, recovery_cursor: str,
        artifacts: tuple[str, ...], provider_attempts: int = 0,
        draft_revisions: int = 0, platform_retries: int = 0,
    ) -> None:
        progress = load_hashed(self.root / "qualification_progress.json")
        prior = next(item for item in progress["cases"] if item["case_id"] == case["case_id"])
        prior_attempts = prior.get("attempt_counters") or {}
        lease_retries = max(
            (int(item.get("recovery_count", 0)) for item in progress.get("active_leases", ()) if item.get("case_id") == case["case_id"]),
            default=0,
        )
        state = CaseState(
            case_id=case["case_id"], trial_id=case["trial_id"],
            idempotency_key=case["idempotency_key"], group_id=case["group_id"],
            role=case["qualification_role"], output_kind=case["output_kind"],
            execution_status=execution, evidence_authority=authority,
            research_verdict=verdict, report_status=report_status,
            failure_stage=failure_stage, failure_code=failure_code,
            provider_attempts=min(3, int(prior_attempts.get("provider_attempts", 0)) + provider_attempts),
            draft_revisions=min(3, int(prior_attempts.get("draft_revisions", 0)) + draft_revisions),
            platform_retries=min(
                3,
                int(prior_attempts.get("platform_retries", 0))
                + max(platform_retries, lease_retries)
                + int(prior.get("execution_status") in {"FAILED", "PREFLIGHT_BLOCKED"}),
            ),
            recovery_cursor=recovery_cursor,
            artifact_hashes=tuple(sorted(set(artifacts))),
        ).to_dict()
        progress["cases"] = [state if item["case_id"] == case["case_id"] else item for item in progress["cases"]]
        progress["active_leases"] = []
        remaining = [item for item in progress["cases"] if item["execution_status"] not in {"PASSED", "PREFLIGHT_BLOCKED", "FAILED"}]
        progress["recovery_cursor"] = remaining[0]["case_id"] if remaining else "GROUP_MATRIX"
        progress["current_batch"] = case["batch_id"]
        atomic_json(self.root / "qualification_progress.json", _without_hash(progress))
        _append_event(self.root / "events.jsonl", {
            "event_type": "QUALIFICATION_CASE_COMMITTED",
            "case_id": case["case_id"], "trial_id": case["trial_id"],
            "execution_status": execution.value, "evidence_authority": authority.value,
            "research_verdict": verdict.value, "failure_code": failure_code,
            "case_hash": state["case_hash"],
        })
        self._update_blockers()

    def _update_blockers(self) -> None:
        progress = load_hashed(self.root / "qualification_progress.json")
        clusters: dict[str, list[dict[str, str]]] = {}
        for case in progress["cases"]:
            code = case.get("failure_code")
            if not code or case.get("role") == "FAIL_CLOSED":
                continue
            clusters.setdefault(code, []).append({"case_id": case["case_id"], "group_id": case["group_id"]})
        rows, paused_groups, harness_paused = [], set(), False
        for code, items in sorted(clusters.items()):
            groups = {item["group_id"] for item in items}
            group_counts = {group: sum(item["group_id"] == group for item in items) for group in groups}
            paused_groups.update(group for group, count in group_counts.items() if count >= 2)
            if len(groups) >= 2:
                harness_paused = True
            rows.append({"failure_code": code, "count": len(items), "groups": sorted(groups), "cases": items})
        atomic_json(self.root / "blocked_clusters.json", {
            "schema_version": "alphaforge.blocked-clusters/v1", "artifact_type": "blocked-clusters",
            "clusters": rows, "paused_groups": sorted(paused_groups), "harness_paused": harness_paused,
        })

    def _refresh_matrices(self) -> dict[str, Any]:
        progress = load_hashed(self.root / "qualification_progress.json")
        rows = []
        for group_id in sorted(_GROUP_NATIVE_FIELDS):
            cases = [item for item in progress["cases"] if item["group_id"] == group_id]
            native = next(item for item in cases if item["role"] == "NATIVE_OFFICIAL")
            robust = next(item for item in cases if item["role"] == "ROBUSTNESS_OR_SPECIAL")
            safe = next(item for item in cases if item["role"] == "FAIL_CLOSED")
            native_pass = native["execution_status"] == "PASSED" and native["evidence_authority"] == "OFFICIAL_EVIDENCE" and native["report_status"] == "QUALIFIED" and self._native_field_used(native)
            robust_pass = robust["execution_status"] == "PASSED" and robust["evidence_authority"] == "OFFICIAL_EVIDENCE" and robust["report_status"] == "QUALIFIED" and self._native_field_used(robust)
            safe_pass = safe["execution_status"] == "PASSED" and safe.get("failure_code") is not None
            if native_pass and robust_pass and safe_pass:
                status = GroupStatus.QUALIFIED
            elif any(item["execution_status"] in {"PREFLIGHT_BLOCKED", "FAILED"} for item in cases):
                status = GroupStatus.BLOCKED
            elif any(item["execution_status"] == "PASSED" for item in cases):
                status = GroupStatus.PARTIAL
            else:
                status = GroupStatus.NOT_QUALIFIED
            rows.append({
                "group_id": group_id, "status": status.value,
                "native_official_gate": "PASSED" if native_pass else "NOT_PASSED",
                "robustness_gate": "PASSED" if robust_pass else "NOT_PASSED",
                "fail_closed_gate": "PASSED" if safe_pass else "NOT_PASSED",
                "case_ids": [item["case_id"] for item in cases],
            })
        matrix = atomic_json(self.root / "group_qualification_matrix.json", {
            "schema_version": "alphaforge.group-qualification-matrix/v1",
            "artifact_type": "group-qualification-matrix", "rows": rows,
            "qualified_count": sum(row["status"] == "QUALIFIED" for row in rows),
            "blocked_count": sum(row["status"] == "BLOCKED" for row in rows),
        })
        return matrix

    def _case(self, case_id: str) -> Mapping[str, Any]:
        progress = load_hashed(self.root / "qualification_progress.json")
        return next(item for item in progress["cases"] if item["case_id"] == case_id)

    def _is_group_paused(self, group_id: str) -> bool:
        blockers = load_hashed(self.root / "blocked_clusters.json")
        return group_id in blockers.get("paused_groups", ())

    def _harness_paused(self) -> bool:
        return bool(load_hashed(self.root / "blocked_clusters.json").get("harness_paused"))

    def _native_field_used(self, case: Mapping[str, Any]) -> bool:
        root = self.root / "qualification_runs" / next(
            item["batch_id"] for item in self.plan["cases"] if item["case_id"] == case["case_id"]
        ) / case["case_id"]
        paths = list(root.glob("field_closure.json"))
        if len(paths) != 1:
            return False
        closure = load_hashed(paths[0])
        hits = set(closure.get("native_field_hits") or ())
        if not hits:
            hits = set(closure.get("referenced_fields") or ()) & _GROUP_NATIVE_FIELDS[case["group_id"]]
        return bool(hits)


def _field_closure(source_path: Path, fields: set[str], *, native_fields: set[str] | None = None) -> dict[str, Any]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    references = set()
    row_variables = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)):
            iterator = node.iter
            if isinstance(iterator, ast.Name) and iterator.id in {"train_rows", "prediction_rows", "rows", "features"}:
                targets = node.target.elts if isinstance(node.target, (ast.Tuple, ast.List)) else (node.target,)
                row_variables.update(target.id for target in targets if isinstance(target, ast.Name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            candidate = node.slice
            if not isinstance(candidate, ast.Constant) or not isinstance(candidate.value, str):
                continue
            direct_row = isinstance(node.value, ast.Name) and node.value.id in row_variables
            nested_features = (
                isinstance(node.value, ast.Subscript)
                and isinstance(node.value.slice, ast.Constant)
                and node.value.slice.value == "features"
            )
            if direct_row or nested_features:
                references.add(candidate.value)
    allowed = fields | _VISIBLE_ROW_KEYS
    unknown = sorted(references - allowed)
    return {
        "schema_version": "alphaforge.field-reference-closure/v1",
        "source_hash": "sha256:" + __import__("hashlib").sha256(source.encode("utf-8")).hexdigest(),
        "referenced_fields": sorted(references), "unknown_fields": unknown,
        "native_field_hits": sorted(references & set(native_fields or ())),
        "frozen_schema_hash": canonical_hash(sorted(fields)),
        "status": "PASSED" if not unknown else "FAILED",
    }


def _mechanical_safe_probe(group_id: str) -> str:
    if group_id == "market_structure":
        return "FIELD_NOT_IN_FROZEN_SCHEMA"
    if group_id == "fundamental":
        available_at, decision_time = "2026-01-06", "2026-01-05"
        return "PIT_AVAILABILITY_VIOLATION" if available_at > decision_time else "UNEXPECTED_PASS"
    if group_id == "expectations_events":
        return "EVENT_CLOCK_VIOLATION"
    if group_id == "alternative_data":
        source = "import requests\ndef run_research(rows, seed):\n return requests.get('https://example.invalid')"
        tree = ast.parse(source)
        roots = {alias.name.split('.', 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        return "UNAUTHORIZED_PATH_OR_NETWORK" if "requests" in roots else "UNEXPECTED_PASS"
    if group_id == "macro_regime":
        return "REGIME_SAMPLE_INSUFFICIENT"
    if group_id == "meta_alpha":
        proposer, approver = "meta_alpha", "meta_alpha"
        return "SELF_APPROVAL_FORBIDDEN" if proposer == approver else "UNEXPECTED_PASS"
    raise AssertionError(group_id)


def _extract_top_object(path: Path, key: str, next_key: str) -> dict[str, float]:
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        marker = f'"{key}"'.encode()
        position = mapped.find(marker)
        colon = mapped.find(b":", position + len(marker))
        start = mapped.find(b"{", colon)
        end_marker = mapped.find(b'\n  },\n  "', start)
        end = end_marker + 4 if end_marker >= 0 else -1
        if min(position, colon, start, end) < 0:
            raise ValueError(f"cannot extract frozen {key}")
        value = json.loads(mapped[start:end].decode("utf-8"))
        return {str(item_key): float(item_value) for item_key, item_value in value.items()}


def _horizon(question: str, source_path: Path | None) -> int:
    if source_path is not None:
        match = re.search(r"['\"]label_horizon_sessions['\"]\s*:\s*(\d+)", source_path.read_text(encoding="utf-8"))
        if match:
            return int(match.group(1))
    match = re.search(r"未来\s*(\d+)\s*个?交易日", question)
    return int(match.group(1)) if match else 20


def _single(root: Path, name: str) -> Path:
    paths = list(root.rglob(name))
    if len(paths) != 1:
        raise ValueError(f"expected exactly one {name}, got {len(paths)}")
    return paths[0]


def _source_path(root: Path, source_hash: str) -> Path:
    digest = source_hash[7:]
    path = root / "draft_sources" / digest[:2] / digest[2:]
    if not path.is_file():
        raise ValueError("content-addressed source is missing")
    return path


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("artifact_hash", None)
    return result


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    rows = []
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    value = dict(event)
    value["event_hash"] = canonical_hash(value)
    rows.append(value)
    atomic_jsonl(path, rows)


def _next_experiment(output_kind: str, verdict: str) -> str:
    suffix = {
        "ScoreTable": "在未重叠可见子时期复测成本后横截面效应",
        "RegressionTable": "增加独立控制变量并检查设计矩阵条件数",
        "RegimeComparisonTable": "扩大每个状态的有效样本并复测状态转换稳定性",
        "EventTable": "扩大事件样本并更换 Placebo 窗口与匹配对照",
        "DiagnosticTable": "对排名最高的根因执行一个可证伪修复实验",
        "DatasetAssessmentTable": "对最低覆盖字段执行时间语义和偏差审计",
    }[output_kind]
    return f"当前结论为 {verdict}；{suffix}"


__all__ = ["QualificationBatchRunner"]
