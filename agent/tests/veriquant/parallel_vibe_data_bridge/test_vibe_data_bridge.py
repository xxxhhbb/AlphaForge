from __future__ import annotations

from datetime import datetime, timezone

from src.veriquant.vibe_data_bridge import (
    NormalizedVibeDataBridge,
    VibeDataBridgeFailureCode,
    VibeDataBridgeRequest,
    VibeDataBridgeStatus,
)
HASH = "sha256:" + "1" * 64
CAMPAIGN_HASH = "sha256:" + "2" * 64
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 2, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 3, tzinfo=timezone.utc)
T3 = datetime(2026, 7, 4, tzinfo=timezone.utc)


def request(**overrides: object) -> VibeDataBridgeRequest:
    payload: dict[str, object] = {
        "request_id": "req_1",
        "campaign_id": "campaign_1",
        "campaign_hash": CAMPAIGN_HASH,
        "research_group_id": "fundamental",
        "capability_id": "frozen.fundamentals.summary",
        "decision_time": T3,
        "allowed_data": ("fundamentals",),
        "required_fields": ("roe",),
        "offline": True,
    }
    payload.update(overrides)
    return VibeDataBridgeRequest(**payload)


def response(payload: dict[str, object] | None = None, *, available_at: datetime = T2) -> dict[str, object]:
    retrieved_at = T3 if available_at <= T3 else available_at
    return {
        "provider": "frozen_provider",
        "source_class": "fundamental_record",
        "manifest": {"dataset": "fixture"},
        "records": [
            {
                "record_id": "r1",
                "event_time": T0.isoformat(),
                "published_at": T1.isoformat(),
                "available_at": available_at.isoformat(),
                "retrieved_at": retrieved_at.isoformat(),
                "payload": payload
                or {
                    "symbol": "000001.SZ",
                    "roe": 0.12,
                    "net_profit": 100.0,
                },
                "source_payload": {"row": "raw"},
            }
        ],
    }


def test_bridge_normalizes_frozen_rows_with_stable_hash_and_coverage() -> None:
    bridge = NormalizedVibeDataBridge()
    first = bridge.query(request(), response())
    second = bridge.query(request(), response())

    assert first.status is VibeDataBridgeStatus.AVAILABLE
    assert first.provider_source_label == "FROZEN_VIBE_MATERIAL"
    assert first.rows[0].symbol == "000001.SZ"
    assert first.rows[0].fields["roe"] == 0.12
    assert first.coverage_by_field["roe"] == 1.0
    assert first.artifact_hash == second.artifact_hash
    assert "OFFICIAL_TUSHARE" not in first.canonical_json()


def test_missing_allowed_data_and_missing_required_field_fail_closed() -> None:
    bridge = NormalizedVibeDataBridge()

    no_scope = bridge.query(request(allowed_data=("price_volume",)), response())
    missing = bridge.query(request(required_fields=("eps",)), response())

    assert no_scope.status is VibeDataBridgeStatus.UNAVAILABLE
    assert no_scope.failure_code is VibeDataBridgeFailureCode.ALLOWED_DATA_MISMATCH
    assert missing.status is VibeDataBridgeStatus.UNAVAILABLE
    assert missing.failure_code is VibeDataBridgeFailureCode.REQUIRED_FIELD_UNAVAILABLE


def test_online_capability_is_not_opened_by_offline_bridge() -> None:
    bridge = NormalizedVibeDataBridge()
    result = bridge.query(
        request(
            capability_id="approved.proxy.daily",
            allowed_data=("price_volume",),
            required_fields=("close",),
        ),
        response({"symbol": "000001.SZ", "close": 10.0}),
    )

    assert result.status is VibeDataBridgeStatus.UNAVAILABLE
    assert result.failure_code is VibeDataBridgeFailureCode.ONLINE_REQUIRED
    assert result.provider_source_label == "APPROVED_PROXY_DATA_VENDOR"


def test_future_available_at_and_secret_payload_fail_closed_without_echo() -> None:
    bridge = NormalizedVibeDataBridge()

    future = bridge.query(request(), response(available_at=datetime(2026, 7, 5, tzinfo=timezone.utc)))
    secret = bridge.query(request(), response({"symbol": "000001.SZ", "token": "do-not-show"}))

    assert future.failure_code is VibeDataBridgeFailureCode.DATA_NOT_AVAILABLE_AS_OF
    assert secret.failure_code is VibeDataBridgeFailureCode.SECRET_OR_PRIVILEGED_CONTENT
    assert "do-not-show" not in secret.canonical_json()


def test_market_screener_filters_to_frozen_universe() -> None:
    bridge = NormalizedVibeDataBridge()
    result = bridge.query(
        request(
            capability_id="frozen.market.screener",
            allowed_data=("price_volume",),
            required_fields=("market_cap",),
            frozen_universe=("000001.SZ",),
        ),
        {
            "provider": "frozen_provider",
            "source_class": "market_screener",
            "manifest": {"dataset": "fixture"},
            "records": [
                {
                    "record_id": "kept",
                    "event_time": T0.isoformat(),
                    "published_at": T1.isoformat(),
                    "available_at": T2.isoformat(),
                    "retrieved_at": T3.isoformat(),
                    "payload": {"symbol": "000001.SZ", "market_cap": 100.0},
                },
                {
                    "record_id": "excluded",
                    "event_time": T0.isoformat(),
                    "published_at": T1.isoformat(),
                    "available_at": T2.isoformat(),
                    "retrieved_at": T3.isoformat(),
                    "payload": {"symbol": "000002.SZ", "market_cap": 200.0},
                },
            ],
        },
    )

    assert result.status is VibeDataBridgeStatus.AVAILABLE
    assert len(result.rows) == 1
    assert result.rows[0].symbol == "000001.SZ"
