"""Compile user questions into semantic answer requirements.

The compiler deliberately does not select an evaluator or result-table type.
It states what the final answer must establish.  Research agents remain free to
choose one or more suitable experiments; deterministic coverage later checks
whether their recorded outputs actually answer each requirement.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..agent_research_sandbox.process_runner import canonical_hash
from .contracts import QuestionRequirement, ResearchAnswerSpec


class ResearchAnswerContractCompiler:
    RULE_VERSION = "alphaforge.semantic-answer-contract/v1"

    def compile(
        self,
        *,
        question_id: str,
        question: str,
        task_hashes: Iterable[str],
    ) -> ResearchAnswerSpec:
        normalized = re.sub(r"\s+", "", question).casefold()
        requirements: list[QuestionRequirement] = []

        def add(
            requirement_id: str,
            text: str,
            semantics: tuple[str, ...],
            *,
            metrics: tuple[str, ...] = (),
            comparison: bool = False,
            minimum: int = 1,
            depends_on: tuple[str, ...] = (),
            semantic_match: str = "ALL",
        ) -> None:
            requirements.append(QuestionRequirement(
                requirement_id=requirement_id,
                question_text=text,
                output_kind=None,
                dimensions=tuple(sorted(semantics)),
                required_metrics=tuple(sorted(metrics)),
                depends_on=tuple(sorted(depends_on)),
                comparison_required=comparison,
                minimum_distinct_candidates=minimum,
                semantic_match=semantic_match,
            ))

        # Every run must answer the primary relationship or research claim.
        add(
            "R10_PRIMARY_CONCLUSION",
            "原问题中的核心预测关系、机制或诊断结论是否得到机器证据支持？",
            ("controlled_relation", "event_effect", "prediction", "ranked_causes"),
            semantic_match="ANY",
        )

        is_search = _contains(normalized, "寻找", "挖", "最佳", "哪个", "比较", "改进", "优化", "候选")
        if is_search:
            add(
                "R20_CANDIDATE_COMPARISON",
                "实际比较了哪些不同定义，哪个候选相对更好，哪些被否定？",
                ("candidate_comparison", "prediction"),
                comparison=True,
                minimum=2,
            )

        if _contains(normalized, "成本", "换手", "容量", "回撤", "可交易", "稳定") or is_search:
            add(
                "R30_TRADING_QUALITY",
                "候选在成本、换手、回撤和跨时期稳定性方面是否可接受？",
                ("cost_after_result", "stability", "trading_quality"),
                metrics=("max_drawdown", "net_return", "turnover"),
            )

        if _contains(normalized, "低相关", "相关", "增量", "控制", "残差", "已有"):
            add(
                "R40_INCREMENTALITY",
                "控制已有因子后是否仍有增量信息，相关性是否符合要求？",
                ("controlled_relation", "incrementality"),
            )

        if _contains(normalized, "shibor", "状态", "环境", "高低", "牛市", "熊市", "波动期"):
            add(
                "R50_REGIME_STABILITY",
                "结论在用户指定的市场状态之间是否稳定，差异是什么？",
                ("regime_difference", "regime_samples", "regime_stability"),
            )

        if _contains(normalized, "公告", "事件", "财报", "业绩预告", "业绩快报", "漂移", "龙虎榜"):
            add(
                "R60_EVENT_DESIGN",
                "事件何时公开并可交易，事件窗和样本如何定义？",
                ("event_timing",),
            )
            add(
                "R61_EVENT_OUTCOME",
                "事件后的异常收益是延续还是反转，相对对照组是否成立？",
                ("control_and_placebo", "event_effect"),
                metrics=("event_count", "mean_car", "mean_control_adjusted_car", "mean_placebo_car"),
                depends_on=("R60_EVENT_DESIGN",),
            )

        if is_search or _contains(normalized, "公式", "定义", "alpha", "因子"):
            dependencies = ("R20_CANDIDATE_COMPARISON",) if is_search else ()
            add(
                "R90_IMPLEMENTABLE_DEFINITION",
                "最终保留候选的字段、变换、方向、窗口和预测周期是什么？",
                ("implementable_definition",),
                depends_on=dependencies,
            )

        return ResearchAnswerSpec(
            question_id=question_id,
            question=question,
            question_hash=canonical_hash({"question": question}),
            requirements=tuple(sorted(requirements, key=lambda item: item.requirement_id)),
            task_hashes=tuple(sorted(set(task_hashes))),
            rule_version=self.RULE_VERSION,
        )


def _contains(value: str, *tokens: str) -> bool:
    return any(token.casefold() in value for token in tokens)


__all__ = ["ResearchAnswerContractCompiler"]
