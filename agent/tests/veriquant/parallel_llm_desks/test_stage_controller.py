from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.llm_desks.api import (
    CompilePITArtifact,
    DeskId,
    DeskLLMConfig,
    DeterministicProposalProvider,
    FIVE_STAGE_SEQUENCE,
    FactorDraftFill,
    LLMDeskStatus,
    OfficialEvaluationView,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    OpenAICompatibleResponse,
    ProviderBudget,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    ResearchStageController,
    build_default_desk_configs,
    build_default_tools_for_desk,
)
from src.veriquant.llm_desks.safety import canonical_hash


def question() -> ResearchQuestionView:
    return ResearchQuestionView(
        question_id="q.controller",
        text="Run six bounded proposal desks.",
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=datetime(2026, 1, 5, tzinfo=timezone.utc),
        availability_assumptions=("available_at <= decision_time",),
    )


@dataclass
class FakeCompiler:
    calls: list[DeskId]

    def compile_and_validate(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        factor: FactorDraftFill,
    ) -> CompilePITArtifact:
        self.calls.append(desk_id)
        return CompilePITArtifact(
            compiled_factor_hash=factor.artifact_hash,
            pit_snapshot_hash=canonical_hash({"question": question.question_id, "desk": desk_id.value}),
        )


@dataclass
class FakeEvaluator:
    calls: list[DeskId]

    def evaluate_official(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        compiled: CompilePITArtifact,
    ) -> OfficialEvaluationView:
        self.calls.append(desk_id)
        evidence_hash = canonical_hash(
            {
                "compiled": compiled.compiled_factor_hash,
                "snapshot": compiled.pit_snapshot_hash,
                "question": question.question_id,
            }
        )
        return OfficialEvaluationView(
            evidence_hash=evidence_hash,
            redacted_summary=RedactedEvidenceSummary(
                evidence_hash=evidence_hash,
                visible_metrics={"net_icir": 0.25, "validation_observations": 10.0},
                allowed_notes=("visible validation summary",),
            ),
        )


class BlockingProvider(DeterministicProposalProvider):
    def draft_factor(self, **kwargs: object) -> FactorDraftFill:
        raise ContractValidationError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "blocked desk attempted a forbidden capability",
        )


def test_controller_runs_fixed_five_stages_for_all_six_desks() -> None:
    compiler = FakeCompiler(calls=[])
    evaluator = FakeEvaluator(calls=[])
    controller = ResearchStageController(compiler=compiler, evaluator=evaluator)
    desks = build_default_desk_configs()
    result = controller.run_all(
        desks=desks,
        question=question(),
        providers={desk.desk_id: DeterministicProposalProvider() for desk in desks},
        tools={desk.desk_id: build_default_tools_for_desk(desk.desk_id) for desk in desks},
    )

    assert len(result.results) == 6
    assert all(item.status is LLMDeskStatus.DRAFT for item in result.results)
    assert all(tuple(record.stage for record in item.stage_trace) == FIVE_STAGE_SEQUENCE for item in result.results)
    assert tuple(compiler.calls) == tuple(desk.desk_id for desk in desks)
    assert tuple(evaluator.calls) == tuple(desk.desk_id for desk in desks)


def test_single_desk_failure_is_isolated() -> None:
    compiler = FakeCompiler(calls=[])
    evaluator = FakeEvaluator(calls=[])
    controller = ResearchStageController(compiler=compiler, evaluator=evaluator)
    desks = build_default_desk_configs()
    providers = {desk.desk_id: DeterministicProposalProvider() for desk in desks}
    providers[DeskId.MARKET_STRUCTURE] = BlockingProvider()

    result = controller.run_all(
        desks=desks,
        question=question(),
        providers=providers,
        tools={desk.desk_id: build_default_tools_for_desk(desk.desk_id) for desk in desks},
    )

    by_desk = {item.desk_id: item for item in result.results}
    assert by_desk[DeskId.MARKET_STRUCTURE].status is LLMDeskStatus.BLOCKED
    assert by_desk[DeskId.MARKET_STRUCTURE].official_evaluation is None
    assert sum(item.status is LLMDeskStatus.DRAFT for item in result.results) == 5


class FixedLLMClient:
    def complete_json(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
    ) -> OpenAICompatibleResponse:
        slot = messages[0]["content"]
        if "hypothesis" in slot:
            payload = {
                "statement": "Visible valuation spreads may forecast next-period cross-sectional returns.",
                "economic_mechanism": "Slow adjustment to released accounting information can leave temporary valuation gaps.",
                "expected_sign": "POSITIVE",
                "falsification_tests": ["Reject if net validation evidence is weak after costs."],
            }
        elif "factor_draft" in slot:
            payload = {
                "name": "llm_book_to_market_draft",
                "description": "Rank released book value over market capitalization.",
                "expression": "rank_cross_section(divide_safe(book_value, market_cap))",
                "variables": ["book_value", "market_cap"],
                "lookback_bars": 1,
                "neutralization": [],
            }
        else:
            payload = {"explanation": "Reviewed redacted visible evidence; deterministic services retain all metric authority."}
        return OpenAICompatibleResponse(payload=payload, input_tokens=1, output_tokens=1)


def test_llm_provider_cannot_change_official_evidence_authority() -> None:
    compiler = FakeCompiler(calls=[])
    evaluator = FakeEvaluator(calls=[])
    controller = ResearchStageController(compiler=compiler, evaluator=evaluator)
    base_desk = build_default_desk_configs()[0]
    deterministic = controller.run_desk(
        desk=base_desk,
        question=question(),
        provider=DeterministicProposalProvider(),
        tools=build_default_tools_for_desk(base_desk.desk_id),
    )
    llm_desk = DeskLLMConfig(
        desk_id=base_desk.desk_id,
        role=base_desk.role,
        context=base_desk.context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", max_calls=6),
        tool_names=base_desk.tool_names,
        artifact_namespace=base_desk.artifact_namespace,
    )
    llm = controller.run_desk(
        desk=llm_desk,
        question=question(),
        provider=OpenAICompatibleProvider(
            client=FixedLLMClient(),
            config=OpenAICompatibleConfig(model="offline-fake"),
        ),
        tools=build_default_tools_for_desk(llm_desk.desk_id),
    )

    assert deterministic.official_evaluation is not None
    assert llm.official_evaluation is not None
    assert deterministic.official_evaluation.evidence_hash == llm.official_evaluation.evidence_hash


def test_redacted_summary_rejects_sealed_leakage() -> None:
    evidence_hash = canonical_hash({"evidence": "visible"})
    try:
        RedactedEvidenceSummary(
            evidence_hash=evidence_hash,
            visible_metrics={"sealed_rank_ic": 1.0},
        )
    except ContractValidationError as exc:
        assert exc.code is FailureCode.UNAUTHORIZED_CAPABILITY
    else:
        raise AssertionError("sealed metric was accepted")
