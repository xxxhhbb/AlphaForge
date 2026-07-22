from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import Component, ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.formula_execution import FormulaProgramIdentity, FormulaProgramKind
from veriquant.qlib_candidate_translation import (
    QlibCandidateDraftEnvelope,
    QlibCandidateFailureCode,
    QlibCandidateTranslationStatus,
    SafeQlibCandidateTranslator,
    build_qlib_candidate_llm_contract,
)
from veriquant.qlib_expression.api import (
    STATIC_QLIB_OPERATOR_MANIFEST,
    QlibExpressionFailureCode,
    QlibOperatorTrust,
)
from veriquant.research_campaign import (
    CampaignBudget,
    CampaignState,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)


NOW = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)
HASH = canonical_content_hash({"fixture": "qlib-candidate"})


def _campaign() -> ResearchCampaign:
    return ResearchCampaign(
        campaign_id="campaign.qlib",
        draft_hash=HASH,
        question="研究50日动量和量价相关性",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=ResearchSubject(
            subject_id="subject.qlib",
            description="A股日频公式研究",
            universe=("000001.SZ", "600000.SH"),
            horizon="20d",
            decision_time=NOW,
            allowed_data=("close", "open", "volume"),
        ),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        candidate_agents=("price_volume_group",),
        budget=CampaignBudget(
            max_factor_families=2,
            max_trials=4,
            max_llm_calls=4,
            max_runtime_seconds=60,
        ),
        user_checkpoints=(
            UserCheckpoint(
                checkpoint_id="checkpoint.confirmed",
                stage="candidate_generation",
                required=True,
            ),
        ),
        stop_conditions=(StopCondition.VALIDATION_COMPLETED,),
        data_manifest_hash=HASH,
        policy_hash=HASH,
        code_hash=HASH,
        confirmed_at=NOW,
        confirmed_by=Component.OPERATOR,
        state=CampaignState.CONFIRMED,
    )


def _draft(
    expression: str,
    *,
    candidate_id: str = "candidate.one",
    lookback: int | None = None,
    fields: tuple[str, ...] = ("close", "open", "volume"),
) -> QlibCandidateDraftEnvelope:
    return QlibCandidateDraftEnvelope(
        campaign_hash=_campaign().artifact_hash,
        campaign_state=CampaignState.CONFIRMED,
        group_id="price_volume_group",
        candidate_id=candidate_id,
        expression=expression,
        confirmed_field_whitelist=fields,
        requested_lookback_sessions=lookback,
        source_draft_hash=HASH,
        proposed_by=Component.RESEARCH_DESK,
    )


@pytest.mark.parametrize(
    ("expression", "lookback", "fields", "expected_required", "expected_maximum"),
    (
        ("($close/Ref($close,50))-1", 50, ("close",), ("close",), 50),
        ("Corr($close,$volume,20)", 20, ("close", "volume"), ("close", "volume"), 19),
        (
            "If(Mean($close,20)>Mean($close,50),1,0)",
            50,
            ("close",),
            ("close",),
            49,
        ),
    ),
)
def test_momentum_correlation_and_conditional_candidates_certify(
    expression: str,
    lookback: int,
    fields: tuple[str, ...],
    expected_required: tuple[str, ...],
    expected_maximum: int,
) -> None:
    result = SafeQlibCandidateTranslator().translate(
        _draft(expression, lookback=lookback, fields=fields)
    )

    assert result.status is QlibCandidateTranslationStatus.CERTIFIED
    assert result.required_fields == expected_required
    assert result.maximum_lookback == expected_maximum
    assert result.expression_hash and result.program_hash
    assert result.certified_expression is not None
    assert result.root is result.certified_expression.root
    assert result.policy_hash.startswith("sha256:")
    assert result.manifest_hash == STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash
    assert "官方" in result.message

    identity = FormulaProgramIdentity(
        program_id="program.test",
        kind=FormulaProgramKind.CERTIFIED_QLIB_EXPRESSION,
        program_hash=result.program_hash,
        required_fields=result.required_fields,
        runtime_hash=HASH,
        policy_hash=result.policy_hash,
    )
    assert identity.program_hash == result.program_hash


def test_requested_window_must_explicitly_appear_or_candidate_revises() -> None:
    result = SafeQlibCandidateTranslator().translate(
        _draft("Mean($close,20)", lookback=50, fields=("close",))
    )
    assert result.status is QlibCandidateTranslationStatus.REVISE
    assert result.failure_code is QlibCandidateFailureCode.REQUESTED_LOOKBACK_NOT_REPRESENTED
    assert result.program_hash is None


def test_unknown_field_is_typed_unsupported() -> None:
    result = SafeQlibCandidateTranslator().translate(
        _draft("Mean($vwap,20)", lookback=20, fields=("close",))
    )
    assert result.status is QlibCandidateTranslationStatus.UNSUPPORTED
    assert result.failure_code is QlibCandidateFailureCode.EXPRESSION_REJECTED
    assert result.expression_failure_code is QlibExpressionFailureCode.UNKNOWN_FIELD


def test_negative_ref_is_future_reference_and_never_a_program() -> None:
    result = SafeQlibCandidateTranslator().translate(
        _draft("Ref($close,-1)", fields=("close",))
    )
    assert result.status is QlibCandidateTranslationStatus.UNSUPPORTED
    assert result.expression_failure_code is QlibExpressionFailureCode.FUTURE_REFERENCE
    assert result.expression_hash is None
    assert result.certified_expression is None
    assert result.root is None
    assert result.program_hash is None


@pytest.mark.parametrize("field", ("sealed_rank", "future_return_20d", "research_label"))
def test_permission_fields_are_explicitly_forbidden(field: str) -> None:
    result = SafeQlibCandidateTranslator().translate(
        _draft(f"Mean(${field},20)", fields=("close",))
    )
    assert result.status is QlibCandidateTranslationStatus.UNSUPPORTED
    assert result.expression_failure_code is QlibExpressionFailureCode.FORBIDDEN_FIELD


@pytest.mark.parametrize(
    "expression",
    (
        "factor.py",
        "```python\nimport os\n```",
        "CustomOperator($close,20)",
    ),
)
def test_code_factor_py_and_custom_operator_are_unsupported(expression: str) -> None:
    result = SafeQlibCandidateTranslator().translate(_draft(expression, fields=("close",)))
    assert result.status is QlibCandidateTranslationStatus.UNSUPPORTED
    assert result.program_hash is None
    if expression.startswith("CustomOperator"):
        assert result.expression_failure_code is QlibExpressionFailureCode.UNKNOWN_OPERATOR
    else:
        assert result.failure_code is QlibCandidateFailureCode.FORBIDDEN_CANDIDATE_PAYLOAD


@pytest.mark.parametrize(
    "forbidden_key",
    ("code", "factor.py", "custom_operator", "official_metrics", "promotion", "risk", "orders", "sealed"),
)
def test_real_llm_payload_cannot_supply_authority_or_execution_fields(forbidden_key: str) -> None:
    payload = {
        "candidate_id": "candidate.llm",
        "expression": "Mean($close,20)",
        forbidden_key: "forbidden",
    }
    with pytest.raises(ContractValidationError) as caught:
        QlibCandidateDraftEnvelope.from_llm_payload(
            payload,
            campaign=_campaign(),
            group_id="price_volume_group",
            requested_lookback_sessions=20,
            source_draft_hash=HASH,
        )
    assert caught.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


def test_from_campaign_consumes_confirmed_field_whitelist() -> None:
    draft = QlibCandidateDraftEnvelope.from_campaign(
        _campaign(),
        group_id="price_volume_group",
        candidate_id="candidate.campaign",
        expression="Mean($close,20)",
        requested_lookback_sessions=20,
        source_draft_hash=HASH,
    )
    assert draft.campaign_state is CampaignState.CONFIRMED
    assert draft.confirmed_field_whitelist == ("close", "open", "volume")
    assert draft.campaign_hash == _campaign().artifact_hash


def test_semantically_equivalent_expression_hashes_and_program_hashes_are_stable() -> None:
    translator = SafeQlibCandidateTranslator()
    first = translator.translate(
        _draft("Mean($close,20)/1.0", lookback=20, fields=("close",))
    )
    second = translator.translate(
        _draft("(((Mean( $close , 20.000 )))/(1))", lookback=20, fields=("close",))
    )
    assert first.status is second.status is QlibCandidateTranslationStatus.CERTIFIED
    assert first.expression_hash == second.expression_hash
    assert first.program_hash == second.program_hash
    assert first.policy_hash == second.policy_hash
    assert first.manifest_hash == second.manifest_hash


def test_candidate_failure_is_independent_of_valid_sibling() -> None:
    invalid = _draft("Ref($close,-1)", candidate_id="candidate.bad", fields=("close",))
    valid = _draft(
        "Mean($close,20)",
        candidate_id="candidate.good",
        lookback=20,
        fields=("close",),
    )
    results = SafeQlibCandidateTranslator().translate_many((invalid, valid))
    assert [item.status for item in results] == [
        QlibCandidateTranslationStatus.UNSUPPORTED,
        QlibCandidateTranslationStatus.CERTIFIED,
    ]
    assert results[1].candidate_id == "candidate.good"


def test_llm_schema_and_capabilities_are_derived_from_operator_manifest() -> None:
    first = build_qlib_candidate_llm_contract(
        confirmed_field_whitelist=("volume", "close"),
        requested_lookback_sessions=50,
    )
    second = build_qlib_candidate_llm_contract(
        confirmed_field_whitelist=("volume", "close"),
        requested_lookback_sessions=50,
    )
    expected = {
        item.name
        for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust is not QlibOperatorTrust.EXCLUDED
    }
    observed = {item["name"] for item in first.capability_summary["operators"]}

    assert observed == expected
    assert first.manifest_hash == STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash
    assert first.schema["additionalProperties"] is False
    assert first.capability_summary["confirmed_field_whitelist"] == ["close", "volume"]
    assert first.contract_hash == second.contract_hash
    assert "CustomOperator" not in observed
    assert not {
        item.name
        for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust is QlibOperatorTrust.EXCLUDED
    } & observed
