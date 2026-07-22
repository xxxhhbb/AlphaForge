"""Typed research-task planning for the unique Agent research product path.

The planner constrains authority and output contracts, not research methods.  It
uses the audited 500-question catalog when possible and falls back to the
existing Campaign compiler for arbitrary user questions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ..research_campaign import DeterministicCampaignCompiler, MethodModel, ResearchMode


PLANNER_RULE_VERSION = "alphaforge.research-task-planner/v2"
OUTPUT_KIND_BY_EXPERIMENT_TYPE: Mapping[str, str] = {
    "CROSS_SECTIONAL_VALIDATION": "ScoreTable",
    "QUANTILE_MONOTONICITY": "ScoreTable",
    "NEUTRALIZED_VALIDATION": "ScoreTable",
    "RESIDUAL_INCREMENTAL": "RegressionTable",
    "NONLINEAR_DIAGNOSTIC": "RegressionTable",
    "REGISTERED_CORRELATION": "RegressionTable",
    "REGIME_CONDITIONING": "RegimeComparisonTable",
    "LIQUIDITY_CONDITIONING": "RegimeComparisonTable",
    "UNIVERSE_ROBUSTNESS": "RegimeComparisonTable",
    "SUBPERIOD_STABILITY": "RegimeComparisonTable",
    "PARAMETER_ROBUSTNESS": "RegimeComparisonTable",
    "DIAGNOSTIC_MONITORING": "DiagnosticTable",
    "ADVERSARIAL_FALSIFICATION": "ScoreTable",
    "CAPACITY_STRESS": "ScoreTable",
    "COST_STRESS": "ScoreTable",
    "OUTLIER_ROBUSTNESS": "ScoreTable",
    "PIT_DELAY_TEST": "ScoreTable",
    "TURNOVER_OPTIMIZATION": "ScoreTable",
}
STANDARD_OUTPUT_KINDS = frozenset(
    {
        "ScoreTable",
        "RegressionTable",
        "RegimeComparisonTable",
        "EventTable",
        "DiagnosticTable",
        "DatasetAssessmentTable",
    }
)
SPECIALISTS = frozenset(
    {
        "alternative_data",
        "expectations_events",
        "fundamental",
        "macro_regime",
        "market_structure",
        "meta_alpha",
    }
)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_question(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("question must be non-empty text")
    return re.sub(r"\s+", "", value).casefold().rstrip("。？！?!")


@dataclass(frozen=True, slots=True)
class ResearchTaskPlan:
    question: str
    mode: str
    selected_specialists: tuple[str, ...]
    lead_specialist: str
    output_kind: str
    required_data: tuple[str, ...]
    experiment_type: str
    expected_result_kind: str
    source: str
    specialist_output_kinds: tuple[tuple[str, str], ...] = ()
    rule_version: str = PLANNER_RULE_VERSION

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("question cannot be empty")
        if self.lead_specialist not in SPECIALISTS:
            raise ValueError("lead_specialist is not registered")
        if not self.selected_specialists or self.lead_specialist not in self.selected_specialists:
            raise ValueError("selected_specialists must include lead_specialist")
        if len(set(self.selected_specialists)) != len(self.selected_specialists):
            raise ValueError("selected_specialists contains duplicates")
        if not set(self.selected_specialists).issubset(SPECIALISTS):
            raise ValueError("selected_specialists contains an unknown specialist")
        if self.output_kind not in STANDARD_OUTPUT_KINDS:
            raise ValueError("unsupported output_kind")
        if not self.required_data:
            raise ValueError("required_data cannot be empty")
        output_kinds = self.specialist_output_kinds or tuple(
            (specialist, self.output_kind) for specialist in sorted(self.selected_specialists)
        )
        if tuple(sorted(output_kinds)) != output_kinds:
            raise ValueError("specialist_output_kinds must be stable-sorted")
        if {item[0] for item in output_kinds} != set(self.selected_specialists):
            raise ValueError("specialist_output_kinds must cover every selected specialist")
        if len({item[0] for item in output_kinds}) != len(output_kinds):
            raise ValueError("specialist_output_kinds contains duplicate specialists")
        if any(item[1] not in STANDARD_OUTPUT_KINDS for item in output_kinds):
            raise ValueError("specialist_output_kinds contains an unsupported output kind")
        object.__setattr__(self, "specialist_output_kinds", output_kinds)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": PLANNER_RULE_VERSION,
            "question": self.question,
            "mode": self.mode,
            "selected_specialists": list(self.selected_specialists),
            "lead_specialist": self.lead_specialist,
            "output_kind": self.output_kind,
            "required_data": list(self.required_data),
            "experiment_type": self.experiment_type,
            "expected_result_kind": self.expected_result_kind,
            "source": self.source,
            "specialist_output_kinds": [list(item) for item in self.specialist_output_kinds],
            "rule_version": self.rule_version,
        }
        value["artifact_hash"] = _canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return self.to_dict()["artifact_hash"]

    def output_kind_for(self, specialist_id: str) -> str:
        try:
            return dict(self.specialist_output_kinds)[specialist_id]
        except KeyError as exc:
            raise ValueError("specialist is absent from the task plan") from exc


class ResearchTaskPlanner:
    """Compile task authority and result shape while leaving methods to AgentLoop."""

    def __init__(self, *, catalog_path: str | Path | None = None) -> None:
        self.catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.campaign_compiler = DeterministicCampaignCompiler()
        self._catalog_by_question: dict[str, Any] | None = None

    def compile(self, question: str, *, catalog_path: str | Path | None = None) -> ResearchTaskPlan:
        path = Path(catalog_path) if catalog_path is not None else self.catalog_path
        if path is not None and path.is_file():
            item = self._catalog(path).get(_normalized_question(question))
            if item is not None:
                return self.compile_catalog_question(item)
        return self._compile_arbitrary(question)

    def compile_catalog_question(self, item: Any) -> ResearchTaskPlan:
        output_kind = OUTPUT_KIND_BY_EXPERIMENT_TYPE.get(str(item.experiment_type))
        if output_kind is None:
            raise ValueError(f"unmapped catalog experiment_type: {item.experiment_type}")
        mode = _mode_for_expected_result(str(item.expected_result_kind))
        specialists = _specialists_for_lead(str(item.lead_specialist))
        return ResearchTaskPlan(
            question=str(item.question),
            mode=mode,
            selected_specialists=specialists,
            lead_specialist=str(item.lead_specialist),
            output_kind=output_kind,
            required_data=tuple(sorted(str(value) for value in item.required_data)),
            experiment_type=str(item.experiment_type),
            expected_result_kind=str(item.expected_result_kind),
            source="RESEARCH500_CATALOG",
            specialist_output_kinds=tuple(
                (specialist, output_kind) for specialist in sorted(specialists)
            ),
        )

    def _catalog(self, path: Path) -> dict[str, Any]:
        if self._catalog_by_question is None:
            from ..research500_harness import load_catalog

            catalog = load_catalog(path)
            self._catalog_by_question = {
                _normalized_question(item.question): item for item in catalog.questions
            }
        return self._catalog_by_question

    def _compile_arbitrary(self, question: str) -> ResearchTaskPlan:
        result = self.campaign_compiler.compile_text(question)
        recommendation = result.recommendation
        selected = tuple(dict.fromkeys(str(value) for value in recommendation.candidate_agents))
        if not selected:
            selected = ("market_structure", "meta_alpha")
        lead = selected[0]
        output_kind, experiment_type, expected_kind = _output_for_campaign(
            recommendation.mode, tuple(recommendation.method_models)
        )
        required_data = (
            tuple(result.draft.subject.allowed_data)
            if result.draft is not None
            else ("price_volume",)
        )
        tags = tuple(result.draft.subject.tags) if result.draft is not None else ()
        return ResearchTaskPlan(
            question=result.sanitized_user_text,
            mode=recommendation.mode.value,
            selected_specialists=selected,
            lead_specialist=lead,
            output_kind=output_kind,
            required_data=tuple(sorted(required_data)),
            experiment_type=experiment_type,
            expected_result_kind=expected_kind,
            source="DETERMINISTIC_CAMPAIGN_COMPILER",
            specialist_output_kinds=_specialist_output_kinds(
                selected=selected,
                primary_output_kind=output_kind,
                mode=recommendation.mode,
                methods=tuple(recommendation.method_models),
                tags=tags,
            ),
        )


def _mode_for_expected_result(expected_result_kind: str) -> str:
    if expected_result_kind == "DIAGNOSTIC_REPORT":
        return ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO.value
    if expected_result_kind == "PORTFOLIO_GAP_OR_INCREMENTAL_REPORT":
        return ResearchMode.PORTFOLIO_GAP_SEARCH.value
    return ResearchMode.VALIDATE_IDEA.value


def _specialists_for_lead(lead: str) -> tuple[str, ...]:
    supporting = {
        "market_structure": ("meta_alpha",),
        "fundamental": ("market_structure", "meta_alpha"),
        "expectations_events": ("market_structure", "alternative_data"),
        "alternative_data": ("expectations_events", "market_structure"),
        "macro_regime": ("market_structure", "meta_alpha"),
        "meta_alpha": ("market_structure",),
    }
    return (lead, *supporting[lead])


def _specialist_output_kinds(
    *,
    selected: tuple[str, ...],
    primary_output_kind: str,
    mode: ResearchMode,
    methods: tuple[MethodModel, ...],
    tags: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    tag_set = set(tags)
    result: dict[str, str] = {specialist: primary_output_kind for specialist in selected}
    if "macro_regime" in result and (
        tag_set & {"macro", "high_volatility"}
        or MethodModel.REGIME_CONDITIONING in methods
    ):
        result["macro_regime"] = "RegimeComparisonTable"
    if "expectations_events" in result and (
        tag_set & {"earnings", "earnings_revision", "event"}
        or MethodModel.EVENT_STUDY in methods
    ):
        result["expectations_events"] = "EventTable"
    if "alternative_data" in result and mode is ResearchMode.NEW_DATA_SOURCE_EVALUATION:
        result["alternative_data"] = "DatasetAssessmentTable"
    if "meta_alpha" in result and (
        mode is ResearchMode.PORTFOLIO_GAP_SEARCH
        or MethodModel.RESIDUAL_INCREMENTAL in methods
    ):
        result["meta_alpha"] = "RegressionTable"
    return tuple(sorted(result.items()))


def _output_for_campaign(
    mode: ResearchMode, methods: tuple[MethodModel, ...]
) -> tuple[str, str, str]:
    if mode is ResearchMode.NEW_DATA_SOURCE_EVALUATION:
        return "DatasetAssessmentTable", "DATASET_ASSESSMENT", "DATASET_ASSESSMENT"
    if mode in {ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO, ResearchMode.MONITOR_DECAY_RETIREMENT}:
        return "DiagnosticTable", "DIAGNOSTIC_MONITORING", "DIAGNOSTIC_REPORT"
    if mode is ResearchMode.EVENT_MARKET_TOPIC or MethodModel.EVENT_STUDY in methods:
        return "EventTable", "EVENT_STUDY", "EVENT_STUDY_REPORT"
    if MethodModel.REGIME_CONDITIONING in methods:
        return "RegimeComparisonTable", "REGIME_CONDITIONING", "VALIDATION_CONCLUSION"
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH or MethodModel.RESIDUAL_INCREMENTAL in methods:
        return "RegressionTable", "RESIDUAL_INCREMENTAL", "PORTFOLIO_GAP_OR_INCREMENTAL_REPORT"
    if mode is ResearchMode.VALIDATE_IDEA:
        return "RegressionTable", "HYPOTHESIS_VALIDATION", "VALIDATION_CONCLUSION"
    return "ScoreTable", "CANDIDATE_EXPLORATION", "CANDIDATE_SET"


__all__ = [
    "OUTPUT_KIND_BY_EXPERIMENT_TYPE",
    "PLANNER_RULE_VERSION",
    "ResearchTaskPlan",
    "ResearchTaskPlanner",
    "STANDARD_OUTPUT_KINDS",
]
