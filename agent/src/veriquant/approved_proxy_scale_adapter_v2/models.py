"""Contracts for bounded, dynamically sized approved-proxy acquisitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_aware_datetime, require_identifier, require_text
from ..real_ashare_scale import FrozenAshareScaleInput, InputOrigin
from ..research_real_data_v2 import DEFAULT_OPTIONAL_FEATURE_FIELDS, PRICE_CORE_FEATURE_FIELDS


SCHEMA_VERSION = "alphaforge.approved-proxy-scale-adapter-v2/v1"


class ProxyScaleAdapterStatus(str, Enum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"


class ProxyScaleFailureCode(str, Enum):
    NETWORK_NOT_ENABLED = "NETWORK_NOT_ENABLED"
    API_UNAVAILABLE = "API_UNAVAILABLE"
    API_RESPONSE_INVALID = "API_RESPONSE_INVALID"
    CALENDAR_INSUFFICIENT = "CALENDAR_INSUFFICIENT"
    HISTORICAL_UNIVERSE_INSUFFICIENT = "HISTORICAL_UNIVERSE_INSUFFICIENT"
    SYMBOL_SCALE_OUT_OF_BOUNDS = "SYMBOL_SCALE_OUT_OF_BOUNDS"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    INDUSTRY_HISTORY_UNAVAILABLE = "INDUSTRY_HISTORY_UNAVAILABLE"
    PIT_TIME_SEMANTICS_UNAVAILABLE = "PIT_TIME_SEMANTICS_UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class ProxyScaleAcquisitionRequest(CanonicalContract):
    dataset_id: str
    index_code: str
    start_date: str
    end_date: str
    capture_time: datetime
    target_symbols: int = 30
    max_acquisition_symbols: int | None = None
    required_sessions: int = 260
    label_horizon_sessions: int = 5
    allow_network: bool = False
    required_feature_fields: tuple[str, ...] = PRICE_CORE_FEATURE_FIELDS
    optional_feature_fields: tuple[str, ...] = DEFAULT_OPTIONAL_FEATURE_FIELDS
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_identifier(self.index_code, "index_code")
        require_text(self.start_date, "start_date")
        require_text(self.end_date, "end_date")
        try:
            start = datetime.strptime(self.start_date, "%Y%m%d")
            end = datetime.strptime(self.end_date, "%Y%m%d")
        except ValueError:
            fail(FailureCode.INVALID_TIMESTAMP, "dates must be YYYYMMDD")
        if start > end:
            fail(FailureCode.INVALID_TIME_ORDER, "start_date must not follow end_date")
        require_aware_datetime(self.capture_time, "capture_time")
        if (
            isinstance(self.target_symbols, bool)
            or not isinstance(self.target_symbols, int)
            or self.target_symbols < 1
        ):
            fail(
                FailureCode.INVALID_VALUE,
                "target_symbols must be a positive integer",
            )
        if self.max_acquisition_symbols is not None and (
            isinstance(self.max_acquisition_symbols, bool)
            or not isinstance(self.max_acquisition_symbols, int)
            or self.max_acquisition_symbols < self.target_symbols
        ):
            fail(
                FailureCode.INVALID_VALUE,
                "max_acquisition_symbols must be absent or at least target_symbols",
            )
        if (
            isinstance(self.required_sessions, bool)
            or not isinstance(self.required_sessions, int)
            or self.required_sessions < 1
        ):
            fail(
                FailureCode.INVALID_VALUE,
                "required_sessions must be a positive integer",
            )
        if (
            isinstance(self.label_horizon_sessions, bool)
            or not isinstance(self.label_horizon_sessions, int)
            or self.label_horizon_sessions < 1
        ):
            fail(
                FailureCode.INVALID_VALUE,
                "label_horizon_sessions must be a positive integer",
            )
        if type(self.allow_network) is not bool:
            fail(FailureCode.INVALID_VALUE, "allow_network must be boolean")
        if not self.required_feature_fields:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "required_feature_fields are required")
        for field_name in self.required_feature_fields + self.optional_feature_fields:
            require_identifier(field_name, "feature_field")
            if "future_return" in field_name.lower():
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return is label-only")
        if len(set(self.required_feature_fields)) != len(self.required_feature_fields):
            fail(FailureCode.INVALID_VALUE, "required fields contain duplicates")
        if len(set(self.optional_feature_fields)) != len(self.optional_feature_fields):
            fail(FailureCode.INVALID_VALUE, "optional fields contain duplicates")
        if set(self.required_feature_fields) & set(self.optional_feature_fields):
            fail(FailureCode.INVALID_VALUE, "required and optional fields must be disjoint")


@dataclass(frozen=True, kw_only=True)
class ProxyScaleAdapterResult(CanonicalContract):
    request_hash: str
    status: ProxyScaleAdapterStatus
    failure_codes: tuple[ProxyScaleFailureCode, ...]
    message: str
    frozen_input: FrozenAshareScaleInput | None
    api_manifest_hash: str | None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.message, "message")
        from ..contracts.base import require_hash
        require_hash(self.request_hash, "request_hash")
        if self.api_manifest_hash is not None:
            require_hash(self.api_manifest_hash, "api_manifest_hash")
        if any(not isinstance(code, ProxyScaleFailureCode) for code in self.failure_codes):
            fail(FailureCode.INVALID_VALUE, "failure codes must be typed")
        if self.status is ProxyScaleAdapterStatus.READY:
            if self.failure_codes or self.frozen_input is None or self.api_manifest_hash is None:
                fail(FailureCode.INCOMPLETE_RESULT, "READY requires frozen input and manifest")
        elif self.frozen_input is not None:
            fail(FailureCode.INVALID_STATE, "failed result cannot carry frozen input")
