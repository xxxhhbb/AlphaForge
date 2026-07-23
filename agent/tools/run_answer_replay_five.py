"""Offline replay of five completed product receipts through Answer v2."""

from __future__ import annotations

import json
from pathlib import Path

from veriquant.research_answer_harness import LegacyProductRunBackfiller


def main() -> int:
    root = Path(__file__).resolve().parents[2] / ".autobuild" / "veriquant"
    selected: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for product_result in sorted(root.rglob("product_result.json")):
        report_path = product_result.with_name("report.json")
        if not report_path.is_file():
            continue
        try:
            question = json.loads(report_path.read_text(encoding="utf-8")).get("question")
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(question, str) or not question or question in seen:
            continue
        seen.add(question)
        selected.append((question, product_result))
        if len(selected) == 5:
            break

    backfiller = LegacyProductRunBackfiller()
    for index, (question, product_result) in enumerate(selected, start=1):
        receipt = backfiller.upgrade(product_result)
        report_path = product_result.parent / "research_answer_v3_backfill" / "report.md"
        print(f"CASE {index}")
        print(f"QUESTION: {question}")
        print(f"ANSWER_STATUS: {receipt['answer_status']}")
        print(f"RESEARCH_VERDICT: {receipt['research_verdict']}")
        print(f"REPORT: {report_path}")
    print(f"TOTAL: {len(selected)}")
    return 0 if len(selected) == 5 else 2


if __name__ == "__main__":
    raise SystemExit(main())
