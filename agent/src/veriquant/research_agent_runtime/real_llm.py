"""Safe OpenAI-compatible ``ChatLLM`` adapter for research DRAFTs.

The production client preserves Vibe's bounded multi-turn message/tool cycle
and accepts tool calls only from the task's exact read-only whitelist.  The
legacy JSON-only call remains solely as a compatibility path for deterministic
fakes.  Returned source is inert text and is never evaluated here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.providers.chat import LLMResponse, ToolCallRequest

from ..alphaforge_llm import AlphaForgeLLMConfigLoader, AlphaForgeLLMConfiguration
from ..contracts import FailureCode
from ..contracts.base import fail
from ..real_llm_provider.client import OpenAICompatibleHTTPClient, ProviderFailure
from .contracts import ResearchAgentTask


class SafeOpenAIResearchChatLLM:
    """Adapt the approved JSON client to Vibe's ``stream_chat`` interface."""

    def __init__(self, *, task: ResearchAgentTask, configuration: AlphaForgeLLMConfiguration, client: Any | None = None) -> None:
        if not configuration.enabled or configuration.settings is None or not configuration.runtime_credential:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "configured research provider is unavailable")
        self._task = task
        self._configuration = configuration
        self._settings = configuration.settings
        self._client = client or OpenAICompatibleHTTPClient(
            settings=self._settings,
            runtime_credential=configuration.runtime_credential,
        )
        self.model_name = self._settings.model

    def __repr__(self) -> str:
        return f"SafeOpenAIResearchChatLLM(model={self.model_name!r})"

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        timeout: int | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> LLMResponse:
        del on_text_chunk, on_reasoning_chunk
        if should_cancel is not None and should_cancel():
            fail(FailureCode.ADAPTER_EXECUTION_FAILED, "research provider call was cancelled")
        _validate_tool_names(tools or (), self._task.allowed_tools)
        settings = self._settings
        complete_chat = getattr(self._client, "complete_chat", None)
        if callable(complete_chat):
            try:
                response = complete_chat(
                    messages=tuple(dict(item) for item in messages),
                    tools=tuple(dict(item) for item in (tools or ())),
                    model=settings.model,
                    timeout_seconds=min(float(timeout or settings.timeout_seconds), float(settings.timeout_seconds)),
                    max_tokens=settings.max_tokens,
                    temperature=settings.temperature,
                    top_p=settings.top_p,
                    response_format=settings.response_format,
                    extra={},
                    response_safety_profile="RESEARCH_SOURCE_DRAFT",
                )
            except ProviderFailure as exc:
                # Some OpenAI-compatible relays advertise tool calling but do
                # not honor JSON response_format on a tool-aware final turn.
                # Retry once through the same client's JSON-only DRAFT surface;
                # this grants no tools or authority and still crosses the AST
                # review gateway. Transport/time/auth failures are not retried
                # through this compatibility path.
                if exc.code is not FailureCode.PROVIDER_SCHEMA_REJECTED:
                    raise
                response = self._client.complete_json(
                    messages=_safe_messages(self._task),
                    model=settings.model,
                    timeout_seconds=min(float(timeout or settings.timeout_seconds), float(settings.timeout_seconds)),
                    max_tokens=settings.max_tokens,
                    temperature=settings.temperature,
                    top_p=settings.top_p,
                    response_format=settings.response_format,
                    extra={},
                    response_safety_profile="RESEARCH_SOURCE_DRAFT",
                )
                draft_payload = response.payload
                content = json.dumps(
                    _normalize_draft_payload(draft_payload),
                    ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
                )
                return LLMResponse(
                    content=content,
                    finish_reason="stop",
                    usage_metadata={
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "total_tokens": response.input_tokens + response.output_tokens,
                    },
                )
            if response.tool_calls:
                return LLMResponse(
                    content=response.content or "",
                    tool_calls=[
                        ToolCallRequest(
                            id=item.call_id,
                            name=item.name,
                            arguments=dict(item.arguments),
                        )
                        for item in response.tool_calls
                    ],
                    finish_reason="tool_calls",
                    usage_metadata={
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "total_tokens": response.input_tokens + response.output_tokens,
                    },
                )
            if response.payload is None:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research provider omitted the final draft payload")
            draft_payload = response.payload
        else:
            # Compatibility path for deterministic fakes and previously frozen
            # provider adapters.  The production HTTP client implements
            # ``complete_chat`` and therefore preserves the real AgentLoop
            # message/tool cycle.
            response = self._client.complete_json(
                messages=_safe_messages(self._task),
                model=settings.model,
                timeout_seconds=min(float(timeout or settings.timeout_seconds), float(settings.timeout_seconds)),
                max_tokens=settings.max_tokens,
                temperature=settings.temperature,
                top_p=settings.top_p,
                response_format=settings.response_format,
                extra={},
                response_safety_profile="RESEARCH_SOURCE_DRAFT",
            )
            draft_payload = response.payload
        content = json.dumps(
            _normalize_draft_payload(draft_payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return LLMResponse(
            content=content,
            finish_reason="stop",
            usage_metadata={
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "total_tokens": response.input_tokens + response.output_tokens,
            },
        )


_PLAN_KEYS = ("plan", "research_plan", "methodology", "research_method", "approach")
_SOURCE_KEYS = (
    "code_drafts", "code_draft", "code", "source", "source_code",
    "python_code", "research_code", "experiment_code",
)


def _normalize_draft_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply machine-owned downgrade and preserve only the draft contract.

    The upstream client has already rejected privileged fields and unsafe
    source text.  The model's own authority/status claims are never trusted.
    """

    unknown = set(payload) - {
        "authority", "status", "data_gaps", "proxy_specs", *_PLAN_KEYS, *_SOURCE_KEYS,
    }
    privileged_tokens = {
        "authorization", "api_key", "secret", "sealed", "official_metrics",
        "promotion", "challenger", "portfolio", "target_weights", "risk_approval",
        "orders", "broker", "ledger",
    }
    if any(str(key).casefold().replace("-", "_") in privileged_tokens for key in unknown):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research provider returned a privileged draft field")
    # Models sometimes add harmless explanation/method fields despite the JSON
    # contract. They are never persisted or trusted; keep only the allowlisted
    # plan and source drafts rather than failing an otherwise reviewable run.
    plan = _one_text_alias(payload, _PLAN_KEYS, name="plan")
    code_drafts = _source_alias(payload)
    if not isinstance(plan, str) or not plan.strip():
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research provider omitted the plan")
    if re.search(
        r"(?i)(?:authorization|bearer\s+\S+|api[_ -]?key|private[_ -]?key|sealed|"
        r"official[_ -]?metrics?|promotion|target[_ -]?weights?|risk[_ -]?approval|broker|ledger)",
        plan,
    ):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research plan contains privileged or secret material")
    if not isinstance(code_drafts, (list, tuple)) or not code_drafts:
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research provider omitted source drafts")
    if any(not isinstance(source, str) or "def run_research(" not in source for source in code_drafts):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research source draft omitted the required entrypoint")
    data_gaps = payload.get("data_gaps", [])
    proxy_specs = payload.get("proxy_specs", [])
    if not isinstance(data_gaps, list) or not isinstance(proxy_specs, list):
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research gap and proxy fields must be arrays")
    return {
        "authority": "DRAFT_ONLY",
        "plan": plan.strip(),
        "code_drafts": list(code_drafts),
        "data_gaps": data_gaps,
        "proxy_specs": proxy_specs,
        "status": "COMPLETED",
    }


def _one_text_alias(payload: Mapping[str, Any], keys: tuple[str, ...], *, name: str) -> str | None:
    values = [payload[key].strip() for key in keys if isinstance(payload.get(key), str) and payload[key].strip()]
    if not values:
        return None
    if len(set(values)) != 1:
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"research provider returned ambiguous {name} aliases")
    return values[0]


def _source_alias(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
    normalized: list[tuple[str, ...]] = []
    for key in _SOURCE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            normalized.append((value.strip(),))
        elif isinstance(value, (list, tuple)) and value and all(isinstance(item, str) and item.strip() for item in value):
            normalized.append(tuple(item.strip() for item in value))
    if not normalized:
        return None
    identities = {json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in normalized}
    if len(identities) != 1:
        fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research provider returned ambiguous source aliases")
    return normalized[0]


class ConfiguredResearchLLMFactory:
    """Load only the narrow AlphaForge settings and construct task-bound LLMs."""

    def __init__(
        self, *, dotenv_path: Path = Path("agent/.env"),
        client_factory: Callable[[AlphaForgeLLMConfiguration], Any] | None = None,
        minimum_max_tokens: int | None = None,
    ) -> None:
        configuration = AlphaForgeLLMConfigLoader(dotenv_path=dotenv_path).load()
        if minimum_max_tokens is not None:
            if isinstance(minimum_max_tokens, bool) or not 1 <= minimum_max_tokens <= 16_384:
                raise ValueError("minimum_max_tokens must be 1..16384")
            if configuration.settings is not None and configuration.settings.max_tokens < minimum_max_tokens:
                configuration = replace(
                    configuration,
                    settings=replace(configuration.settings, max_tokens=minimum_max_tokens),
                )
        self._configuration = configuration
        self._client_factory = client_factory

    @property
    def public_view(self) -> Mapping[str, object]:
        return self._configuration.public_view()

    def __call__(self, task: ResearchAgentTask) -> SafeOpenAIResearchChatLLM:
        client = self._client_factory(self._configuration) if self._client_factory is not None else None
        return SafeOpenAIResearchChatLLM(task=task, configuration=self._configuration, client=client)


def _validate_tool_names(tools: Sequence[Mapping[str, Any]], allowed: tuple[str, ...]) -> None:
    names: list[str] = []
    for definition in tools:
        function = definition.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "research tool definition is invalid")
        names.append(str(function["name"]))
    # Vibe AgentLoop deliberately removes every tool on its final iteration to
    # force a plain-text/DRAFT answer. Zero capabilities is a safe reduction;
    # any non-empty partial or expanded set remains an authority violation.
    if not names:
        return
    if tuple(sorted(names)) != allowed:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider tool definitions differ from task whitelist")


def _safe_messages(task: ResearchAgentTask) -> tuple[Mapping[str, str], ...]:
    # Keep this prompt compatible with the existing provider's strict content
    # scanner.  Privileged capability names are intentionally absent.
    contract_instruction, safe_example = _output_contract_prompt(task.output_kind)
    system = (
        "You are an AlphaForge specialist. Return exactly one JSON object and no prose. "
        "The top-level keys must be exactly authority, plan, code_drafts, status; code_drafts is an array of source strings. "
        "Your authority is DRAFT_ONLY. Produce an inert research source draft for later independent review. "
        "The plan is also the customer-facing candidate definition. For every draft it must state a specific "
        "candidate name, the exact visible input fields, the transformation and calculation order, score or "
        "effect direction, observation window, forecast horizon, normalization or neutralization, economic "
        "rationale, and any proxy limitation. For ScoreTable, explain in plain Chinese what a higher score means. "
        "For a regime, regression, event, diagnostic, or dataset output, explicitly say that it is an analysis "
        "rather than a second Alpha signal. A generic statement such as 'construct a score' is incomplete. "
        "For a cross-sectional score, do not add a market-wide variable such as Shibor as a common standalone "
        "term because it cannot change same-day stock ranks; use it as a regime condition or an interaction with "
        "a stock-specific feature. Avoid double-counting closely related volume or flow proxies unless the plan "
        "justifies the distinction, and explain why any non-estimated weights are economically reasonable. "
        f"Submit {task.minimum_experiments} to {task.max_experiments} genuinely distinct experiment drafts. "
        "When comparison is required, vary the economic definition or test rather than only renaming a formula. "
        "Together they must address these answer semantics: "
        f"{', '.join(task.required_answer_semantics) or 'primary_conclusion'}. "
        "Each source must define exactly one synchronous function named run_research with signature "
        "run_research(train_rows, prediction_rows, seed). Do not load modules, access files, contact a network, "
        "or use dynamic evaluation. Use only the supplied table objects and deterministic operations. "
        f"{contract_instruction} The runner, not this "
        "source, owns contract_type, schema_version, experiment identity, code identity, data identity, and "
        "authority fields. Sort rows by decision_time then symbol. Prediction rows never contain future labels. "
        "Exact input schema: each train row is "
        "{symbol, decision_time, source_available_at, outcome_available_at, target_value, features:{...}}; "
        "each prediction row is flat {symbol, decision_time, source_available_at, <visible feature fields>}. "
        "Read train features as item['features']['field'] and prediction features as item['field']. "
        "Define one feature accessor and use it for every feature: when 'features' in item read "
        "item['features'][field], otherwise read item[field]. Do not bypass it in lambdas or special cases. "
        "The user content contains the exhaustive visible-field list. The source may reference only those exact "
        "feature names. Never invent a convenient field name. If the question names an unavailable concept, "
        "derive a defensible proxy from visible price, volume, benchmark, or registered-baseline fields and explain "
        "that proxy in plan/comments. "
        "A train row has no feature names at its top level: item['close'] or item['pct_change'] on train_rows is invalid. "
        "Visible numeric fields can be null. Handle missing values deterministically, never call float on null, "
        "and do not drop prediction rows because the output must preserve the exact prediction identity set. "
        "Use bracket indexing rather than a method named get. In the plan and source comments, avoid discussing "
        "platform permissions or operational infrastructure; return only the research method and inert source."
    )
    material = {
        "question": task.question,
        "specialist_id": task.specialist_id,
        "decision_time": task.decision_time.isoformat(),
        "available_read_only_views": task.allowed_tools,
        "output_contract": {
            "authority": "DRAFT_ONLY",
            "output_kind": task.output_kind,
            "plan": (
                "complete Chinese candidate definition: name, exact fields, transformations, direction, "
                "observation window, forecast horizon, normalization, rationale, and proxy limitations"
            ),
            "answer_requirement_ids": task.answer_requirement_ids,
            "required_answer_semantics": task.required_answer_semantics,
            "comparison_required": task.comparison_required,
            "code_drafts": [
                f"{task.minimum_experiments} to {task.max_experiments} complete run_research source strings"
            ],
            "status": "COMPLETED",
        },
        "safe_source_example": safe_example,
    }
    return (system and {"role": "system", "content": system}, {"role": "user", "content": json.dumps(material, ensure_ascii=False, sort_keys=True)})


def _output_contract_prompt(output_kind: str) -> tuple[str, str]:
    header = "def run_research(train_rows, prediction_rows, seed):\n"
    if output_kind == "ScoreTable":
        return (
            "Return exactly {'rows': [...]} for a ScoreTable body. Each row contains only symbol, "
            "decision_time, source_available_at, score, and label_horizon_sessions.",
            header
            + "    baseline = sum(float(x['features']['close']) for x in train_rows) / len(train_rows)\n"
            + "    rows = [{'symbol': x['symbol'], 'decision_time': x['decision_time'], "
            + "'source_available_at': x['source_available_at'], 'score': float(x['close']) - baseline, "
            + "'label_horizon_sessions': 20} for x in prediction_rows]\n"
            + "    rows.sort(key=lambda row: (row['decision_time'], row['symbol']))\n    return {'rows': rows}",
        )
    if output_kind == "RegressionTable":
        return (
            "Return exactly {'specification_id': 'research_spec', 'rows': [...]} for a RegressionTable body. "
            "Each row contains only term, estimate, std_error, t_statistic, p_value, and observation_count. "
            "The rows array must never be empty. Standardize materially different feature scales, remove "
            "zero-variance or exactly collinear columns, and use a small deterministic ridge fallback when an "
            "unregularized system is singular. If an effect still cannot be estimated, return a conservative "
            "non-empty row with finite estimate 0, std_error 1, t_statistic 0, p_value 1, and the actual "
            "observation_count; never signal failure with an empty table. Before returning, mechanically check "
            "every estimate, std_error, t_statistic, and p_value with math.isfinite. Replace any non-finite "
            "statistic with the same conservative finite fallback and clamp p_value to the inclusive [0, 1] "
            "interval. Do not let NaN, infinity, overflow, or a numerical exception escape the function. Use "
            "distinct descriptive names for matrix dimensions, loop indexes, coefficients, and p-values. Never "
            "reuse or rebind an integer row_count or column_count variable as a floating p_value or statistic.",
            header
            + "    n = len(train_rows)\n"
            + "    rows = [{'term': 'signal', 'estimate': 0.0, 'std_error': 1.0, "
            + "'t_statistic': 0.0, 'p_value': 1.0, 'observation_count': n}]\n"
            + "    return {'specification_id': 'research_spec', 'rows': rows}",
        )
    if output_kind == "RegimeComparisonTable":
        return (
            "Return exactly {'rows': [...]} for a RegimeComparisonTable body. Each row contains only "
            "regime_id, metric_name, estimate, and sample_count. Return at least two non-overlapping "
            "states for every metric and use only decision-time-visible state definitions.",
            header
            + "    midpoint = max(1, len(train_rows) // 2)\n"
            + "    return {'rows': ["
            + "{'regime_id': 'state_low', 'metric_name': 'visible_effect', 'estimate': 0.0, 'sample_count': midpoint}, "
            + "{'regime_id': 'state_high', 'metric_name': 'visible_effect', 'estimate': 0.0, 'sample_count': max(1, len(train_rows) - midpoint)}]}",
        )
    if output_kind == "EventTable":
        return (
            "Return exactly {'rows': [...]} for an EventTable body. Each row contains only "
            "event_id, symbol, event_type, event_time, published_at, available_at, decision_time, "
            "and signal. Publication and availability must not be after decision_time. Use only "
            "visible frozen fields and return a non-empty set of genuinely selected events.",
            header
            + "    rows = []\n"
            + "    for index, item in enumerate(prediction_rows):\n"
            + "        rows.append({'event_id': 'event_' + str(index), 'symbol': item['symbol'], "
            + "'event_type': 'visible_signal', 'event_time': item['source_available_at'], "
            + "'published_at': item['source_available_at'], 'available_at': item['source_available_at'], "
            + "'decision_time': item['decision_time'], 'signal': 0.0})\n"
            + "    return {'rows': rows}",
        )
    if output_kind == "DiagnosticTable":
        return (
            "Return exactly {'rows': [...]} for a DiagnosticTable body. Each row contains only "
            "diagnostic_id, metric_name, value, unit, and interpretation.",
            header + "    return {'rows': [{'diagnostic_id': 'baseline', 'metric_name': 'coverage', "
            + "'value': float(len(prediction_rows)), 'unit': 'rows', 'interpretation': 'visible sample'}]}",
        )
    if output_kind == "DatasetAssessmentTable":
        return (
            "Return exactly {'rows': [...]} for a DatasetAssessmentTable body. Each row contains only "
            "field_name, source_id, coverage, pit_eligible, observed_count, and limitation. The table "
            "is a DRAFT assessment; the independent evaluator recomputes coverage from the frozen schema.",
            header
            + "    observed = len(prediction_rows)\n"
            + "    present = sum(1 for item in prediction_rows if item['close'] is not None)\n"
            + "    return {'rows': [{'field_name': 'close', 'source_id': 'frozen_snapshot', "
            + "'coverage': float(present) / observed if observed else 0.0, 'pit_eligible': True, "
            + "'observed_count': present, 'limitation': 'independent recomputation required'}]}",
        )
    return (
        "Return a rows array matching the requested standard research table; do not add identity fields.",
        header + "    return {'rows': []}",
    )


__all__ = ["ConfiguredResearchLLMFactory", "SafeOpenAIResearchChatLLM"]
