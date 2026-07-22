from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.campaign_data_binding import (
    CampaignDataBindingFailure,
    CampaignDataBindingRequest,
    CampaignDataBindingService,
    CampaignDataBindingStatus,
    CampaignDatasetBundle,
    DataVendorClass,
)
from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignState,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _dt() -> datetime:
    return datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def _draft(*, state: CampaignState = CampaignState.NEEDS_CONFIRMATION) -> ResearchCampaignDraft:
    return ResearchCampaignDraft(
        campaign_id="campaign.data_binding",
        question="bind data",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=ResearchSubject(
            subject_id="subject.data_binding",
            description="data binding subject",
            universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
            horizon="20d",
            decision_time=_dt(),
            allowed_data=("price_volume", "fundamentals"),
        ),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        candidate_agents=("market_structure", "fundamental"),
        budget=CampaignBudget(max_factor_families=2, max_trials=4, max_llm_calls=4, max_runtime_seconds=120),
        user_checkpoints=(UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief"),),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=HASH_A,
        policy_hash=HASH_B,
        code_hash=HASH_C,
        created_at=_dt(),
        proposed_by=Component.LLM_RESEARCHER,
        state=state,
    )


def _campaign() -> ResearchCampaign:
    return ResearchCampaign.confirm(_draft(), confirmed_at=_dt())


def test_confirmed_campaign_binds_to_stable_bundle() -> None:
    request = CampaignDataBindingRequest(campaign=_campaign(), content_refs={"fixture": HASH_A})
    left = CampaignDataBindingService().bind(request)
    right = CampaignDataBindingService().bind(request)

    assert left.status is CampaignDataBindingStatus.BOUND
    assert left.bundle is not None
    assert left.bundle.artifact_hash == right.bundle.artifact_hash
    assert left.bundle.universe == ("CN_ASHARE_APPROVED_PIT_SAMPLE",)
    assert left.bundle.horizon == "20d"


def test_unconfirmed_campaign_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignDataBindingRequest(campaign=_draft(state=CampaignState.DRAFT))
    assert exc.value.code is FailureCode.INVALID_STATE


def test_approved_proxy_is_not_official_tushare() -> None:
    request = CampaignDataBindingRequest(
        campaign=_campaign(),
        vendor_class=DataVendorClass.APPROVED_PROXY_DATA_VENDOR,
        provider_source_label="APPROVED_PROXY_DATA_VENDOR",
    )
    result = CampaignDataBindingService().bind(request)
    assert result.bundle is not None
    assert result.bundle.provider_source_label == "APPROVED_PROXY_DATA_VENDOR"
    assert result.bundle.official_tushare_source is False

    with pytest.raises(ContractValidationError):
        CampaignDataBindingRequest(
            campaign=_campaign(),
            vendor_class=DataVendorClass.APPROVED_PROXY_DATA_VENDOR,
            provider_source_label="OFFICIAL_TUSHARE",
        )


def test_provider_unavailable_is_typed_and_does_not_substitute_data() -> None:
    result = CampaignDataBindingService().bind(CampaignDataBindingRequest(campaign=_campaign(), provider_available=False))
    assert result.status is CampaignDataBindingStatus.UNAVAILABLE
    assert result.bundle is None
    assert result.failure is CampaignDataBindingFailure.DATA_PROVIDER_UNAVAILABLE
    assert result.failure_code is FailureCode.ADAPTER_UNAVAILABLE


def test_bundle_rejects_downstream_or_sealed_claims() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignDatasetBundle(
            bundle_id="bundle.bad",
            campaign_hash=HASH_A,
            vendor_class=DataVendorClass.APPROVED_PROXY_DATA_VENDOR,
            provider_source_label="APPROVED_PROXY_DATA_VENDOR",
            official_tushare_source=True,
            universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
            horizon="20d",
            decision_time=_dt(),
            allowed_data=("price_volume",),
            calendar_id="cn_ashare_sse_szse",
            manifest_hash=HASH_B,
            research_visible_summary_hash=HASH_C,
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
