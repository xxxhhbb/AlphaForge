"""CLI for reproducible, read-only product acceptance assembly."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .assembler import assemble_matrix_evidence
from .evaluator import ProductAcceptanceEvaluator, file_sha256


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--shard", type=Path, action="append", required=True)
    parser.add_argument("--test-receipt", type=Path, required=True)
    parser.add_argument("--semantic-failure", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    receipt_hash = file_sha256(args.test_receipt)
    evidence = assemble_matrix_evidence(
        shard_roots=args.shard,
        test_evidence_hashes=(receipt_hash,),
        semantic_failures=frozenset(args.semantic_failure),
    )
    report = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(args.matrix))
    evidence_path = args.output / "scenario_evidence.json"
    report_path = args.output / "acceptance_report.json"
    summary_path = args.output / "acceptance_summary.md"
    _atomic_write(
        evidence_path,
        json.dumps([item.to_dict() for item in evidence], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    _atomic_write(report_path, report.canonical_json() + "\n")
    rows = [
        "# AlphaForge 24 场景机械验收",
        "",
        f"- 总状态：`{report.status.value}`",
        f"- 通过：`{report.passed_count}/{report.scenario_count}`",
        f"- 真实冻结/登记证据覆盖：`{report.real_or_registered_count}`",
        f"- 报告哈希：`{report.artifact_hash}`",
        "",
        "| 场景 | 终态 | 验收 | 失败码 |",
        "|---|---|---|---|",
    ]
    for verdict in report.verdicts:
        failures = ", ".join(item.value for item in verdict.failure_codes) or "—"
        rows.append(f"| {verdict.scenario_id} | {verdict.terminal.value} | {verdict.status.value} | {failures} |")
    if report.global_failure_codes:
        rows.extend(("", "全局失败码：" + ", ".join(item.value for item in report.global_failure_codes)))
    _atomic_write(summary_path, "\n".join(rows) + "\n")
    print(report.canonical_json())
    return 0 if report.status.value == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
