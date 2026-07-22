from __future__ import annotations

import pytest

from src.veriquant.campaign_stage_execution import (
    CampaignStageFailure,
    CampaignStageRequest,
    CampaignStageResult,
    CampaignStageStatus,
    DeterministicStageFixtureExecutor,
    ProviderMode,
    StageExecutionPolicy,
)
from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import CampaignState


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def _request(**overrides) -> CampaignStageRequest:
    payload = {
        "run_id": "campaign-stage-run",
        "campaign_hash": HASH_A,
        "route_hash": HASH_B,
        "protocol_plan_hash": HASH_C,
        "campaign_state": CampaignState.CONFIRMED,
        "route_state": CampaignState.ROUTED,
        "selected_desks": ("market_structure",),
        "route_task_id": "task.market_structure.1",
        "desk_id": "market_structure",
        "stage_id": "hypothesis",
        "stage_index": 0,
        "stage_sequence": ("hypothesis", "factor_construct", "factor_dag_pit", "official_evaluate", "redacted_feedback"),
        "input_hash": HASH_D,
        "artifact_namespace": "campaign.route.market_structure",
    }
    payload.update(overrides)
    return CampaignStageRequest(**payload)


def test_request_requires_confirmed_campaign_and_routed_route() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(campaign_state=CampaignState.DRAFT)
    assert exc.value.details["stage_failure_code"] == CampaignStageFailure.CAMPAIGN_NOT_CONFIRMED.value

    with pytest.raises(ContractValidationError) as exc:
        _request(route_state=CampaignState.CONFIRMED)
    assert exc.value.details["stage_failure_code"] == CampaignStageFailure.ROUTE_NOT_READY.value


def test_unselected_desk_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(desk_id="fundamental")
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_same_stage_input_produces_stable_hash() -> None:
    executor = DeterministicStageFixtureExecutor()
    policy = StageExecutionPolicy(provider_mode=ProviderMode.DETERMINISTIC)
    left = executor.run_stage(_request(), policy)
    right = executor.run_stage(_request(), policy)

    assert left.artifact_hash == right.artifact_hash
    assert executor.calls == ["task.market_structure.1:hypothesis", "task.market_structure.1:hypothesis"]


def test_real_llm_unavailable_fails_closed_without_deterministic_output() -> None:
    executor = DeterministicStageFixtureExecutor(provider_available=False)
    policy = StageExecutionPolicy(
        provider_mode=ProviderMode.REAL_LLM,
        allow_real_provider=True,
        allow_deterministic_fallback=False,
    )
    result = executor.run_stage(_request(), policy)

    assert result.status is CampaignStageStatus.UNAVAILABLE
    assert result.failure_code is CampaignStageFailure.PROVIDER_UNAVAILABLE
    assert result.output_hash is None
    assert result.artifact_refs == ()


def test_upstream_failure_blocks_only_requested_stage() -> None:
    result = DeterministicStageFixtureExecutor().run_stage(
        _request(upstream_satisfied=False),
        StageExecutionPolicy(provider_mode=ProviderMode.DETERMINISTIC),
    )

    assert result.status is CampaignStageStatus.SKIPPED_UPSTREAM
    assert result.failure_detail_code is FailureCode.UPSTREAM_BLOCKED


def test_forbidden_context_is_rejected_before_prompt_surface() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(context={"official_metrics": {"rank_ic": 1.0}})
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_llm_or_research_desk_cannot_commit_control_reference() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignStageResult(
            run_id="campaign-stage-run",
            stage_key="task.market_structure.1:official_evaluate",
            route_task_id="task.market_structure.1",
            desk_id="market_structure",
            stage_id="official_evaluate",
            stage_index=3,
            status=CampaignStageStatus.CONTROL_REFERENCE,
            produced_by=Component.RESEARCH_DESK,
            input_hash=HASH_D,
            output_hash=HASH_A,
            prompt_hash=HASH_B,
            trace_hash=HASH_C,
        )
    assert exc.value.details["stage_failure_code"] == CampaignStageFailure.AUTHORITY_VIOLATION.value
