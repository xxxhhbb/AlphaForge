"""Recoverable, content-addressed qualification for formula semantics."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash
from ..formula_semantics import FormulaSemanticReviewer


def _source(fields: tuple[str, ...], expression: str) -> str:
    return (
        "def run_research(train_rows, prediction_rows, seed):\n"
        f"    feature_fields = {list(fields)!r}\n"
        "    z_scores = [1.0 for _ in feature_fields]\n"
        f"    score = {expression}\n"
        "    return {'rows': []}\n"
    )


class FormulaQualityHarness:
    def __init__(self, *, output_root: str | Path) -> None:
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.reviewer = FormulaSemanticReviewer()

    def run(self) -> dict[str, Any]:
        cases = self._cases()
        plan = {
            "schema_version": "alphaforge.formula-quality-plan/v1",
            "cases": [{key: value for key, value in case.items() if key != "rows"} for case in cases],
        }
        plan["artifact_hash"] = canonical_hash(plan)
        self._atomic_json("qualification_plan.json", plan)
        results = []
        for case in cases:
            source = _source(tuple(case["fields"]), case["expression"])
            result = self.reviewer.review(
                source_text=source,
                output_kind="ScoreTable",
                feature_rows=case["rows"],
                data_snapshot_hash="sha256:" + "d" * 64,
            )
            codes = tuple(item.code for item in result.findings)
            passed = (
                result.requires_revision is case["expect_revision"]
                and set(codes) == set(case["expected_codes"])
            )
            receipt = {
                "schema_version": "alphaforge.formula-quality-case/v1",
                "case_id": case["case_id"],
                "execution_status": "PASSED" if passed else "FAILED",
                "expect_revision": case["expect_revision"],
                "actual_revision": result.requires_revision,
                "expected_codes": list(case["expected_codes"]),
                "actual_codes": list(codes),
                "formula_review_hash": result.artifact_hash,
                "source_hash": result.source_hash,
            }
            receipt["artifact_hash"] = canonical_hash(receipt)
            self._atomic_json(f"cases/{case['case_id']}.json", receipt)
            self._append_event({
                "event": "FORMULA_CASE_COMMITTED",
                "case_id": case["case_id"],
                "receipt_hash": receipt["artifact_hash"],
                "status": receipt["execution_status"],
            })
            results.append(receipt)
        failed = tuple(item["case_id"] for item in results if item["execution_status"] != "PASSED")
        progress = {
            "schema_version": "alphaforge.formula-quality-progress/v1",
            "status": "PASSED" if not failed else "FAILED",
            "completed": len(results),
            "total": len(cases),
            "failed_cases": list(failed),
            "recovery_cursor": None if not failed else failed[0],
        }
        progress["artifact_hash"] = canonical_hash(progress)
        self._atomic_json("progress.json", progress)
        verdict = {
            "schema_version": "alphaforge.formula-quality-verdict/v1",
            "status": "QUALIFIED" if not failed else "FAILED",
            "scope": "FORMULA_SEMANTICS_AND_REVISION_GATE",
            "case_count": len(results),
            "passed_count": len(results) - len(failed),
            "failed_cases": list(failed),
            "plan_hash": plan["artifact_hash"],
            "progress_hash": progress["artifact_hash"],
        }
        verdict["artifact_hash"] = canonical_hash(verdict)
        self._atomic_json("final_verdict.json", verdict)
        return verdict

    @staticmethod
    def _cases() -> tuple[dict[str, Any], ...]:
        def rows(**columns):
            return tuple({
                "decision_time": f"2026-01-{1 + index // 3:02d}",
                **{name: values[index] for name, values in columns.items()},
            } for index in range(len(next(iter(columns.values())))))

        return (
            {
                "case_id": "FQ-01_RENAMED_CONSTANT_ADDITIVE",
                "fields": ("random_feature", "turnover_rate"),
                "expression": "z_scores[0] + z_scores[1]",
                "rows": rows(random_feature=[1, 1, 1, 2, 2, 2], turnover_rate=[1, 2, 3, 2, 4, 8]),
                "expect_revision": True,
                "expected_codes": ("CROSS_SECTION_CONSTANT_ADDITIVE_TERM",),
            },
            {
                "case_id": "FQ-02_NAME_FALSE_POSITIVE",
                "fields": ("shibor_1w", "turnover_rate"),
                "expression": "z_scores[0] + z_scores[1]",
                "rows": rows(shibor_1w=[1, 2, 3, 2, 3, 4], turnover_rate=[3, 1, 2, 4, 2, 1]),
                "expect_revision": False,
                "expected_codes": (),
            },
            {
                "case_id": "FQ-03_MACRO_INTERACTION",
                "fields": ("macro_state", "turnover_rate"),
                "expression": "z_scores[0] * z_scores[1]",
                "rows": rows(macro_state=[1, 1, 1, 2, 2, 2], turnover_rate=[1, 2, 3, 2, 4, 8]),
                "expect_revision": False,
                "expected_codes": (),
            },
            {
                "case_id": "FQ-04_HIGH_CORRELATION_ADVISORY",
                "fields": ("value_a", "value_b"),
                "expression": "(z_scores[0] + z_scores[1]) / 2.0",
                "rows": rows(value_a=[1, 2, 3, 4, 5, 6], value_b=[2, 4, 6, 8, 10, 12]),
                "expect_revision": False,
                "expected_codes": ("HIGH_FEATURE_CORRELATION",),
            },
            {
                "case_id": "FQ-05_SUM_FORM",
                "fields": ("constant_state", "turnover_rate"),
                "expression": "sum(z_scores) / len(z_scores)",
                "rows": rows(constant_state=[1, 1, 1, 2, 2, 2], turnover_rate=[1, 2, 3, 2, 4, 8]),
                "expect_revision": True,
                "expected_codes": ("CROSS_SECTION_CONSTANT_ADDITIVE_TERM",),
            },
        )

    def _atomic_json(self, relative: str, value: Any) -> None:
        target = (self.output_root / relative).resolve()
        target.relative_to(self.output_root)
        target.parent.mkdir(parents=True, exist_ok=True)
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
        path = self.output_root / "events.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    output_root = repo_root / ".autobuild" / "veriquant" / "formula_quality_harness"
    verdict = FormulaQualityHarness(output_root=output_root).run()
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["status"] == "QUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FormulaQualityHarness", "main"]
