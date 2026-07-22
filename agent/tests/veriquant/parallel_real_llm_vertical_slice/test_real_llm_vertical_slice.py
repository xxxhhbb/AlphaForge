from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.evidence import TrialLedger, TrialOutcome, TrialStatus
from src.veriquant.llm_desks.api import (
    LLMDeskStatus,
    OpenAICompatibleConfig,
    OpenAICompatibleResponse,
    ResearchQuestionView,
)
from src.veriquant.research.authority import DeterministicOfficialEvidenceAuthority
from src.veriquant.real_llm_vertical_slice.vertical import (
    CandidateSubmissionService,
    RealLLMVerticalSlice,
    VerticalSliceRunManifest,
)


def _hypothesis_payload() -> Mapping[str, Any]:
    return {
        "state": "DRAFT",
        "statement": "Released book value relative to market value may forecast near-horizon cross-sectional returns.",
        "economic_mechanism": "Released accounting information can diffuse slowly into valuation spreads.",
        "expected_sign": "POSITIVE",
        "falsification_tests": [
            "Reject if visible net validation turns negative after costs.",
            "Reject if visible stability is weak across periods.",
        ],
    }


def _factor_payload(**overrides: Any) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "state": "DRAFT",
        "name": "rl4_book_to_market",
        "description": "Rank released book value divided by market capitalization.",
        "expression": "rank_cross_section(divide_safe(book_value, market_cap))",
        "variables": ["book_value", "market_cap"],
        "lookback_bars": 1,
        "neutralization": [],
    }
    payload.update(overrides)
    return payload


def _feedback_payload() -> Mapping[str, Any]:
    return {
        "state": "DRAFT",
        "explanation": "Reviewed visible validation numbers; deterministic services decide the next research state.",
    }


class FakeOpenAIClient:
    def __init__(self, payloads: list[Mapping[str, Any]], *, fail_on_call: bool = False) -> None:
        self.payloads = list(payloads)
        self.fail_on_call = fail_on_call
        self.calls: list[Mapping[str, Any]] = []

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
        if self.fail_on_call:
            raise AssertionError("provider should not be called during resume")
        self.calls.append(
            {
                "messages": tuple(dict(item) for item in messages),
                "model": model,
                "max_tokens": max_tokens,
                "extra": dict(extra),
            }
        )
        return OpenAICompatibleResponse(
            payload=self.payloads.pop(0),
            input_tokens=7,
            output_tokens=11,
            cost=0.0,
        )


def _config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(model="rl4-fake-openai-compatible", temperature=0.0)


def test_one_desk_real_llm_protocol_reaches_factor_dag_pit_evidence_and_feedback(tmp_path: Path) -> None:
    client = FakeOpenAIClient([_hypothesis_payload(), _factor_payload(), _feedback_payload()])
    runner = RealLLMVerticalSlice(client=client, config=_config())

    manifest = runner.run(run_id="rl4-success", run_dir=tmp_path)

    assert manifest.status == "COMPLETED"
    assert manifest.stage_sequence == (
        "hypothesis",
        "factor_draft",
        "deterministic_compile_pit",
        "official_evaluate",
        "redacted_feedback",
    )
    assert len(client.calls) == 3
    assert len(manifest.prompt_hashes) == 3
    assert manifest.factor_draft_hash is not None
    assert manifest.compile_pit_hash is not None
    assert manifest.evaluation_view_hash is not None
    assert manifest.feedback_hash is not None

    receipt = json.loads((tmp_path / "candidate_receipt.json").read_text(encoding="utf-8"))
    assert receipt["evidence_hash"].startswith("sha256:")
    assert receipt["decision_hash"].startswith("sha256:")
    assert receipt["budget_units_consumed"] == 1
    assert receipt["sealed_visible"] is False

    replay = TrialLedger(tmp_path / "trial_ledger.sqlite3").replay()
    family = replay.families["rl4-success:fundamental"]
    trial = replay.trials["rl4-success:fundamental:trial"]
    assert family.consumed == 1
    assert family.terminal_trials == 1
    assert trial.status is TrialStatus.TERMINAL
    assert trial.outcome is TrialOutcome.SUCCESS


def test_provider_payload_with_forbidden_authority_is_rejected_before_submission(tmp_path: Path) -> None:
    client = FakeOpenAIClient(
        [
            _hypothesis_payload(),
            _factor_payload(official_metrics={"rank_ic": 1.0}),
        ]
    )
    runner = RealLLMVerticalSlice(client=client, config=_config())

    manifest = runner.run(run_id="rl4-forbidden", run_dir=tmp_path)

    assert manifest.status == "BLOCKED"
    assert manifest.failure_code == FailureCode.UNAUTHORIZED_CAPABILITY.value
    assert manifest.factor_draft_hash is None
    assert not (tmp_path / "candidate_receipt.json").exists()
    assert len(client.calls) == 2
    assert "official_metrics" not in json.dumps(manifest.to_dict())


def test_pit_failure_after_submission_consumes_trial_budget(tmp_path: Path) -> None:
    authority = DeterministicOfficialEvidenceAuthority()
    bad_question = ResearchQuestionView(
        question_id="rl4-bad-time:question",
        text="Find a visible valuation signal for the frozen equity universe.",
        universe=authority.dataset.registry.get(authority.dataset.dataset_id).card.universe,
        horizon="5d",
        decision_time=authority.dataset.decision_time + timedelta(days=1),
        availability_assumptions=(
            "use only fields with available_at not after decision_time",
            "historical membership is point-in-time",
            "labels are never prompt-visible features",
        ),
    )
    client = FakeOpenAIClient([_hypothesis_payload(), _factor_payload()])
    runner = RealLLMVerticalSlice(client=client, config=_config(), authority=authority)

    manifest = runner.run(run_id="rl4-pit-fail", run_dir=tmp_path, question=bad_question)

    assert manifest.status == "INCOMPLETE"
    assert manifest.failure_code == FailureCode.INVALID_TIME_ORDER.value
    receipt = json.loads((tmp_path / "candidate_receipt.json").read_text(encoding="utf-8"))
    assert receipt["failure_code"] == FailureCode.INVALID_TIME_ORDER.value
    assert receipt["budget_units_consumed"] == 1
    replay = TrialLedger(tmp_path / "trial_ledger.sqlite3").replay()
    family = replay.families["rl4-pit-fail:fundamental"]
    trial = replay.trials["rl4-pit-fail:fundamental:trial"]
    assert family.consumed == 1
    assert trial.status is TrialStatus.TERMINAL
    assert trial.outcome is TrialOutcome.PIT_REJECTION


def test_resume_returns_existing_manifest_without_provider_or_evidence_call(tmp_path: Path) -> None:
    first_client = FakeOpenAIClient([_hypothesis_payload(), _factor_payload(), _feedback_payload()])
    first = RealLLMVerticalSlice(client=first_client, config=_config())
    manifest = first.run(run_id="rl4-resume", run_dir=tmp_path)

    second_client = FakeOpenAIClient([], fail_on_call=True)
    second = RealLLMVerticalSlice(client=second_client, config=_config())
    resumed = second.run(run_id="rl4-resume", run_dir=tmp_path, resume=True)

    assert resumed.artifact_hash == manifest.artifact_hash
    assert resumed == VerticalSliceRunManifest.from_payload(
        json.loads((tmp_path / "vertical_manifest.json").read_text(encoding="utf-8"))
    )
    assert second_client.calls == []
    replay = TrialLedger(tmp_path / "trial_ledger.sqlite3").replay()
    assert replay.families["rl4-resume:fundamental"].terminal_trials == 1


def test_redacted_feedback_prompt_contains_no_sealed_or_privileged_strings(tmp_path: Path) -> None:
    client = FakeOpenAIClient([_hypothesis_payload(), _factor_payload(), _feedback_payload()])
    runner = RealLLMVerticalSlice(client=client, config=_config())

    runner.run(run_id="rl4-redaction", run_dir=tmp_path)

    rendered_messages = "\n".join(
        message["content"] for call in client.calls for message in call["messages"]
    ).lower()
    for forbidden in (
        "sealed",
        "holdout",
        "official_metrics",
        "promotion",
        "target_weight",
        "risk_approval",
        "broker",
        "ledger",
    ):
        assert forbidden not in rendered_messages


def test_candidate_submission_service_is_not_a_tool_or_llm_authority() -> None:
    exported = set(CandidateSubmissionService.__dict__)
    assert "complete_json" not in exported
    assert "call" not in exported
    assert "place_order" not in exported
    assert "approve_risk" not in exported
    assert "promote" not in exported
