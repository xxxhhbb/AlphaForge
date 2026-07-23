"""Data-driven semantic checks for generated cross-sectional formulas."""

from __future__ import annotations

import math
from itertools import combinations
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash
from ..research_answer.source_summary import summarize_reviewed_source
from .contracts import (
    FeatureSemanticProfile,
    FormulaFindingSeverity,
    FormulaReviewResult,
    FormulaSemanticFinding,
)


class FormulaSemanticReviewer:
    """Review formula meaning from frozen rows, never from field-name folklore."""

    def __init__(self, *, constant_fraction_threshold: float = 0.95) -> None:
        if not 0.5 <= constant_fraction_threshold <= 1.0:
            raise ValueError("constant threshold must be in [0.5, 1]")
        self.constant_fraction_threshold = float(constant_fraction_threshold)

    def review(
        self,
        *,
        source_text: str,
        output_kind: str,
        feature_rows: Sequence[Any],
        data_snapshot_hash: str,
    ) -> FormulaReviewResult:
        source_hash = "sha256:" + __import__("hashlib").sha256(source_text.encode("utf-8")).hexdigest()
        summary = summarize_reviewed_source(source_text, output_kind=output_kind)
        if summary is None or output_kind != "ScoreTable":
            return FormulaReviewResult(source_hash, data_snapshot_hash, (), ())
        profiles = tuple(
            self._profile(field, feature_rows)
            for field in summary.input_fields
        )
        by_name = {item.field_name: item for item in profiles}
        findings: list[FormulaSemanticFinding] = []
        for field in summary.additive_fields:
            profile = by_name.get(field)
            if profile is None or profile.cross_section_group_count < 1:
                continue
            if profile.cross_section_constant_fraction >= self.constant_fraction_threshold:
                findings.append(FormulaSemanticFinding(
                    code="CROSS_SECTION_CONSTANT_ADDITIVE_TERM",
                    severity=FormulaFindingSeverity.REVISION_REQUIRED,
                    field_names=(field,),
                    message=(
                        f"字段 {field} 在同一决策时点的横截面中基本不变化，"
                        "不能作为加减项直接贡献股票排序；请改为状态分组、交互项或移除。"
                    ),
                ))
        for left, right in combinations(summary.input_fields, 2):
            correlation = self._correlation(left, right, feature_rows)
            if correlation is not None and abs(correlation) >= 0.98:
                findings.append(FormulaSemanticFinding(
                    code="HIGH_FEATURE_CORRELATION",
                    severity=FormulaFindingSeverity.ADVISORY,
                    field_names=tuple(sorted((left, right))),
                    message=(
                        f"字段 {left} 与 {right} 在冻结输入中高度相关；"
                        "该提示不阻断研究，但报告应披露冗余与稳健性风险。"
                    ),
                ))
        return FormulaReviewResult(
            source_hash=source_hash,
            data_snapshot_hash=data_snapshot_hash,
            profiles=profiles,
            findings=tuple(sorted(findings, key=lambda item: (item.code, item.field_names))),
        )

    @staticmethod
    def _profile(field: str, rows: Sequence[Any]) -> FeatureSemanticProfile:
        grouped: dict[str, list[float]] = defaultdict(list)
        usable = 0
        for row in rows:
            values = getattr(row, "features", row)
            if not isinstance(values, Mapping):
                continue
            raw = values.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                continue
            decision = getattr(row, "decision_time", values.get("decision_time", ""))
            grouped[str(decision)].append(float(raw))
            usable += 1
        cross_sections = [values for values in grouped.values() if len(values) >= 2]
        constant = sum(1 for values in cross_sections if max(values) - min(values) <= 1e-12)
        total = max(1, len(rows))
        return FeatureSemanticProfile(
            field_name=field,
            observation_count=total,
            usable_observation_count=usable,
            decision_group_count=len(grouped),
            cross_section_group_count=len(cross_sections),
            cross_section_constant_fraction=(constant / len(cross_sections) if cross_sections else 0.0),
            coverage=usable / total,
        )

    @staticmethod
    def _correlation(left: str, right: str, rows: Sequence[Any]) -> float | None:
        pairs: list[tuple[float, float]] = []
        for row in rows:
            values = getattr(row, "features", row)
            if not isinstance(values, Mapping):
                continue
            a, b = values.get(left), values.get(right)
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (a, b)):
                continue
            if not math.isfinite(float(a)) or not math.isfinite(float(b)):
                continue
            pairs.append((float(a), float(b)))
        if len(pairs) < 3:
            return None
        mean_a = sum(a for a, _ in pairs) / len(pairs)
        mean_b = sum(b for _, b in pairs) / len(pairs)
        covariance = sum((a - mean_a) * (b - mean_b) for a, b in pairs)
        variance_a = sum((a - mean_a) ** 2 for a, _ in pairs)
        variance_b = sum((b - mean_b) ** 2 for _, b in pairs)
        if variance_a <= 1e-24 or variance_b <= 1e-24:
            return None
        return covariance / math.sqrt(variance_a * variance_b)


__all__ = ["FormulaSemanticReviewer"]
