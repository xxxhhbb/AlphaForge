"""Mode-appropriate semantic adapters for standard research outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ResearchOutputAnswerAdapter:
    output_kind: str
    requirement_text: str
    dimensions: tuple[str, ...]
    evaluation_label: str

    def answer(self, *, positive_count: int, negative_count: int, revision_count: int) -> str:
        if positive_count:
            return (
                f"{self.evaluation_label}发现 {positive_count} 个获得支持的候选；"
                f"同时有 {negative_count} 个被拒绝、{revision_count} 个需要修订。"
            )
        if negative_count:
            return f"{self.evaluation_label}已完成，但 {negative_count} 个候选均未获得支持。"
        return f"{self.evaluation_label}形成了部分结果，但尚不足以保留或拒绝候选。"

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        """Translate evaluator-owned values without deriving new metrics."""

        values = dict(metrics)
        if not values:
            return "没有形成可引用的机器指标。"
        selected = tuple(sorted(values.items()))[:6]
        return "机器直接记录：" + "；".join(f"{name}={value}" for name, value in selected) + "。"


class ScoreTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "ScoreTable",
            "候选是否具备预测能力，并在成本、稳定性和增量性检查后仍可保留？",
            ("cost_after_result", "incrementality", "prediction", "stability"),
            "横截面预测、成本和稳定性评价",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        values = dict(metrics)
        parts = _named(values, (
            ("ic_mean", "IC均值"), ("rank_ic", "RankIC"),
            ("icir", "ICIR"), ("net_return", "成本后收益"),
            ("turnover", "换手率"),
            ("subperiod_positive_fraction", "子时期正向比例"),
            ("regime_positive_fraction", "状态正向比例"),
            ("incremental_ic_mean", "增量IC"),
            ("max_registered_abs_correlation", "与登记因子最高绝对相关"),
        ))
        caveat = ""
        if values.get("max_registered_abs_correlation") in {"0", "0.0", "0.000000"}:
            caveat = " 相关性记录为0时必须核对基准因子是否实际载入，不能仅凭该数值宣称低相关。"
        return ("机器直接检验：" + "；".join(parts) + "。" if parts else super().summarize_metrics(metrics)) + caveat


class RegressionTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "RegressionTable",
            "核心关系在控制相关变量后是否仍成立，统计诊断是否可靠？",
            ("collinearity", "controlled_relation", "sample_and_uncertainty"),
            "控制变量后的统计关系评价",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        values = dict(metrics)
        terms = sorted(name.split(".", 1)[1] for name in values if name.startswith("coefficient."))
        rows = []
        for term in terms[:6]:
            coefficient = values.get(f"coefficient.{term}")
            p_value = values.get(f"p_value_normal.{term}")
            sample = values.get(f"sample_count.{term}")
            rows.append(
                f"{term}系数={coefficient}"
                + (f"、p值={p_value}" if p_value is not None else "")
                + (f"、样本={sample}" if sample is not None else "")
            )
        condition = values.get("condition_number")
        if condition is not None:
            rows.append(f"条件数={condition}")
        return "控制变量后的直接结果：" + "；".join(rows) + "。" if rows else super().summarize_metrics(metrics)


class RegimeComparisonTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "RegimeComparisonTable",
            "研究关系在不同市场状态下是否稳定，状态差异是什么？",
            ("regime_difference", "regime_samples", "regime_stability"),
            "分状态效果与稳定性评价",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        values = dict(metrics)
        rows = []
        for name, value in sorted(values.items()):
            if name.startswith("difference."):
                rows.append(f"{name.removeprefix('difference.')}状态差={value}")
            elif name.startswith("sign_stability."):
                rows.append(f"{name.removeprefix('sign_stability.')}同号稳定={value}")
            elif name.startswith("minimum_sample_count."):
                rows.append(f"{name.removeprefix('minimum_sample_count.')}最小状态样本={value}")
        effects = [
            f"{name.removeprefix('effect.')}={value}"
            for name, value in sorted(values.items()) if name.startswith("effect.")
        ][:6]
        rows.extend(effects)
        return "分状态直接结果：" + "；".join(rows[:10]) + "。" if rows else super().summarize_metrics(metrics)


class EventTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "EventTable",
            "事件发生并可交易后是否存在相对对照组的延续或反转效应？",
            ("control_and_placebo", "event_effect", "event_timing"),
            "事件窗、对照与安慰剂评价",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        values = dict(metrics)
        parts = _named(values, (
            ("event_count", "事件数"), ("mean_car", "平均CAR"),
            ("mean_control_adjusted_car", "控制组调整后CAR"),
            ("mean_placebo_car", "安慰剂CAR"),
            ("event_window_start", "事件窗起点"),
            ("event_window_end", "事件窗终点"),
        ))
        return "事件研究直接结果：" + "；".join(parts) + "。" if parts else super().summarize_metrics(metrics)


class DiagnosticTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "DiagnosticTable",
            "最可能的根因是什么，哪些原因被证据支持或排除？",
            ("ranked_causes", "recommended_action", "support_and_opposition"),
            "根因排序和反证诊断",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        rows = [f"{name}={value}" for name, value in sorted(metrics)][:10]
        return "根因诊断直接结果：" + "；".join(rows) + "。" if rows else super().summarize_metrics(metrics)


class DatasetAssessmentTableAnswerAdapter(ResearchOutputAnswerAdapter):
    def __init__(self) -> None:
        super().__init__(
            "DatasetAssessmentTable",
            "该数据在覆盖、PIT 和偏差方面是否适合当前研究？",
            ("coverage", "pit_and_revision", "research_suitability"),
            "覆盖、PIT 与偏差评价",
        )

    def summarize_metrics(self, metrics: Iterable[tuple[str, str]]) -> str:
        values = dict(metrics)
        rows = []
        for name, value in sorted(values.items()):
            if name.startswith("coverage."):
                rows.append(f"{name.removeprefix('coverage.')}覆盖率={value}")
            elif name.startswith("pit_eligible."):
                rows.append(f"{name.removeprefix('pit_eligible.')} PIT可用={value}")
        if "minimum_coverage" in values:
            rows.append(f"最低覆盖率={values['minimum_coverage']}")
        return "数据适用性直接结果：" + "；".join(rows[:12]) + "。" if rows else super().summarize_metrics(metrics)


def _named(values: dict[str, str], names: tuple[tuple[str, str], ...]) -> list[str]:
    return [f"{label}={values[name]}" for name, label in names if name in values]


class AnswerAdapterRegistry:
    def __init__(self) -> None:
        adapters = (
            ScoreTableAnswerAdapter(), RegressionTableAnswerAdapter(),
            RegimeComparisonTableAnswerAdapter(), EventTableAnswerAdapter(),
            DiagnosticTableAnswerAdapter(), DatasetAssessmentTableAnswerAdapter(),
        )
        self._adapters = {item.output_kind: item for item in adapters}

    def get(self, output_kind: str) -> ResearchOutputAnswerAdapter:
        try:
            return self._adapters[output_kind]
        except KeyError as exc:
            raise ValueError(f"unsupported research output kind: {output_kind}") from exc

    @property
    def output_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


__all__ = [
    "AnswerAdapterRegistry", "DatasetAssessmentTableAnswerAdapter",
    "DiagnosticTableAnswerAdapter", "EventTableAnswerAdapter",
    "RegressionTableAnswerAdapter", "RegimeComparisonTableAnswerAdapter",
    "ResearchOutputAnswerAdapter", "ScoreTableAnswerAdapter",
]
