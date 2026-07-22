"""Research-visible evidence queries and experiment-design helpers.

These helpers are intentionally read-only.  They consume visible
train/validation summaries through an injected reader and emit deterministic
plans or explanations.  They do not evaluate factors, write Trial Ledger
events, compute official evidence, decide promotion, or expose sealed results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from veriquant.research_tool_contracts import (
    ExperimentDesignPlan,
    ResearchToolFailureCode,
    content_hash,
)


_VISIBLE_SPLITS = {"train", "validation"}
_SEALED_KEYS = {
    "sealed",
    "sealed_metric",
    "sealed_metrics",
    "sealed_rank",
    "sealed_reason",
    "sealed_path",
    "test_metric",
    "test_metrics",
    "test_rank",
    "promotion",
    "promotion_decision",
    "official_metrics",
    "raw_sealed_reader",
}
_AUTHORITY_KEYS = {
    "promotion",
    "promotion_decision",
    "official_evidence",
    "official_metrics",
    "risk_approval",
    "target_weights",
    "orders",
    "ledger",
}
_PATH_URL_RE = re.compile(r"(?i)(https?://|[a-z]:\\|\\\\|(?:^|[\s])\.\./)")


class ExplorerFailureCode(str, Enum):
    SEALED_LEAKAGE = "SEALED_LEAKAGE"
    AUTHORITY_REJECTED = "AUTHORITY_REJECTED"
    AUXILIARY_MASQUERADE = "AUXILIARY_MASQUERADE"
    CANDIDATE_UNAVAILABLE = "CANDIDATE_UNAVAILABLE"
    INVALID_VISIBLE_EVIDENCE = "INVALID_VISIBLE_EVIDENCE"
    INVALID_EXPERIMENT_DESIGN = "INVALID_EXPERIMENT_DESIGN"
    TRIAL_LEDGER_READ_ONLY = "TRIAL_LEDGER_READ_ONLY"


class ResearchExplorerError(ValueError):
    def __init__(self, code: ExplorerFailureCode, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: ExplorerFailureCode, message: str, **details: Any) -> None:
    raise ResearchExplorerError(code, message, details=details)


def _clean(value: Any, path: str = "payload") -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(ExplorerFailureCode.INVALID_VISIBLE_EVIDENCE, "visible evidence keys must be strings", path=path)
            normalized = key.lower()
            if normalized in _AUTHORITY_KEYS:
                _fail(ExplorerFailureCode.AUTHORITY_REJECTED, "privileged authority field is not visible", path=f"{path}.{key}")
            if normalized in _SEALED_KEYS:
                _fail(ExplorerFailureCode.SEALED_LEAKAGE, "sealed-derived field is not visible", path=f"{path}.{key}")
            out[key] = _clean(item, f"{path}.{key}")
        return MappingProxyType(dict(sorted(out.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_clean(item, f"{path}[{index}]") for index, item in enumerate(value))
    if isinstance(value, str):
        lowered = value.lower()
        if "sealed" in lowered or "promotion_decision" in lowered or "official_metrics" in lowered:
            _fail(ExplorerFailureCode.SEALED_LEAKAGE, "sealed or privileged text is not visible", path=path)
        if _PATH_URL_RE.search(value):
            _fail(ExplorerFailureCode.INVALID_VISIBLE_EVIDENCE, "path or URL is not visible evidence", path=path)
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    _fail(ExplorerFailureCode.INVALID_VISIBLE_EVIDENCE, f"unsupported visible evidence type: {type(value).__name__}")


def _as_tuple(values: Sequence[str] | None, field: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        _fail(ExplorerFailureCode.INVALID_EXPERIMENT_DESIGN, f"{field} must be a sequence")
    result = tuple(str(item) for item in values if isinstance(item, str) and item.strip())
    if len(result) != len(values):
        _fail(ExplorerFailureCode.INVALID_EXPERIMENT_DESIGN, f"{field} values must be strings")
    return tuple(sorted(dict.fromkeys(result)))


@dataclass(frozen=True, kw_only=True)
class ExplorerToolResult:
    tool_name: str
    payload: Mapping[str, Any]
    result_kind: str = "DRAFT_SUPPORT"

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            _fail(ExplorerFailureCode.INVALID_VISIBLE_EVIDENCE, "tool_name is required")
        if self.result_kind not in {"DRAFT_SUPPORT", "AUXILIARY_RESEARCH_EVIDENCE"}:
            _fail(ExplorerFailureCode.AUTHORITY_REJECTED, "explorer tools cannot emit official evidence")
        object.__setattr__(self, "payload", _clean(self.payload))

    @property
    def artifact_hash(self) -> str:
        return content_hash(
            {
                "tool_name": self.tool_name,
                "payload": dict(self.payload),
                "result_kind": self.result_kind,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "result_kind": self.result_kind,
            "payload": dict(self.payload),
            "artifact_hash": self.artifact_hash,
        }


@runtime_checkable
class VisibleEvidenceReader(Protocol):
    def read_candidate(self, candidate_id: str) -> Mapping[str, Any]: ...

    def read_trial_budget(self, family_id: str) -> Mapping[str, Any]: ...


class InMemoryVisibleEvidenceReader:
    def __init__(
        self,
        *,
        candidates: Mapping[str, Mapping[str, Any]] | None = None,
        budgets: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._candidates = dict(candidates or {})
        self._budgets = dict(budgets or {})

    def read_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        if candidate_id not in self._candidates:
            _fail(ExplorerFailureCode.CANDIDATE_UNAVAILABLE, "candidate is unavailable", candidate_id=candidate_id)
        return self._candidates[candidate_id]

    def read_trial_budget(self, family_id: str) -> Mapping[str, Any]:
        return self._budgets.get(
            family_id,
            {"family_id": family_id, "budget_limit": 0, "budget_consumed": 0, "budget_remaining": 0},
        )


def _visible_candidate(reader: VisibleEvidenceReader, candidate_id: str) -> Mapping[str, Any]:
    raw = _clean(reader.read_candidate(candidate_id))
    split = raw.get("split")
    if split not in _VISIBLE_SPLITS:
        _fail(ExplorerFailureCode.SEALED_LEAKAGE, "only train/validation evidence is visible", split=split)
    source_component = str(raw.get("source_component", "")).upper()
    evidence_class = str(raw.get("evidence_class", "AUXILIARY_RESEARCH_EVIDENCE")).upper()
    if source_component in {"QLIB", "QUANTAALPHA", "LLM_RESEARCHER"} and evidence_class == "OFFICIAL_EVIDENCE":
        _fail(ExplorerFailureCode.AUXILIARY_MASQUERADE, "auxiliary source cannot masquerade as official evidence")
    return raw


class VisibleEvidenceToolbox:
    def __init__(self, reader: VisibleEvidenceReader) -> None:
        if not isinstance(reader, VisibleEvidenceReader):
            _fail(ExplorerFailureCode.INVALID_VISIBLE_EVIDENCE, "reader must implement VisibleEvidenceReader")
        self.reader = reader

    def query_visible_evidence(self, candidate_id: str) -> ExplorerToolResult:
        visible = _visible_candidate(self.reader, candidate_id)
        return ExplorerToolResult(tool_name="query_visible_evidence", payload=visible)

    def compare_candidates(self, candidate_ids: Sequence[str]) -> ExplorerToolResult:
        rows = tuple(_visible_candidate(self.reader, candidate_id) for candidate_id in candidate_ids)
        summaries = []
        for row in rows:
            metrics = dict(row.get("metrics", {}))
            summaries.append(
                {
                    "candidate_id": row.get("candidate_id"),
                    "split": row.get("split"),
                    "net_icir": metrics.get("net_icir"),
                    "stability": metrics.get("stability"),
                    "cost_bps": metrics.get("cost_bps"),
                    "incrementality": metrics.get("incrementality"),
                    "official_eligible": row.get("evidence_class") == "OFFICIAL_EVIDENCE" and row.get("source_component") == "EVIDENCE_SERVICE",
                }
            )
        return ExplorerToolResult(tool_name="compare_candidates", payload={"candidates": summaries})

    def explain_rejection(self, candidate_id: str) -> ExplorerToolResult:
        row = _visible_candidate(self.reader, candidate_id)
        metrics = dict(row.get("metrics", {}))
        reason_codes = list(row.get("reason_codes", ()))
        if metrics.get("gross_return", 0.0) > 0.0 and metrics.get("net_return", 0.0) < 0.0:
            reason_codes.append("COST_FAILURE")
        if set(metrics) <= {"rank_ic", "rankic"}:
            reason_codes.append("RANKIC_ONLY_INSUFFICIENT")
        return ExplorerToolResult(
            tool_name="explain_rejection",
            payload={"candidate_id": candidate_id, "reason_codes": tuple(sorted(dict.fromkeys(reason_codes)))},
        )

    def inspect_robustness(self, candidate_id: str) -> ExplorerToolResult:
        row = _visible_candidate(self.reader, candidate_id)
        metrics = dict(row.get("metrics", {}))
        findings = []
        if metrics.get("stability", 0.0) < 0.5:
            findings.append("LOW_STABILITY")
        if metrics.get("incrementality", 0.0) <= 0.0:
            findings.append("NO_INCREMENTALITY")
        return ExplorerToolResult(
            tool_name="inspect_robustness",
            payload={"candidate_id": candidate_id, "findings": tuple(findings), "split": row.get("split")},
        )

    def inspect_trial_budget(self, family_id: str) -> ExplorerToolResult:
        budget = _clean(self.reader.read_trial_budget(family_id))
        return ExplorerToolResult(tool_name="inspect_trial_budget", payload=budget)

    def preregister_hypothesis(self, payload: Mapping[str, Any]) -> ExplorerToolResult:
        clean = _clean(payload)
        required = {"hypothesis_id", "statement", "falsification_tests"}
        missing = required - set(clean)
        if missing:
            _fail(ExplorerFailureCode.INVALID_EXPERIMENT_DESIGN, "preregistration is incomplete", fields=tuple(sorted(missing)))
        return ExplorerToolResult(tool_name="preregister_hypothesis", payload=clean)

    def design_walk_forward(
        self,
        *,
        plan_id: str,
        hypothesis_id: str,
        horizon: str,
        universe: str,
        decision_time,
        research_depth: str = "STANDARD_RESEARCH",
    ) -> ExperimentDesignPlan:
        official = research_depth != "QUICK_EXPLORATION"
        tests = ("train_validation_walk_forward", "cost_adjusted_metrics")
        if research_depth == "STRICT_VALIDATION":
            tests = (*tests, "sealed_controller_review")
        if research_depth == "QUICK_EXPLORATION":
            tests = ("draft_only_research_brief",)
        return ExperimentDesignPlan(
            plan_id=plan_id,
            hypothesis_id=hypothesis_id,
            horizon=horizon,
            universe=universe,
            decision_time=decision_time,
            planned_tests=tests,
            official_evidence_requested=official,
        )

    def design_placebo_tests(self, candidate_id: str) -> ExplorerToolResult:
        _visible_candidate(self.reader, candidate_id)
        return ExplorerToolResult(
            tool_name="design_placebo_tests",
            payload={"candidate_id": candidate_id, "tests": ("label_shuffle", "sign_flip", "random_factor_control")},
        )

    def design_control_tests(self, candidate_id: str, controls: Sequence[str]) -> ExplorerToolResult:
        _visible_candidate(self.reader, candidate_id)
        return ExplorerToolResult(
            tool_name="design_control_tests",
            payload={"candidate_id": candidate_id, "controls": _as_tuple(controls, "controls")},
        )

    def design_regime_splits(self, candidate_id: str, regimes: Sequence[str]) -> ExplorerToolResult:
        _visible_candidate(self.reader, candidate_id)
        return ExplorerToolResult(
            tool_name="design_regime_splits",
            payload={"candidate_id": candidate_id, "regimes": _as_tuple(regimes, "regimes")},
        )


def query_visible_evidence(reader: VisibleEvidenceReader, candidate_id: str) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).query_visible_evidence(candidate_id)


def compare_candidates(reader: VisibleEvidenceReader, candidate_ids: Sequence[str]) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).compare_candidates(candidate_ids)


def explain_rejection(reader: VisibleEvidenceReader, candidate_id: str) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).explain_rejection(candidate_id)


def inspect_robustness(reader: VisibleEvidenceReader, candidate_id: str) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).inspect_robustness(candidate_id)


def inspect_trial_budget(reader: VisibleEvidenceReader, family_id: str) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).inspect_trial_budget(family_id)


def preregister_hypothesis(reader: VisibleEvidenceReader, payload: Mapping[str, Any]) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).preregister_hypothesis(payload)


def design_walk_forward(reader: VisibleEvidenceReader, **kwargs: Any) -> ExperimentDesignPlan:
    return VisibleEvidenceToolbox(reader).design_walk_forward(**kwargs)


def design_placebo_tests(reader: VisibleEvidenceReader, candidate_id: str) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).design_placebo_tests(candidate_id)


def design_control_tests(reader: VisibleEvidenceReader, candidate_id: str, controls: Sequence[str]) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).design_control_tests(candidate_id, controls)


def design_regime_splits(reader: VisibleEvidenceReader, candidate_id: str, regimes: Sequence[str]) -> ExplorerToolResult:
    return VisibleEvidenceToolbox(reader).design_regime_splits(candidate_id, regimes)
