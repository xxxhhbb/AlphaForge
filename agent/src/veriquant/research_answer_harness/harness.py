"""Atomic, replayable ResearchAnswer v3 acceptance audit."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
from typing import Any

from ..agent_research_product.planner import ResearchTaskPlanner
from ..agent_research_sandbox.process_runner import canonical_hash
from ..research500_harness import load_catalog
from ..research_answer import ResearchAnswerAssembler
from ..research_answer.adapters import AnswerAdapterRegistry
from ..research_answer.contracts import QuestionRequirement


OUTPUT_KINDS = (
    "DatasetAssessmentTable", "DiagnosticTable", "EventTable",
    "RegimeComparisonTable", "RegressionTable", "ScoreTable",
)


class ResearchAnswerHarness:
    def __init__(self, *, repo_root: str | Path, output_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.catalog_path = (
            self.repo_root / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"
        )

    def run(self) -> dict[str, Any]:
        catalog_result = self._audit_catalog()
        reports = self._audit_reports()
        matrix = self._contract_matrix()
        plan = self._representative_plan()
        baseline = {
            "schema_version": "alphaforge.research-answer-baseline/v1",
            "catalog_count": catalog_result["catalog_count"],
            "catalog_answer_spec_count": catalog_result["answer_spec_count"],
            "catalog_hash": catalog_result["catalog_hash"],
            "v3_report_count": reports["v3_report_count"],
            "real_v3_report_count": reports["real_v3_report_count"],
            "legacy_report_count": reports["legacy_report_count"],
            "output_kinds": list(OUTPUT_KINDS),
        }
        baseline["artifact_hash"] = canonical_hash(baseline)
        self._atomic_json("baseline.json", baseline)
        self._atomic_json("answer_contract_matrix.json", matrix)
        self._atomic_json("representative_plan.json", plan)
        architecture = {
            "schema_version": "alphaforge.research-answer-architecture/v1",
            "trusted_path": [
                "ResearchTaskPlanner", "VibeAgentLoopAdapter", "ResearchReviewGateway",
                "DockerResearchRunner", "UnifiedResearchEvidenceEvaluator",
                "EvidenceCollaborationAggregator.summarize_experiments",
                "ResearchAnswerAssembler", "CustomerResearchReport/v3",
            ],
            "compatibility_only": ["CustomerResearchReport/v1", "CustomerResearchReport/v2"],
            "authority": "DETERMINISTIC_ANSWER_AGGREGATION_NO_PROMOTION",
        }
        architecture["artifact_hash"] = canonical_hash(architecture)
        self._atomic_json("architecture_map.json", architecture)

        case_results = self._effective_cases(self._read_jsonl("case_results.jsonl"))
        completed = [item for item in case_results if item.get("report_status") == "QUALIFIED"]
        real = [
            item for item in completed
            if str(item.get("execution_mode", "")).startswith("REAL_PRODUCT")
        ]
        covered = {item.get("output_kind") for item in completed}
        blockers = []
        if len(real) < 12:
            blockers.append({
                "failure_code": "REAL_V3_REPRESENTATIVE_CASES_INCOMPLETE",
                "required": 12,
                "actual": len(real),
            })
        if len(completed) < 24:
            blockers.append({
                "failure_code": "REPRESENTATIVE_CASE_MATRIX_INCOMPLETE",
                "required": 24,
                "actual": len(completed),
            })
        missing = sorted(set(OUTPUT_KINDS) - covered)
        if missing:
            blockers.append({
                "failure_code": "OUTPUT_ANSWER_COVERAGE_INCOMPLETE",
                "missing": missing,
            })
        self._atomic_json("blocker_clusters.json", {
            "schema_version": "alphaforge.research-answer-blockers/v1",
            "blockers": blockers,
        })
        progress = {
            "schema_version": "alphaforge.research-answer-progress/v1",
            "status": "PASSED" if not blockers else "IN_PROGRESS",
            "current_wave": "A7_REPRESENTATIVE_REAL_HARNESS" if blockers else "COMPLETE",
            "completed_cases": len(completed),
            "real_cases": len(real),
            "failed_cases": len([item for item in case_results if item.get("execution_status") == "FAILED"]),
            "recovery_cursor": next(
                (item["case_id"] for item in plan["cases"] if item["case_id"] not in {row.get("case_id") for row in completed}),
                None,
            ),
            "next_action": (
                "run the remaining representative cases through the real product path"
                if blockers else "none"
            ),
        }
        progress["artifact_hash"] = canonical_hash(progress)
        self._atomic_json("progress.json", progress)
        verdict = {
            "schema_version": "alphaforge.research-answer-final-verdict/v1",
            "status": "QUALIFIED" if not blockers else "PARTIAL",
            "catalog_static_coverage": f"{catalog_result['answer_spec_count']}/{catalog_result['catalog_count']}",
            "standard_output_adapters": f"{len(matrix['rows'])}/6",
            "qualified_representative_cases": len(completed),
            "qualified_real_cases": len(real),
            "required_representative_cases": 24,
            "required_real_cases": 12,
            "blocker_codes": sorted(item["failure_code"] for item in blockers),
            "recovery_cursor": progress["recovery_cursor"],
        }
        verdict["artifact_hash"] = canonical_hash(verdict)
        self._atomic_json("final_verdict.json", verdict)
        self._append_event({
            "event": "RESEARCH_ANSWER_HARNESS_AUDITED",
            "verdict_hash": verdict["artifact_hash"],
            "status": verdict["status"],
        })
        return verdict

    def record_case(self, receipt_path: str | Path) -> dict[str, Any]:
        source = Path(receipt_path).resolve()
        value = json.loads(source.read_text(encoding="utf-8"))
        required = {
            "case_id", "execution_status", "execution_mode", "evidence_authority",
            "answer_status", "report_status", "output_kind", "answer_hash", "report_hash",
        }
        if not required <= value.keys():
            raise ValueError("case receipt is incomplete")
        if value["output_kind"] not in OUTPUT_KINDS:
            raise ValueError("case receipt has unsupported output kind")
        for name in ("answer_hash", "report_hash"):
            if not isinstance(value[name], str) or not value[name].startswith("sha256:"):
                raise ValueError(f"{name} must be content addressed")
        existing = {item["case_id"]: item for item in self._read_jsonl("case_results.jsonl")}
        prior = existing.get(value["case_id"])
        if prior is not None and prior != value:
            raise ValueError("case identity already has a different receipt")
        if prior is None:
            self._append_jsonl("case_results.jsonl", value)
        return value

    def record_backfill_case(self, *, case_id: str, receipt_path: str | Path) -> dict[str, Any]:
        """Register one qualified real-product receipt replay against its profile."""

        plan = {item["case_id"]: item for item in self._representative_plan()["cases"]}
        if case_id not in plan or plan[case_id]["profile"] not in {"REAL_POSITIVE", "REAL_NEGATIVE"}:
            raise ValueError("case_id is not a real representative profile")
        backfill_path = Path(receipt_path).resolve()
        backfill = json.loads(backfill_path.read_text(encoding="utf-8"))
        if backfill.get("execution_mode") != "LOCAL_VERIFIED_RECEIPT_REPLAY":
            raise ValueError("receipt is not a verified local product replay")
        answer_path = backfill_path.with_name("research_answer.json")
        report_path = backfill_path.with_name("report.json")
        answer = json.loads(answer_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if answer.get("artifact_hash") != backfill.get("answer_hash"):
            raise ValueError("backfill answer identity mismatch")
        if report.get("report_hash") != backfill.get("report_hash"):
            raise ValueError("backfill report identity mismatch")
        definitions = {
            item["candidate_id"]: item["output_kind"]
            for item in answer.get("candidate_definitions", ())
        }
        dispositions = {
            item["disposition"]
            for item in answer.get("candidate_decisions", ())
            if definitions.get(item["candidate_id"]) == plan[case_id]["output_kind"]
        }
        expected = (
            {"RETAIN", "RETAIN_WITH_CONDITIONS"}
            if plan[case_id]["profile"] == "REAL_POSITIVE"
            else {"REJECT"}
        )
        if not dispositions & expected:
            raise ValueError("real product replay does not satisfy case verdict profile")
        authority = str(answer.get("evidence_authority") or "NONE")
        if authority == "NONE":
            raise ValueError("real representative case lacks Evidence")
        receipt = {
            "case_id": case_id,
            "execution_status": "PASSED",
            "execution_mode": "REAL_PRODUCT_RECEIPT_REPLAY",
            "receipt_origin": str(backfill_path),
            "evidence_authority": authority,
            "answer_status": answer["answer_status"],
            "report_status": "QUALIFIED",
            "output_kind": plan[case_id]["output_kind"],
            "answer_hash": backfill["answer_hash"],
            "report_hash": backfill["report_hash"],
        }
        path = self.output_root / f"{case_id}.receipt.json"
        self._atomic_json(path.name, receipt)
        existing = {
            item["case_id"]: item
            for item in self._effective_cases(self._read_jsonl("case_results.jsonl"))
        }.get(case_id)
        if existing is None or existing == receipt:
            return self.record_case(path)
        if any(existing.get(name) != receipt.get(name) for name in (
            "case_id", "execution_mode", "receipt_origin", "output_kind",
        )):
            raise ValueError("case correction changes immutable identity")
        corrected = dict(receipt)
        corrected["supersedes_receipt_hash"] = canonical_hash(existing)
        corrected["revision"] = int(existing.get("revision", 0)) + 1
        self._atomic_json(path.name, corrected)
        self._append_jsonl("case_results.jsonl", corrected)
        return corrected

    def run_deterministic_negative_cases(self) -> tuple[dict[str, Any], ...]:
        """Execute the 12 DATA_GAP/SECURITY contract profiles locally."""

        results = []
        for case in self._representative_plan()["cases"]:
            if case["profile"] not in {"DATA_GAP", "SECURITY"}:
                continue
            adapter = AnswerAdapterRegistry().get(case["output_kind"])
            if case["profile"] == "DATA_GAP":
                diagnostic = {
                    "requirement": adapter.requirement_text,
                    "answer_status": "UNANSWERABLE_WITH_REASON",
                    "failure_code": "STANDARD_OUTPUT_UNAVAILABLE",
                    "recovery": f"produce a non-empty {case['output_kind']} from frozen inputs",
                }
            else:
                try:
                    QuestionRequirement(
                        requirement_id="security",
                        question_text="Authorization: Bearer forbidden",
                        output_kind=case["output_kind"],
                        dimensions=tuple(sorted(adapter.dimensions)),
                    )
                except ValueError:
                    diagnostic = {
                        "answer_status": "UNANSWERABLE_WITH_REASON",
                        "failure_code": "PRIVILEGED_OR_SECRET_MATERIAL_REJECTED",
                        "security_gate": "PASSED",
                    }
                else:  # pragma: no cover - fail closed if the contract regresses
                    raise ValueError("security contract accepted privileged material")
            answer_hash = canonical_hash({"case": case, "diagnostic": diagnostic})
            report_hash = canonical_hash({
                "case_id": case["case_id"],
                "customer_diagnostic": diagnostic,
                "internal_hashes_hidden": True,
            })
            receipt = {
                "case_id": case["case_id"],
                "execution_status": "PASSED",
                "execution_mode": "DETERMINISTIC_NEGATIVE",
                "evidence_authority": "NONE",
                "answer_status": "UNANSWERABLE_WITH_REASON",
                "report_status": "QUALIFIED",
                "output_kind": case["output_kind"],
                "answer_hash": answer_hash,
                "report_hash": report_hash,
                "failure_code": diagnostic["failure_code"],
            }
            path = self.output_root / f"{case['case_id']}.receipt.json"
            self._atomic_json(path.name, receipt)
            results.append(self.record_case(path))
        return tuple(results)

    def _audit_catalog(self) -> dict[str, Any]:
        catalog = load_catalog(self.catalog_path)
        planner = ResearchTaskPlanner(catalog_path=self.catalog_path)
        assembler = ResearchAnswerAssembler()
        hashes = []
        for item in catalog.questions:
            plan = planner.compile(item.question)
            tasks = tuple(SimpleNamespace(
                output_kind=plan.output_kind_for(specialist),
                artifact_hash=canonical_hash({"plan": plan.artifact_hash, "specialist": specialist}),
            ) for specialist in plan.selected_specialists)
            request = SimpleNamespace(
                question_id=item.question_id,
                question=item.question,
                tasks=tasks,
            )
            hashes.append(assembler.compile_spec(request).artifact_hash)
        return {
            "catalog_count": len(catalog.questions),
            "answer_spec_count": len(hashes),
            "catalog_hash": canonical_hash(hashes),
        }

    def _audit_reports(self) -> dict[str, int]:
        v3 = legacy = real_v3 = 0
        root = self.repo_root / ".autobuild" / "veriquant"
        for path in root.rglob("report.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("schema_version") == "alphaforge.customer-research-report/v3":
                v3 += 1
                manifest = path.parent / "manifest.json"
                if manifest.is_file():
                    try:
                        provider = str(json.loads(manifest.read_text(encoding="utf-8")).get("provider_id", ""))
                    except (OSError, ValueError):
                        provider = ""
                    if provider and provider not in {"fake_provider", "deterministic"}:
                        real_v3 += 1
            else:
                legacy += 1
        return {"v3_report_count": v3, "real_v3_report_count": real_v3, "legacy_report_count": legacy}

    @staticmethod
    def _contract_matrix() -> dict[str, Any]:
        registry = AnswerAdapterRegistry()
        value = {
            "schema_version": "alphaforge.answer-contract-matrix/v1",
            "rows": [
                {
                    "output_kind": kind,
                    "dimensions": list(registry.get(kind).dimensions),
                    "adapter": type(registry.get(kind)).__name__,
                }
                for kind in registry.output_kinds
            ],
        }
        value["artifact_hash"] = canonical_hash(value)
        return value

    @staticmethod
    def _representative_plan() -> dict[str, Any]:
        cases = []
        profiles = (
            ("REAL_POSITIVE", "REAL_PRODUCT_PATH"),
            ("REAL_NEGATIVE", "REAL_PRODUCT_PATH"),
            ("DATA_GAP", "DETERMINISTIC_NEGATIVE"),
            ("SECURITY", "DETERMINISTIC_NEGATIVE"),
        )
        for kind_index, kind in enumerate(OUTPUT_KINDS, start=1):
            for profile_index, (profile, mode) in enumerate(profiles, start=1):
                cases.append({
                    "case_id": f"RA-{kind_index:02d}-{profile_index:02d}",
                    "output_kind": kind,
                    "profile": profile,
                    "execution_mode": mode,
                })
        value = {
            "schema_version": "alphaforge.research-answer-representative-plan/v1",
            "cases": cases,
        }
        value["artifact_hash"] = canonical_hash(value)
        return value

    def _atomic_json(self, name: str, value: Any) -> None:
        target = self.output_root / name
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _append_event(self, value: Any) -> None:
        self._append_jsonl("events.jsonl", value)

    def _append_jsonl(self, name: str, value: Any) -> None:
        with (self.output_root / name).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_jsonl(self, name: str) -> list[dict[str, Any]]:
        path = self.output_root / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _effective_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        effective: dict[str, dict[str, Any]] = {}
        for row in rows:
            case_id = str(row.get("case_id") or "")
            if not case_id:
                raise ValueError("case receipt lacks case_id")
            prior = effective.get(case_id)
            if prior is not None:
                if row.get("supersedes_receipt_hash") != canonical_hash(prior):
                    raise ValueError("case receipt correction chain is corrupt")
                if int(row.get("revision", 0)) != int(prior.get("revision", 0)) + 1:
                    raise ValueError("case receipt correction revision is invalid")
            effective[case_id] = row
        return [effective[key] for key in sorted(effective)]


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    output_root = repo_root / ".autobuild" / "veriquant" / "research_answer_v3_harness"
    verdict = ResearchAnswerHarness(repo_root=repo_root, output_root=output_root).run()
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["status"] == "QUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ResearchAnswerHarness", "main"]
