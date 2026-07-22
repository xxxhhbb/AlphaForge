from __future__ import annotations

import pytest

from veriquant.research500_harness.coverage_compose import (
    _derived_auxiliary_receipts,
    _registered_receipt_map,
)


def test_registered_receipt_merge_input_preserves_real_portfolio_context() -> None:
    values = _registered_receipt_map([
        {"data_id": "portfolio_returns", "status": "READY", "content_hash": "sha256:portfolio"},
        {"data_id": "registered_evidence", "status": "READY", "content_hash": "sha256:evidence"},
    ])
    values["registered_factor_summary"] = {
        "data_id": "registered_factor_summary",
        "status": "READY",
        "content_hash": "sha256:factor",
    }
    assert values["portfolio_returns"]["content_hash"] == "sha256:portfolio"
    assert set(values) == {"portfolio_returns", "registered_evidence", "registered_factor_summary"}


def test_conflicting_registered_context_receipt_fails_closed() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        _registered_receipt_map([
            {"data_id": "portfolio_returns", "status": "READY", "content_hash": "sha256:first"},
            {"data_id": "portfolio_returns", "status": "READY", "content_hash": "sha256:second"},
        ])


@pytest.mark.parametrize(
    "value",
    [
        {"data_id": "", "status": "READY", "content_hash": "sha256:x"},
        {"data_id": "portfolio_returns", "status": "MISSING", "content_hash": "sha256:x"},
        {"data_id": "portfolio_returns", "status": "READY", "content_hash": "not-a-hash"},
    ],
)
def test_invalid_registered_context_receipt_fails_closed(value) -> None:
    with pytest.raises(ValueError, match="invalid"):
        _registered_receipt_map([value])


def test_verified_documents_and_express_form_disclosed_forecast_proxy() -> None:
    official = {
        "express": {
            "source_id": "express", "status": "SUCCESS",
            "authority": "OFFICIAL_INPUT", "row_count": 3004,
            "content_hash": "sha256:express",
        },
        "forecast": {
            "source_id": "forecast", "status": "EMPTY",
            "authority": "OFFICIAL_INPUT", "row_count": 0,
            "content_hash": "sha256:empty",
        },
    }
    auxiliary = {
        "research_reports": {
            "source_id": "research_reports", "status": "SUCCESS",
            "authority": "AUXILIARY_RESEARCH_EVIDENCE", "row_count": 50,
            "content_hash": "sha256:reports",
        },
        "stock_news": {
            "source_id": "stock_news", "status": "SUCCESS",
            "authority": "AUXILIARY_RESEARCH_EVIDENCE", "row_count": 20,
            "content_hash": "sha256:news",
        },
    }
    values = _derived_auxiliary_receipts(
        gateway_receipts=official, auxiliary_receipts=auxiliary
    )
    assert values["documents"]["row_count"] == 70
    proxy = values["forecast_proxy_express_documents"]
    assert proxy["proxy_for"] == "forecast"
    assert proxy["authority"] == "AUXILIARY_RESEARCH_EVIDENCE"
    assert proxy["limitation"] == "RESEARCH_ONLY_NOT_ACTUAL_MANAGEMENT_FORECAST"
