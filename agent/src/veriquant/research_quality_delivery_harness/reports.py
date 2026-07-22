"""Customer-facing Research Report v2 with Evidence-bound numbers."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .contracts import EvidenceSnapshotV2
from .storage import canonical_hash, normalize_json


REPORT_SCHEMA = "alphaforge.research-report/v2"
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def build_report_v2(
    *, question_id: str, question: str, evidence: EvidenceSnapshotV2,
    specialists: Sequence[str], tested: Sequence[str], not_tested: Sequence[str],
    methods: Sequence[str], baselines: Sequence[str], controls: Sequence[str],
    contributions: Sequence[Mapping[str, Any]], conflicts: Sequence[Mapping[str, Any]],
    associated_artifacts: Sequence[str], next_experiment: str,
) -> tuple[dict[str, Any], str]:
    evidence_hash = evidence.artifact_hash
    metrics = [
        {
            "name": name,
            "value": value,
            "evidence_hash": evidence.metric_hashes[name],
        }
        for name, value in sorted(evidence.metrics.items())
    ]
    report = normalize_json({
        "schema_version": REPORT_SCHEMA,
        "question_id": question_id,
        "question_hash": canonical_hash({"question": question}),
        "question": question,
        "machine_conclusion": evidence.verdict.value,
        "evidence_authority": evidence.authority.value,
        "evidence_snapshot_hash": evidence_hash,
        "tested": list(tested),
        "not_tested": list(not_tested),
        "data_scope": dict(evidence.data_scope),
        "missingness": dict(evidence.missingness),
        "methods": list(methods),
        "baselines": list(baselines),
        "controls": list(controls),
        "metrics": metrics,
        "cost_model": dict(evidence.cost_model),
        "subperiod_results": list(evidence.subperiod_results),
        "regime_results": list(evidence.regime_results),
        "layer_results": list(evidence.layer_results),
        "robustness": list(evidence.robustness),
        "falsification": list(evidence.falsification),
        "diagnostics": list(evidence.diagnostics),
        "specialists": list(specialists),
        "contributions": list(contributions),
        "conflicts": list(conflicts),
        "reproduction": {
            "evaluator_version": evidence.evaluator_version,
            "source_artifact_hashes": list(evidence.source_artifact_hashes),
            "associated_artifacts": list(associated_artifacts),
        },
        "next_experiment": next_experiment,
    })
    report["report_hash"] = canonical_hash(report)
    markdown = _render(report)
    _validate_report_numbers(markdown)
    return report, markdown


def _render(report: Mapping[str, Any]) -> str:
    evidence_hash = str(report["evidence_snapshot_hash"])
    lines = [
        "# AlphaForge（千因研究院）正式研究报告",
        "",
        "## 一句话机器结论",
        "",
        f"{_conclusion_cn(str(report['machine_conclusion']))} 证据等级为 `{report['evidence_authority']}`；机器结论绑定 `{evidence_hash}`。",
        "",
        "## 研究问题",
        "",
        f"{report['question']}（问题身份绑定 `{report['question_hash']}`）",
    ]
    _list(lines, "实际测试了什么", report["tested"], evidence_hash)
    _list(lines, "没有测试什么", report["not_tested"], evidence_hash)
    _mapping(lines, "数据范围、样本与覆盖", report["data_scope"], evidence_hash)
    _mapping(lines, "缺失率与排除规则", report["missingness"], evidence_hash)
    _list(lines, "方法", report["methods"], evidence_hash)
    _list(lines, "基线", report["baselines"], evidence_hash)
    _list(lines, "对照", report["controls"], evidence_hash)
    lines.extend(["", "## 关键机器指标", "", "| 指标 | 数值 | Evidence |", "|---|---:|---|"])
    for item in report["metrics"]:
        lines.append(f"| {item['name']} | {_display(item['value'])} | `{item['evidence_hash']}` |")
    _mapping(lines, "成本、换手与容量政策", report["cost_model"], evidence_hash)
    _structured(lines, "子时期、状态与分层结果", [
        *report["subperiod_results"], *report["regime_results"], *report["layer_results"],
    ], evidence_hash)
    _structured(lines, "稳健性与反证", [*report["robustness"], *report["falsification"]], evidence_hash)
    _structured(lines, "失败诊断", report["diagnostics"], evidence_hash)
    _structured(lines, "各研究组贡献", report["contributions"], evidence_hash)
    _structured(lines, "研究组冲突", report["conflicts"], evidence_hash)
    support, oppose = _support(report)
    _list(lines, "支持证据", support, evidence_hash)
    _list(lines, "反对证据", oppose, evidence_hash)
    lines.extend([
        "", "## 下一项针对性实验", "",
        f"- {report['next_experiment']}（研究设计绑定 `{evidence_hash}`）",
        "", "## 复现信息", "",
        f"- Evidence Snapshot：`{evidence_hash}`",
        f"- 报告标识：`{report['report_hash']}`",
        f"- 评价器版本：`{report['reproduction']['evaluator_version']}`（证据 `{evidence_hash}`）",
    ])
    for artifact in report["reproduction"]["source_artifact_hashes"]:
        lines.append(f"- 输入产物：`{artifact}`")
    for artifact in report["reproduction"]["associated_artifacts"]:
        lines.append(f"- 关联产物：`{artifact}`")
    lines.extend([
        "", "> 研究模型只生成计划、代码草案和解释；本报告数字来自独立确定性评价器。",
        "",
    ])
    return "\n".join(lines)


def _conclusion_cn(value: str) -> str:
    return {
        "SUPPORTED": "机器证据支持当前研究命题。",
        "CONDITIONALLY_SUPPORTED": "机器证据仅在已列条件下支持当前研究命题。",
        "REJECTED": "机器证据拒绝当前研究命题。",
        "INSUFFICIENT_EVIDENCE": "实验已经完成，但机器证据不足以确认当前命题。",
        "DATA_UNAVAILABLE": "冻结数据不足以对当前命题形成正式评价。",
    }[value]


def _display(value: Any) -> str:
    if isinstance(value, float):
        return format(value, ".10g")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _list(lines: list[str], title: str, values: Sequence[Any], evidence_hash: str) -> None:
    lines.extend(["", f"## {title}", ""])
    if not values:
        lines.append(f"- 无已登记项目。（证据 `{evidence_hash}`）")
    else:
        lines.extend(f"- {value}（证据 `{evidence_hash}`）" for value in values)


def _mapping(lines: list[str], title: str, value: Mapping[str, Any], evidence_hash: str) -> None:
    _structured(lines, title, [{key: item} for key, item in sorted(value.items())], evidence_hash)


def _structured(lines: list[str], title: str, values: Sequence[Mapping[str, Any]], evidence_hash: str) -> None:
    lines.extend(["", f"## {title}", ""])
    if not values:
        lines.append(f"- 无已登记结果。（证据 `{evidence_hash}`）")
    else:
        lines.extend(
            f"- {json.dumps(normalize_json(item), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}（证据 `{evidence_hash}`）"
            for item in values
        )


def _support(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    verdict = report["machine_conclusion"]
    if verdict == "SUPPORTED":
        return ["主要机器指标与已登记稳健性检查方向一致。"], ["仍需关注报告列出的成本、容量与状态限制。"]
    if verdict == "CONDITIONALLY_SUPPORTED":
        return ["至少一个已登记机器指标支持命题。"], ["效应依赖已列条件，不能外推到未测试范围。"]
    if verdict == "REJECTED":
        return [], ["主要机器指标未达到确定性结论政策。"]
    return [], ["有效样本、覆盖或统计功效不足。"]


def _validate_report_numbers(markdown: str) -> None:
    """Every customer-facing numeric line must carry an Evidence/hash binding."""

    in_metrics = False
    for line in markdown.splitlines():
        if line.startswith("## 关键机器指标"):
            in_metrics = True
            continue
        if in_metrics and line.startswith("## "):
            in_metrics = False
        if not _NUMBER.search(line):
            continue
        if line.startswith("# ") or "schema" in line.lower():
            continue
        if _HASH.search(line) is None:
            raise ValueError(f"numeric report line lacks Evidence binding: {line[:80]}")


__all__ = ["REPORT_SCHEMA", "build_report_v2"]
