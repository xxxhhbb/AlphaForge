from __future__ import annotations

from veriquant.candidate_translation import CandidateDraftEnvelope, CandidateTranslationStatus, SafeCandidateTranslator


H = "sha256:" + "c" * 64


def draft(expression: str, *, requested: int | None = 50, allowed=("close", "volume")) -> CandidateDraftEnvelope:
    return CandidateDraftEnvelope(
        campaign_hash=H, group_id="market_structure", candidate_id="momentum_candidate",
        expression=expression, requested_lookback_sessions=requested,
        declared_lookback_sessions=60, allowed_fields=allowed, source_draft_hash=H,
    )


def test_requested_50_day_candidate_compiles_to_factor_dag() -> None:
    result = SafeCandidateTranslator().translate(draft("rank_cross_section(pct_change(close, 50))"))
    assert result.status is CandidateTranslationStatus.ACCEPTED
    assert result.factor_dag_hash


def test_non_50_day_candidate_is_revise_not_silently_substituted() -> None:
    result = SafeCandidateTranslator().translate(draft("rank_cross_section(pct_change(close, 20))"))
    assert result.status is CandidateTranslationStatus.REVISE
    assert result.failure_code == "REQUESTED_LOOKBACK_NOT_REPRESENTED"


def test_transparent_operator_alias_is_recorded() -> None:
    result = SafeCandidateTranslator().translate(draft("rank(pct_change(close, 50))"))
    assert result.status is CandidateTranslationStatus.ACCEPTED
    assert result.repairs == ("operator_alias:rank->rank_cross_section",)


def test_unknown_operator_returns_typed_unsupported() -> None:
    result = SafeCandidateTranslator().translate(draft("ts_argmax(close, 50)"))
    assert result.status is CandidateTranslationStatus.UNSUPPORTED
    assert result.factor_dag_hash is None


def test_unknown_field_does_not_execute_or_fallback() -> None:
    result = SafeCandidateTranslator().translate(draft("rank_cross_section(pct_change(future_return, 50))"))
    assert result.status is CandidateTranslationStatus.UNSUPPORTED


def test_python_and_factor_py_are_rejected() -> None:
    for value in ("__import__('os').system('x')", "factor.py", "```python\nprint(1)\n```"):
        result = SafeCandidateTranslator().translate(draft(value))
        assert result.status is CandidateTranslationStatus.UNSUPPORTED
        assert result.failure_code == "FORBIDDEN_EXECUTABLE_PAYLOAD"


def test_same_draft_produces_same_hash() -> None:
    translator = SafeCandidateTranslator()
    assert translator.translate(draft("rank(pct_change(close, 50))")).artifact_hash == translator.translate(draft("rank(pct_change(close, 50))")).artifact_hash
