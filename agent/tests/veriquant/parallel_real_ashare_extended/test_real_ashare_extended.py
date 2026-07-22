from __future__ import annotations

from src.veriquant.real_ashare_extended import (
    AshareExtendedFailureCode,
    AshareExtendedScaleProbe,
    canonical_hash,
)


def good_payload() -> dict[str, object]:
    return {
        "data_source_class": "APPROVED_PROXY_DATA_VENDOR",
        "source_class": "TUSHARE_COMPATIBLE_PROXY",
        "official_tushare_source": False,
        "symbol_count": 30,
        "session_count": 180,
        "row_count": 5400,
        "feature_fields": ["close", "volume", "amount", "adv", "market_cap", "industry", "benchmark_return"],
        "label_fields": ["future_return_5d"],
        "factor_count": 3,
        "calendar_hash": "sha256:" + "a" * 64,
        "universe_hash": "sha256:" + "b" * 64,
        "snapshot_hash": "sha256:" + "c" * 64,
        "manifest_hash": "sha256:" + "d" * 64,
    }


def test_good_extended_payload_is_ready() -> None:
    verdict = AshareExtendedScaleProbe().evaluate(good_payload())
    assert verdict.status == "READY"
    assert verdict.failure_codes == ()


def test_existing_c8_shape_cannot_impersonate_extended_scale() -> None:
    payload = {
        "data_source_class": "APPROVED_PROXY_DATA_VENDOR",
        "source_class": "TUSHARE_COMPATIBLE_PROXY",
        "official_tushare_source": False,
        "snapshot": {
            "symbols": ["000001.SZ", "600000.SH"],
            "row_count": 116,
            "calendar_hash": "sha256:" + "a" * 64,
            "universe_hash": "sha256:" + "b" * 64,
            "snapshot_hash": "sha256:" + "c" * 64,
            "snapshot_manifest_hash": "sha256:" + "d" * 64,
        },
        "official_evidence": {"factor_count": 3},
    }
    verdict = AshareExtendedScaleProbe().evaluate(payload)
    assert verdict.status == "BLOCKED"
    assert AshareExtendedFailureCode.INSUFFICIENT_SYMBOLS in verdict.failure_codes
    assert AshareExtendedFailureCode.INSUFFICIENT_SESSIONS in verdict.failure_codes


def test_official_tushare_or_wrong_source_label_fails_closed() -> None:
    payload = {**good_payload(), "source_class": "OFFICIAL_TUSHARE", "official_tushare_source": True}
    verdict = AshareExtendedScaleProbe().evaluate(payload)
    assert verdict.status == "BLOCKED"
    assert AshareExtendedFailureCode.APPROVED_PROXY_REQUIRED in verdict.failure_codes
    assert AshareExtendedFailureCode.OFFICIAL_TUSHARE_MISLABEL in verdict.failure_codes


def test_future_return_cannot_be_feature() -> None:
    payload = good_payload()
    payload["feature_fields"] = [*payload["feature_fields"], "future_return_5d"]  # type: ignore[index]
    verdict = AshareExtendedScaleProbe().evaluate(payload)
    assert verdict.status == "BLOCKED"
    assert AshareExtendedFailureCode.FUTURE_RETURN_AS_FEATURE in verdict.failure_codes


def test_missing_required_fields_fail_closed() -> None:
    payload = good_payload()
    payload["feature_fields"] = ["close", "volume"]
    verdict = AshareExtendedScaleProbe().evaluate(payload)
    assert verdict.status == "BLOCKED"
    assert AshareExtendedFailureCode.MISSING_REQUIRED_FIELDS in verdict.failure_codes


def test_token_or_authorization_leakage_fails_closed() -> None:
    verdict = AshareExtendedScaleProbe().evaluate(good_payload(), log_text="Authorization: Bearer abcdefghijklmnop")
    assert verdict.status == "BLOCKED"
    assert AshareExtendedFailureCode.TOKEN_OR_SECRET_LEAKAGE in verdict.failure_codes


def test_canonical_hash_is_stable_for_key_order() -> None:
    left = canonical_hash({"b": [2, 1], "a": {"x": 1}})
    right = canonical_hash({"a": {"x": 1}, "b": [2, 1]})
    assert left == right
