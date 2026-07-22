"""Provider boundary for open-ended but structured specialist research turns."""

from __future__ import annotations

import json
import types
import collections.abc
from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Union, get_args, get_origin, get_type_hints

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail
from ..qlib_candidate_translation import build_qlib_candidate_llm_contract
from .models import (
    CandidateFamilyPlan,
    CandidateCritique,
    CombinationAxis,
    CritiqueAction,
    FactorLanguageCapabilityRequest,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    SpecialistCandidateDraft,
    SpecialistResearchRequest,
    SpecialistDraftIssue,
    SpecialistStage,
    SpecialistTurn,
    ToolCallDraft,
    TurnKind,
)
from .profiles import SpecialistAgentProfile
from .tools import RestrictedResearchToolRegistry


@dataclass(frozen=True)
class SpecialistProviderContext:
    request: SpecialistResearchRequest
    profile: SpecialistAgentProfile
    stage: SpecialistStage
    iteration: int
    plan: ResearchExplorationPlan | None
    finding_summaries: tuple[Mapping[str, Any], ...]
    candidates: tuple[SpecialistCandidateDraft, ...]
    preflight_feedback: tuple[Mapping[str, Any], ...]
    tool_definitions: tuple[Mapping[str, Any], ...]


class SpecialistResearchProvider(Protocol):
    def next_turn(self, context: SpecialistProviderContext) -> SpecialistTurn: ...


class StructuredJSONClient(Protocol):
    def complete_json(self, **kwargs: Any) -> Any: ...


class OpenEndedJSONSpecialistProvider:
    """Real-model adapter; the caller injects an already configured client."""

    def __init__(
        self,
        *,
        client: StructuredJSONClient,
        model: str,
        timeout_seconds: float = 120.0,
        max_tokens_per_turn: int = 4096,
        temperature: float = 0.4,
        top_p: float = 0.95,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens_per_turn
        self._temperature = temperature
        self._top_p = top_p

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def provider_id(self) -> str:
        return "configured_openai_compatible"

    def next_turn(self, context: SpecialistProviderContext) -> SpecialistTurn:
        messages = _provider_messages(context)
        try:
            response = self._client.complete_json(
                messages=messages,
                model=self._model,
                timeout_seconds=min(self._timeout_seconds, context.request.budget.timeout_seconds),
                max_tokens=min(self._max_tokens, context.request.budget.max_output_tokens),
                temperature=self._temperature,
                top_p=self._top_p,
                response_format="json_object",
                extra={},
            )
        except ContractValidationError:
            raise
        except Exception as exc:
            fail(FailureCode.ADAPTER_EXECUTION_FAILED, "specialist provider call failed", error=type(exc).__name__)
        payload = getattr(response, "payload", None)
        if not isinstance(payload, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider returned no JSON object")
        return turn_from_mapping(
            payload,
            input_tokens=int(getattr(response, "input_tokens", 0)),
            output_tokens=int(getattr(response, "output_tokens", 0)),
        )


def build_configured_specialist_provider(
    dotenv_path: Path = Path("agent/.env"),
) -> OpenEndedJSONSpecialistProvider:
    """Load the existing narrow AlphaForge config without exposing credentials."""

    from ..alphaforge_llm import AlphaForgeLLMConfigLoader
    from ..real_llm_provider.client import OpenAICompatibleHTTPClient

    configuration = AlphaForgeLLMConfigLoader(dotenv_path=dotenv_path).load()
    if not configuration.enabled or configuration.settings is None or not configuration.runtime_credential:
        fail(FailureCode.ADAPTER_UNAVAILABLE, "configured AlphaForge LLM is unavailable")
    settings = configuration.settings
    client = OpenAICompatibleHTTPClient(
        settings=settings,
        runtime_credential=configuration.runtime_credential,
    )
    return OpenEndedJSONSpecialistProvider(
        client=client,
        model=settings.model,
        timeout_seconds=settings.timeout_seconds,
        temperature=settings.temperature,
        top_p=settings.top_p,
    )


def _provider_messages(context: SpecialistProviderContext) -> tuple[Mapping[str, str], ...]:
    remaining_tool_calls = max(
        0,
        context.request.budget.max_tool_calls - len(context.finding_summaries),
    )
    remaining_iterations = max(0, context.request.budget.max_iterations - context.iteration)
    allow_tool_call = remaining_tool_calls > 0 and remaining_iterations > 1
    allowed_kinds = {
        SpecialistStage.PLANNING: (TurnKind.PLAN.value,),
        SpecialistStage.INVESTIGATING: (
            (TurnKind.TOOL_CALL.value, TurnKind.CANDIDATES.value)
            if allow_tool_call
            else (TurnKind.CANDIDATES.value,)
        ),
        SpecialistStage.GENERATING: (TurnKind.CANDIDATES.value,),
        SpecialistStage.SELF_CRITIQUING: (TurnKind.SELF_CRITIQUE.value,),
        SpecialistStage.REVISING: (TurnKind.REVISED_CANDIDATES.value,),
    }.get(context.stage, ())
    qlib_contract = build_qlib_candidate_llm_contract(
        confirmed_field_whitelist=context.request.available_fields,
        requested_lookback_sessions=context.request.requested_lookback_sessions,
    )
    qlib_capabilities = dict(qlib_contract.capability_summary)
    # Authority-shaped deny-list tokens stay in server policy and must not be
    # echoed into model context, where the shared prompt-leak scanner rejects
    # them even when they are presented as prohibitions.
    qlib_capabilities.pop("forbidden_capabilities", None)
    submission_maximum = context.request.budget.max_submitted_candidates
    submission_minimum = min(
        8 if context.request.research_depth != "QUICK_EXPLORATION" else 3,
        submission_maximum,
    )
    family_maximum = context.request.budget.max_candidate_families
    family_minimum = min(3 if family_maximum >= 3 else 2, family_maximum)
    remaining_candidate_slots = max(0, submission_maximum - len(context.candidates))
    turn_batch_maximum = min(6, remaining_candidate_slots)
    turn_batch_minimum = min(4, turn_batch_maximum)
    system = (
        "你是 AlphaForge 的专业研究 Agent。必须先规划、再调查、再提出候选并自我审查。"
        "规划必须覆盖多个不同研究方向、机制家族和候选家族；不得只给单一路线。"
        "候选阶段要在预算内系统展开窗口、标准化、风险调整、量价确认、中性化和成本控制等有经济含义的变体，"
        "优先达到系统给出的候选数量目标；不得把同一公式仅改名后重复提交。"
        "可以广泛思考经济机制和组合，但只能输出结构化 DRAFT JSON；候选表达式只能使用系统给出的认证 Qlib Expression 语言。"
        "必须使用 $close、Ref、Mean、Std、Sub、Div 等 Qlib 形式；不得输出 pct_change、rolling_mean、"
        "rank_cross_section、zscore_cross_section、multiply、subtract 等旧版小写函数形式。"
        "禁止任何可执行代码、命令行、数据库语句、动态求值、任意文件读取或外部程序调用。"
        "不得声称或填写官方指标、独立晋级、风险审批、资产配置、交易指令、成交或会计记录。"
        "不得请求、推断或复述独立封闭验证内容；每个候选必须说明机制、时间语义、反证、成本与暴露风险。"
        "不支持的字段或算子只能作为 capability_requests 提出，不得输出实现代码。"
    )
    material = {
        "stage": context.stage.value,
        "allowed_turn_kinds": allowed_kinds,
        "question": context.request.question,
        "universe": context.request.universe,
        "label_horizon_sessions": context.request.label_horizon_sessions,
        "requested_lookback_sessions": context.request.requested_lookback_sessions,
        "available_fields": context.request.available_fields,
        "factor_language": {
            "contract_hash": qlib_contract.contract_hash,
            "manifest_hash": qlib_contract.manifest_hash,
            "capabilities": qlib_capabilities,
            "grammar": (
                "Use Qlib field references such as $close and only the listed operators. Infix arithmetic and "
                "comparisons are allowed only as certified expression nodes. General-purpose scripts, imports, lambdas, subscripts, "
                "attributes, custom operators and any dynamic runtime path are forbidden."
            ),
            "valid_examples": (
                "($close/Ref($close,50))-1",
                "1-($close/Ref($close,50))",
                "Div((($close/Ref($close,50))-1),Std($close,20))",
                "Mul((($close/Ref($close,50))-1),Div($volume,Mean($volume,20)))",
                "Corr($close,$volume,20)",
                "If(Mean($close,20)>Mean($close,50),1,0)",
            ),
            "revision_rule": (
                "Every revision needs a new unique candidate_id, parent_candidate_id equal to an existing "
                "candidate_id, and revision_number matching the requested revision."
            ),
        },
        "remaining_limits": {
            "iterations": context.request.budget.max_iterations - context.iteration,
            "ideas": context.request.budget.max_ideas,
            "candidate_families": context.request.budget.max_candidate_families,
            "combinations": context.request.budget.max_combinations,
            "submitted_candidates": context.request.budget.max_submitted_candidates,
            "tool_calls": remaining_tool_calls,
            "total_tokens": context.request.budget.max_tokens,
            "revisions": context.request.budget.max_revisions,
            "family_trial_budget": context.request.budget.family_trial_budget,
        },
        "candidate_submission_target": {
            "minimum": submission_minimum,
            "maximum": submission_maximum,
            "family_minimum": family_minimum,
            "family_maximum": family_maximum,
            "available_candidate_families": context.profile.candidate_families,
            "already_submitted": len(context.candidates),
            "remaining_slots": remaining_candidate_slots,
            "this_turn_batch_minimum": turn_batch_minimum,
            "this_turn_batch_maximum": turn_batch_maximum,
            "instruction": (
                f"The bounded run targets {submission_minimum}-{submission_maximum} genuinely differentiated candidates "
                f"across {family_minimum}-{family_maximum} relevant mechanism families. In this turn submit only "
                f"{turn_batch_minimum}-{turn_batch_maximum} new candidates so the response remains complete and valid. "
                "Cover the requested lookback exactly in multiple candidates, then add justified neighbouring "
                "windows or transformations as robustness comparisons."
            ),
        },
        "specialist_protocol_contract": {
            "fundamental": (
                "Every candidate availability_assumptions array must contain the exact token available_at "
                "and state that the financial record is available no later than decision_time. A price-only "
                "candidate must explicitly say cross-domain or price proxy in its mechanism or novelty explanation."
            ),
            "expectations_events": (
                "Every candidate availability_assumptions array must contain the exact tokens event_time, "
                "published_at, available_at, and first_tradable_time."
            ),
            "macro_regime": (
                "Regime definitions must be knowable at decision_time and must not use ex-post bull/bear labels."
            ),
            "alternative_data": (
                "If provenance, publication time, coverage, or entity mapping is unavailable, report a capability "
                "request instead of inventing the missing dataset."
            ),
            "meta_alpha": (
                "Use visible train/validation summaries only; do not reference hidden evaluation or private rankings."
            ),
            "market_structure": (
                "State turnover, liquidity, and cost risks for each candidate."
            ),
        }[context.profile.specialist_id],
        "requested_lookback_contract": (
            "No exact lookback was requested; choose bounded windows justified by the mechanism."
            if context.request.requested_lookback_sessions is None
            else f"At least one submitted expression must explicitly represent the requested {context.request.requested_lookback_sessions}-session lookback."
        ),
        "specialist": {
            "id": context.profile.specialist_id,
            "mission": context.profile.mission,
            "research_lenses": context.profile.research_lenses,
            "failure_lenses": context.profile.failure_lenses,
        },
        "tools": context.tool_definitions,
        "plan": context.plan.to_dict() if context.plan else None,
        "visible_findings": context.finding_summaries,
        "draft_candidates": tuple(item.to_dict() for item in context.candidates),
        "preflight_feedback": context.preflight_feedback,
        "schema_note": (
            "PLAN requires plan; TOOL_CALL requires tool_call; CANDIDATES/REVISED_CANDIDATES "
            "require candidates; SELF_CRITIQUE requires critiques. Include kind as the enum string. "
            "Return exactly one JSON object with only the keys shown for the selected alternative; "
            "do not add analysis, reasoning, summary, candidate_shape, or schema-description keys."
        ),
        "required_output_schema": stage_json_schema(
            context.stage,
            allow_tool_call=allow_tool_call,
        ),
    }
    return (
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(_json_material(material), ensure_ascii=False, sort_keys=True)},
    )


def _json_material(value: Any) -> Any:
    """Thaw read-only mappings for provider JSON without changing source objects."""

    if isinstance(value, Mapping):
        return {
            str(key): _json_material(item)
            for key, item in value.items()
            if str(key) not in {"contract_type", "schema_version"}
        }
    if isinstance(value, (tuple, list)):
        return [_json_material(item) for item in value]
    return value


def contract_json_schema(contract_type: type[Any]) -> Mapping[str, Any]:
    """Project one dataclass contract into a deterministic provider JSON Schema."""

    if not is_dataclass(contract_type):
        fail(FailureCode.INVALID_VALUE, "schema source must be a dataclass contract")
    hints = get_type_hints(contract_type)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in fields(contract_type):
        properties[item.name] = _annotation_schema(hints.get(item.name, Any))
        if item.default is MISSING and item.default_factory is MISSING:
            required.append(item.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _annotation_schema(annotation: Any) -> Mapping[str, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_annotation_schema(item) for item in args]}
    if annotation is type(None):
        return {"type": "null"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [item.value for item in annotation]}
    if origin is tuple:
        item_type = args[0] if args else Any
        return {"type": "array", "items": _annotation_schema(item_type)}
    if origin in (dict, Mapping, collections.abc.Mapping):
        return {"type": "object"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is Any:
        return {}
    if isinstance(annotation, type) and is_dataclass(annotation):
        return contract_json_schema(annotation)
    return {}


def _turn_contract_schema(kind: TurnKind, field_name: str, contract: type[Any], *, array: bool = False) -> Mapping[str, Any]:
    value_schema: Mapping[str, Any] = contract_json_schema(contract)
    if array:
        value_schema = {"type": "array", "minItems": 1, "items": value_schema}
    properties: dict[str, Any] = {
        "kind": {"const": kind.value},
        field_name: value_schema,
    }
    if kind in {TurnKind.CANDIDATES, TurnKind.REVISED_CANDIDATES}:
        properties["capability_requests"] = {
            "type": "array",
            "items": contract_json_schema(FactorLanguageCapabilityRequest),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["kind", field_name],
        "additionalProperties": False,
    }


def stage_json_schema(
    stage: SpecialistStage,
    *,
    allow_tool_call: bool = True,
) -> Mapping[str, Any]:
    if stage is SpecialistStage.PLANNING:
        return _turn_contract_schema(TurnKind.PLAN, "plan", ResearchExplorationPlan)
    if stage is SpecialistStage.INVESTIGATING:
        candidates = _turn_contract_schema(TurnKind.CANDIDATES, "candidates", SpecialistCandidateDraft, array=True)
        if not allow_tool_call:
            return candidates
        return {"oneOf": [_turn_contract_schema(TurnKind.TOOL_CALL, "tool_call", ToolCallDraft), candidates]}
    if stage in {SpecialistStage.GENERATING, SpecialistStage.REVISING}:
        kind = TurnKind.REVISED_CANDIDATES if stage is SpecialistStage.REVISING else TurnKind.CANDIDATES
        return _turn_contract_schema(kind, "candidates", SpecialistCandidateDraft, array=True)
    return _turn_contract_schema(TurnKind.SELF_CRITIQUE, "critiques", CandidateCritique, array=True)


def _stage_output_shape(stage: SpecialistStage, *, allow_tool_call: bool = True) -> Mapping[str, Any]:
    """Backward-compatible alias; generated from contracts rather than handwritten."""

    return stage_json_schema(stage, allow_tool_call=allow_tool_call)


def turn_from_mapping(payload: Mapping[str, Any], *, input_tokens: int = 0, output_tokens: int = 0) -> SpecialistTurn:
    allowed = {
        "kind", "plan", "tool_call", "candidates", "critiques", "capability_requests",
        "research_question",
    }
    unknown = set(payload) - allowed
    if unknown:
        fail(FailureCode.UNKNOWN_FIELD, "provider turn contains unknown fields", fields=tuple(sorted(unknown)))
    try:
        kind = TurnKind(payload.get("kind"))
    except (TypeError, ValueError):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider turn kind is invalid")
    tool_call_value = payload.get("tool_call")
    redundant_question = payload.get("research_question")
    if redundant_question is not None:
        if kind is not TurnKind.TOOL_CALL:
            fail(FailureCode.UNKNOWN_FIELD, "research_question is only valid inside a tool_call")
        if not isinstance(redundant_question, str) or not redundant_question.strip():
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research_question must be a non-empty string")
        raw_tool_call = _mapping(tool_call_value, "tool_call")
        nested_question = raw_tool_call.get("research_question")
        if nested_question is not None and nested_question != redundant_question:
            fail(FailureCode.INVALID_VALUE, "duplicate research_question values do not match")
        if nested_question is None:
            tool_call_value = {**raw_tool_call, "research_question": redundant_question}
    plan = _plan(payload["plan"]) if payload.get("plan") is not None else None
    tool_call = _tool_call(tool_call_value) if tool_call_value is not None else None
    raw_candidates = payload.get("candidates", ())
    candidates, candidate_issues, candidate_errors = _parse_items(
        raw_candidates,
        item_kind="candidate",
        parser=_candidate,
    )
    if raw_candidates and not candidates and candidate_errors:
        raise candidate_errors[0]
    critiques = tuple(_critique(item) for item in payload.get("critiques", ()))
    capabilities, capability_issues, _ = _parse_items(
        payload.get("capability_requests", ()),
        item_kind="capability_request",
        parser=_capability,
    )
    return SpecialistTurn(
        kind=kind,
        plan=plan,
        tool_call=tool_call,
        candidates=candidates,
        critiques=critiques,
        capability_requests=capabilities,
        draft_issues=candidate_issues + capability_issues,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_items(
    values: Any,
    *,
    item_kind: str,
    parser: Any,
) -> tuple[tuple[Any, ...], tuple[SpecialistDraftIssue, ...], tuple[ContractValidationError, ...]]:
    if not isinstance(values, (tuple, list)):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{item_kind}s must be an array")
    accepted: list[Any] = []
    issues: list[SpecialistDraftIssue] = []
    errors: list[ContractValidationError] = []
    for index, value in enumerate(values):
        try:
            accepted.append(parser(value))
        except ContractValidationError as exc:
            errors.append(exc)
            issues.append(
                SpecialistDraftIssue(
                    item_kind=item_kind,
                    item_index=index,
                    failure_code=exc.code.value,
                    message=f"{item_kind} item was rejected at the provider boundary",
                )
            )
    return tuple(accepted), tuple(issues), tuple(errors)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{name} must be an object")
    return value


def _plan(value: Any) -> ResearchExplorationPlan:
    raw = _mapping(value, "plan")
    _reject_unknown(
        raw,
        {
            "specialist_id", "scope_summary", "directions", "investigation_questions",
            "stop_conditions", "mechanism_ideas", "candidate_family_plans",
        },
        "plan",
    )
    return ResearchExplorationPlan(
        specialist_id=str(raw.get("specialist_id", "")),
        scope_summary=str(raw.get("scope_summary", "")),
        directions=tuple(
            _direction(item)
            for item in (_mapping(item, "direction") for item in raw.get("directions", ()))
        ),
        investigation_questions=tuple(raw.get("investigation_questions", ())),
        stop_conditions=tuple(raw.get("stop_conditions", ())),
        mechanism_ideas=tuple(_mechanism_idea(_mapping(item, "mechanism idea")) for item in raw.get("mechanism_ideas", ())),
        candidate_family_plans=tuple(_family_plan(_mapping(item, "candidate family plan")) for item in raw.get("candidate_family_plans", ())),
    )


def _tool_call(value: Any) -> ToolCallDraft:
    raw = _mapping(value, "tool_call")
    _reject_unknown(raw, {"tool_name", "arguments", "research_question"}, "tool_call")
    return ToolCallDraft(
        tool_name=str(raw.get("tool_name", "")),
        arguments=_mapping(raw.get("arguments", {}), "tool arguments"),
        research_question=str(raw.get("research_question", "")),
    )


def _candidate(value: Any) -> SpecialistCandidateDraft:
    raw = _mapping(value, "candidate")
    _reject_unknown(
        raw,
        {
            "candidate_id", "family_id", "hypothesis", "economic_mechanism",
            "expected_direction", "forecast_horizon_sessions", "lookback_sessions",
            "required_fields", "expression", "availability_assumptions", "neutralization",
            "falsification_tests", "cost_risks", "exposure_risks", "novelty_explanation",
            "parent_candidate_id", "revision_number",
        },
        "candidate",
    )
    return SpecialistCandidateDraft(
        candidate_id=str(raw.get("candidate_id", "")),
        family_id=str(raw.get("family_id", "")),
        hypothesis=str(raw.get("hypothesis", "")),
        economic_mechanism=str(raw.get("economic_mechanism", "")),
        expected_direction=str(raw.get("expected_direction", "")),
        forecast_horizon_sessions=int(raw.get("forecast_horizon_sessions", 0)),
        lookback_sessions=int(raw.get("lookback_sessions", 0)),
        required_fields=tuple(raw.get("required_fields", ())),
        expression=str(raw.get("expression", "")),
        availability_assumptions=tuple(raw.get("availability_assumptions", ())),
        neutralization=tuple(raw.get("neutralization", ())),
        falsification_tests=tuple(raw.get("falsification_tests", ())),
        cost_risks=tuple(raw.get("cost_risks", ())),
        exposure_risks=tuple(raw.get("exposure_risks", ())),
        novelty_explanation=str(raw.get("novelty_explanation", "")),
        parent_candidate_id=raw.get("parent_candidate_id"),
        revision_number=int(raw.get("revision_number", 0)),
    )


def _critique(value: Any) -> CandidateCritique:
    raw = _mapping(value, "critique")
    _reject_unknown(raw, {"candidate_id", "action", "weaknesses", "rationale"}, "critique")
    return CandidateCritique(
        candidate_id=str(raw.get("candidate_id", "")),
        action=CritiqueAction(raw.get("action")),
        weaknesses=tuple(raw.get("weaknesses", ())),
        rationale=str(raw.get("rationale", "")),
    )


def _capability(value: Any) -> FactorLanguageCapabilityRequest:
    raw = _mapping(value, "capability_request")
    _reject_unknown(
        raw,
        {"request_id", "request_type", "requested_name", "economic_reason", "deterministic_semantics", "lookahead_risk", "example_expression"},
        "capability_request",
    )
    return FactorLanguageCapabilityRequest(
        request_id=str(raw.get("request_id", "")),
        request_type=str(raw.get("request_type", "")),
        requested_name=str(raw.get("requested_name", "")),
        economic_reason=str(raw.get("economic_reason", "")),
        deterministic_semantics=str(raw.get("deterministic_semantics", "")),
        lookahead_risk=str(raw.get("lookahead_risk", "")),
        example_expression=str(raw.get("example_expression", "")),
    )


def _direction(raw: Mapping[str, Any]) -> ResearchDirectionIdea:
    _reject_unknown(raw, {"direction_id", "mechanism", "differentiation", "required_fields", "falsification"}, "direction")
    return ResearchDirectionIdea(
        direction_id=str(raw.get("direction_id", "")),
        mechanism=str(raw.get("mechanism", "")),
        differentiation=str(raw.get("differentiation", "")),
        required_fields=tuple(raw.get("required_fields", ())),
        falsification=str(raw.get("falsification", "")),
    )


def _mechanism_idea(raw: Mapping[str, Any]) -> ResearchMechanismIdea:
    _reject_unknown(
        raw,
        {"mechanism_id", "family_id", "mechanism", "observable_proxy", "predicted_direction", "required_fields", "falsification"},
        "mechanism idea",
    )
    return ResearchMechanismIdea(
        mechanism_id=str(raw.get("mechanism_id", "")),
        family_id=str(raw.get("family_id", "")),
        mechanism=str(raw.get("mechanism", "")),
        observable_proxy=str(raw.get("observable_proxy", "")),
        predicted_direction=str(raw.get("predicted_direction", "")),
        required_fields=tuple(raw.get("required_fields", ())),
        falsification=str(raw.get("falsification", "")),
    )


def _family_plan(raw: Mapping[str, Any]) -> CandidateFamilyPlan:
    _reject_unknown(raw, {"family_id", "mechanism_ids", "combination_axes", "max_combinations"}, "candidate family plan")
    axes = []
    for item in raw.get("combination_axes", ()):
        axis = _mapping(item, "combination axis")
        _reject_unknown(axis, {"axis_id", "values", "rationale"}, "combination axis")
        axes.append(
            CombinationAxis(
                axis_id=str(axis.get("axis_id", "")),
                values=tuple(axis.get("values", ())),
                rationale=str(axis.get("rationale", "")),
            )
        )
    return CandidateFamilyPlan(
        family_id=str(raw.get("family_id", "")),
        mechanism_ids=tuple(raw.get("mechanism_ids", ())),
        combination_axes=tuple(axes),
        max_combinations=int(raw.get("max_combinations", 0)),
    )


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        fail(FailureCode.UNKNOWN_FIELD, f"{name} contains unknown fields", fields=tuple(sorted(unknown)))


class ScriptedSpecialistProvider:
    """Deterministic fake used by offline tests; not a production idea generator."""

    def __init__(self, turns: tuple[SpecialistTurn, ...]) -> None:
        self._turns = list(turns)
        self.calls = 0

    def next_turn(self, context: SpecialistProviderContext) -> SpecialistTurn:
        del context
        self.calls += 1
        if not self._turns:
            fail(FailureCode.INCOMPLETE_RESULT, "scripted provider has no remaining turn")
        return self._turns.pop(0)
