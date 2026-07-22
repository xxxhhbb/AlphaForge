from __future__ import annotations

from src.veriquant.real_llm_campaign_smoke import (
    RealLLMSmokeFailureCode,
    RealLLMSmokeProbe,
    canonical_hash,
)


def good_artifact() -> dict[str, object]:
    return {
        "selected_desks": ["fundamental", "market_structure"],
        "context_exposed_desks": ["fundamental", "market_structure"],
        "provider_calls": [
            {"desk_id": "fundamental", "provider_mode": "REAL_LLM", "call_hash": "sha256:" + "a" * 64},
            {"desk_id": "market_structure", "provider_mode": "REAL_LLM", "call_hash": "sha256:" + "b" * 64},
        ],
        "output_authorities": ["DRAFT", "EXPLANATION"],
        "timeout_injected": True,
        "independent_desks_completed_after_timeout": True,
        "resume_repeated_committed_stage": False,
        "redacted_prompt_hash": "sha256:" + "c" * 64,
        "redacted_response_hash": "sha256:" + "d" * 64,
    }


def test_good_real_llm_artifact_is_ready() -> None:
    verdict = RealLLMSmokeProbe().evaluate(good_artifact())
    assert verdict.status == "READY"
    assert verdict.failure_codes == ()


def test_no_real_provider_call_is_unavailable_not_ready() -> None:
    artifact = {**good_artifact(), "provider_calls": [{"desk_id": "fundamental", "provider_mode": "DETERMINISTIC"}]}
    verdict = RealLLMSmokeProbe().evaluate(artifact)
    assert verdict.status == "UNAVAILABLE"
    assert RealLLMSmokeFailureCode.NO_REAL_PROVIDER_CALL in verdict.failure_codes


def test_excluded_desk_call_or_context_fails_closed() -> None:
    artifact = {
        **good_artifact(),
        "selected_desks": ["fundamental"],
        "context_exposed_desks": ["fundamental", "macro_regime"],
        "provider_calls": [{"desk_id": "macro_regime", "provider_mode": "REAL_LLM"}],
    }
    verdict = RealLLMSmokeProbe().evaluate(artifact)
    assert verdict.status == "BLOCKED"
    assert RealLLMSmokeFailureCode.EXCLUDED_DESK_CALLED in verdict.failure_codes
    assert RealLLMSmokeFailureCode.UNSELECTED_CONTEXT_EXPOSED in verdict.failure_codes


def test_privileged_authority_and_sealed_secret_text_fail() -> None:
    artifact = {
        **good_artifact(),
        "output_authorities": ["DRAFT", "PROMOTION"],
        "trace": "sealed_metric=1 Authorization: Bearer abcdefghijklmnop",
    }
    verdict = RealLLMSmokeProbe().evaluate(artifact)
    assert verdict.status == "BLOCKED"
    assert RealLLMSmokeFailureCode.PRIVILEGED_OUTPUT_AUTHORITY in verdict.failure_codes
    assert RealLLMSmokeFailureCode.SEALED_OR_SECRET_LEAKAGE in verdict.failure_codes


def test_timeout_and_resume_invariants_are_checked() -> None:
    artifact = {
        **good_artifact(),
        "timeout_injected": True,
        "independent_desks_completed_after_timeout": False,
        "resume_repeated_committed_stage": True,
    }
    verdict = RealLLMSmokeProbe().evaluate(artifact)
    assert RealLLMSmokeFailureCode.TIMEOUT_COLLAPSED_INDEPENDENT_DESKS in verdict.failure_codes
    assert RealLLMSmokeFailureCode.RESUME_REPEATED_COMMITTED_STAGE in verdict.failure_codes


def test_canonical_hash_is_stable_for_key_order() -> None:
    assert canonical_hash({"b": 2, "a": [1, 2]}) == canonical_hash({"a": [1, 2], "b": 2})
