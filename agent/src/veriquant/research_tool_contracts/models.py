"""Fail-closed contracts for Campaign-routed research tools.

The contracts in this module are product-layer envelopes.  They are suitable
for frozen market/document summaries, visible train/validation evidence
summaries, and experiment design plans.  They are not official financial
truth and do not grant authority to calculate metrics, promote candidates,
approve risk, create orders, or mutate ledgers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "alphaforge.research-tool-contracts/v1"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)")
_POSIX_PATH_RE = re.compile(r"(?:^|[\s'\"(])(?:\.\./|/[^/\s]+/)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(authorization\s*[:=]|bearer\s+[a-z0-9._\-]{8,}|api[_-]?key\s*[:=]|token\s*[:=]|secret\s*[:=])"
)

_FORBIDDEN_KEYS = {
    "official",
    "official_evidence",
    "official_metrics",
    "sealed",
    "sealed_metric",
    "sealed_metrics",
    "sealed_rank",
    "promotion",
    "promotion_decision",
    "risk",
    "risk_approval",
    "target_weights",
    "weights",
    "orders",
    "order",
    "ledger",
    "fills",
    "broker",
}

_SECRET_KEYS = {
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "cookie",
    "headers",
    "provider_url",
    "url",
    "uri",
    "path",
    "filepath",
    "file_path",
}


class ResearchToolFailureCode(str, Enum):
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_SCHEMA_VERSION = "INVALID_SCHEMA_VERSION"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    INVALID_HASH = "INVALID_HASH"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_TIME_ORDER = "INVALID_TIME_ORDER"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    MISSING_AVAILABLE_AT = "MISSING_AVAILABLE_AT"
    DATA_NOT_AVAILABLE_AS_OF = "DATA_NOT_AVAILABLE_AS_OF"
    FORBIDDEN_AUTHORITY_FIELD = "FORBIDDEN_AUTHORITY_FIELD"
    SECRET_LEAKAGE = "SECRET_LEAKAGE"
    PATH_OR_URL_LEAKAGE = "PATH_OR_URL_LEAKAGE"
    UNAUTHORIZED_TOOL = "UNAUTHORIZED_TOOL"
    UNAUTHORIZED_CAPABILITY = "UNAUTHORIZED_CAPABILITY"
    OFFLINE_NETWORK_DISABLED = "OFFLINE_NETWORK_DISABLED"
    CAMPAIGN_MISMATCH = "CAMPAIGN_MISMATCH"
    DESK_MISMATCH = "DESK_MISMATCH"
    ALLOWED_DATA_MISMATCH = "ALLOWED_DATA_MISMATCH"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class ResearchToolContractError(ValueError):
    """Typed fail-closed validation error."""

    def __init__(
        self,
        code: ResearchToolFailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ResearchToolVisibility(str, Enum):
    DRAFT_SUPPORT = "DRAFT_SUPPORT"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"


class ResearchToolCapability(str, Enum):
    READ_FROZEN_DATA = "READ_FROZEN_DATA"
    READ_FROZEN_DOCUMENT = "READ_FROZEN_DOCUMENT"
    READ_VISIBLE_EVIDENCE = "READ_VISIBLE_EVIDENCE"
    DESIGN_EXPERIMENT = "DESIGN_EXPERIMENT"
    LINT_DRAFT = "LINT_DRAFT"
    NETWORK_PROVIDER = "NETWORK_PROVIDER"
    WRITE_FILE = "WRITE_FILE"
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"
    PROMOTION = "PROMOTION"
    RISK_APPROVAL = "RISK_APPROVAL"
    ORDER_CREATION = "ORDER_CREATION"
    LEDGER_APPEND = "LEDGER_APPEND"

    @property
    def is_read_only(self) -> bool:
        return self in {
            ResearchToolCapability.READ_FROZEN_DATA,
            ResearchToolCapability.READ_FROZEN_DOCUMENT,
            ResearchToolCapability.READ_VISIBLE_EVIDENCE,
            ResearchToolCapability.DESIGN_EXPERIMENT,
            ResearchToolCapability.LINT_DRAFT,
            ResearchToolCapability.NETWORK_PROVIDER,
        }

    @property
    def is_privileged_financial_authority(self) -> bool:
        return self in {
            ResearchToolCapability.OFFICIAL_EVIDENCE,
            ResearchToolCapability.PROMOTION,
            ResearchToolCapability.RISK_APPROVAL,
            ResearchToolCapability.ORDER_CREATION,
            ResearchToolCapability.LEDGER_APPEND,
        }


def _fail(code: ResearchToolFailureCode, message: str, **details: Any) -> None:
    raise ResearchToolContractError(code, message, details=details)


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, f"{field} is required", field=field)


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(ResearchToolFailureCode.INVALID_IDENTIFIER, f"{field} is not a stable identifier", field=field)


def _require_tool_name(value: str, field: str = "tool_name") -> None:
    if not isinstance(value, str) or _TOOL_RE.fullmatch(value) is None:
        _fail(ResearchToolFailureCode.INVALID_IDENTIFIER, f"{field} is not a safe tool name", field=field)


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _fail(ResearchToolFailureCode.INVALID_HASH, f"{field} must be sha256:<64 lowercase hex>", field=field)


def _require_datetime(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(ResearchToolFailureCode.INVALID_TIMESTAMP, f"{field} must be timezone-aware", field=field)


def _normalize_datetime(value: datetime) -> str:
    _require_datetime(value, "datetime")
    rendered = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return rendered[:-6] + "Z"


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        _require_datetime(value, field)
        return value
    if not isinstance(value, str):
        _fail(ResearchToolFailureCode.INVALID_TIMESTAMP, f"{field} must be ISO-8601", field=field)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ResearchToolContractError(
            ResearchToolFailureCode.INVALID_TIMESTAMP,
            f"{field} must be ISO-8601",
            details={"field": field},
        ) from exc
    _require_datetime(parsed, field)
    return parsed


def _assert_time_order(left_name: str, left: datetime, right_name: str, right: datetime) -> None:
    _require_datetime(left, left_name)
    _require_datetime(right, right_name)
    if left > right:
        _fail(
            ResearchToolFailureCode.INVALID_TIME_ORDER,
            f"{left_name} must not be after {right_name}",
            left=left_name,
            right=right_name,
        )


def _assert_no_forbidden_visible_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "visible payload keys must be strings", path=path)
            normalized_key = key.lower()
            if normalized_key in _FORBIDDEN_KEYS:
                _fail(
                    ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
                    "privileged authority field is not desk-visible",
                    path=f"{path}.{key}",
                )
            if normalized_key in _SECRET_KEYS:
                _fail(
                    ResearchToolFailureCode.SECRET_LEAKAGE,
                    "secret, provider, URL, or path field is not desk-visible",
                    path=f"{path}.{key}",
                )
            _assert_no_forbidden_visible_payload(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_forbidden_visible_payload(item, f"{path}[{index}]")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "non-finite float is not canonical", path=path)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        lowered = value.lower()
        if _SECRET_VALUE_RE.search(value):
            _fail(ResearchToolFailureCode.SECRET_LEAKAGE, "secret-like value is not desk-visible", path=path)
        if _URL_RE.search(value) or _WINDOWS_PATH_RE.search(value) or _POSIX_PATH_RE.search(value) or "..\\" in lowered or "../" in lowered:
            _fail(ResearchToolFailureCode.PATH_OR_URL_LEAKAGE, "path or URL is not desk-visible", path=path)
        forbidden_terms = {
            "official_metrics",
            "official_evidence",
            "sealed_metric",
            "sealed_rank",
            "promotion_decision",
            "target_weights",
        }
        if any(term in lowered for term in forbidden_terms):
            _fail(ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD, "privileged term is not desk-visible", path=path)
        return
    _fail(
        ResearchToolFailureCode.INVALID_PAYLOAD,
        f"unsupported desk-visible value type: {type(value).__name__}",
        path=path,
    )


def _canonical_tree(value: Any, *, exclude_generated_at: bool = True) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if is_dataclass(value):
        return {
            field.name: _canonical_tree(getattr(value, field.name), exclude_generated_at=exclude_generated_at)
            for field in fields(value)
            if not (exclude_generated_at and field.name == "generated_at")
        }
    if isinstance(value, Mapping):
        return {
            key: _canonical_tree(value[key], exclude_generated_at=exclude_generated_at)
            for key in sorted(value)
            if not (exclude_generated_at and key == "generated_at")
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_tree(item, exclude_generated_at=exclude_generated_at) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "non-finite floats are not canonical")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    _fail(ResearchToolFailureCode.INVALID_PAYLOAD, f"unsupported canonical type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_tree(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _tuple_of_text(values: Sequence[str], field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, f"{field} is required", field=field)
    normalized: list[str] = []
    for item in values:
        _require_text(item, field)
        normalized.append(str(item))
    return tuple(sorted(dict.fromkeys(normalized)))


def _coerce_payload_dict(payload: Mapping[str, Any], required: Sequence[str], model_name: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _fail(ResearchToolFailureCode.INVALID_PAYLOAD, f"{model_name} payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, f"{model_name} schema_version mismatch")
    missing = [field for field in required if field not in payload]
    if missing:
        _fail(
            ResearchToolFailureCode.MISSING_REQUIRED_FIELD,
            f"{model_name} missing required fields",
            fields=tuple(missing),
        )
    return dict(payload)


@dataclass(frozen=True, kw_only=True)
class ResearchToolRequest:
    campaign_id: str
    campaign_hash: str
    desk_id: str
    decision_time: datetime
    allowed_data: tuple[str, ...]
    tool_name: str
    request_id: str
    offline: bool
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, "unsupported request schema")
        _require_identifier(self.campaign_id, "campaign_id")
        _require_hash(self.campaign_hash, "campaign_hash")
        _require_identifier(self.desk_id, "desk_id")
        _require_datetime(self.decision_time, "decision_time")
        object.__setattr__(self, "allowed_data", _tuple_of_text(self.allowed_data, "allowed_data"))
        _require_tool_name(self.tool_name)
        _require_identifier(self.request_id, "request_id")
        if type(self.offline) is not bool:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "offline must be a boolean", field="offline")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResearchToolRequest":
        raw = _coerce_payload_dict(
            payload,
            (
                "campaign_id",
                "campaign_hash",
                "desk_id",
                "decision_time",
                "allowed_data",
                "tool_name",
                "request_id",
                "offline",
            ),
            "ResearchToolRequest",
        )
        return cls(
            campaign_id=raw["campaign_id"],
            campaign_hash=raw["campaign_hash"],
            desk_id=raw["desk_id"],
            decision_time=_parse_datetime(raw["decision_time"], "decision_time"),
            allowed_data=tuple(raw["allowed_data"]),
            tool_name=raw["tool_name"],
            request_id=raw["request_id"],
            offline=raw["offline"],
            schema_version=raw["schema_version"],
        )

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class SourceProvenance:
    event_time: datetime
    published_at: datetime
    available_at: datetime
    retrieved_at: datetime
    provider: str
    source_class: str
    provider_artifact_id: str
    source_hash: str
    content_hash: str
    manifest_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, "unsupported provenance schema")
        for field_name in ("event_time", "published_at", "available_at", "retrieved_at"):
            _require_datetime(getattr(self, field_name), field_name)
        _assert_time_order("event_time", self.event_time, "published_at", self.published_at)
        _assert_time_order("published_at", self.published_at, "available_at", self.available_at)
        _assert_time_order("available_at", self.available_at, "retrieved_at", self.retrieved_at)
        for field_name in ("provider", "source_class", "provider_artifact_id"):
            _require_identifier(getattr(self, field_name), field_name)
        for field_name in ("source_hash", "content_hash", "manifest_hash"):
            _require_hash(getattr(self, field_name), field_name)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceProvenance":
        raw = _coerce_payload_dict(
            payload,
            (
                "event_time",
                "published_at",
                "available_at",
                "retrieved_at",
                "provider",
                "source_class",
                "provider_artifact_id",
                "source_hash",
                "content_hash",
                "manifest_hash",
            ),
            "SourceProvenance",
        )
        return cls(
            event_time=_parse_datetime(raw["event_time"], "event_time"),
            published_at=_parse_datetime(raw["published_at"], "published_at"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            retrieved_at=_parse_datetime(raw["retrieved_at"], "retrieved_at"),
            provider=raw["provider"],
            source_class=raw["source_class"],
            provider_artifact_id=raw["provider_artifact_id"],
            source_hash=raw["source_hash"],
            content_hash=raw["content_hash"],
            manifest_hash=raw["manifest_hash"],
            schema_version=raw["schema_version"],
        )

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FrozenResearchArtifact:
    artifact_id: str
    artifact_type: str
    provenance: SourceProvenance
    payload: Mapping[str, Any]
    payload_hash: str
    generated_at: datetime | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, "unsupported artifact schema")
        _require_identifier(self.artifact_id, "artifact_id")
        _require_identifier(self.artifact_type, "artifact_type")
        if not isinstance(self.provenance, SourceProvenance):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "provenance must be SourceProvenance")
        if not isinstance(self.payload, Mapping):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "payload must be a mapping")
        _assert_no_forbidden_visible_payload(self.payload)
        expected = content_hash(self.payload)
        if self.payload_hash != expected:
            _fail(
                ResearchToolFailureCode.INVALID_HASH,
                "payload_hash does not match canonical payload",
                expected=expected,
                actual=self.payload_hash,
            )
        if self.generated_at is not None:
            _require_datetime(self.generated_at, "generated_at")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class CitationBundle:
    source_hash: str
    chunk_hash: str
    span_start: int
    span_end: int
    available_at: datetime
    claim: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_hash(self.source_hash, "source_hash")
        _require_hash(self.chunk_hash, "chunk_hash")
        if self.span_start < 0 or self.span_end <= self.span_start:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "citation span is invalid")
        _require_datetime(self.available_at, "available_at")
        _require_text(self.claim, "claim")
        _assert_no_forbidden_visible_payload({"claim": self.claim})

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class MarketEventSummary:
    symbol: str
    event_type: str
    event_time: datetime
    available_at: datetime
    summary: str
    provenance_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.symbol, "symbol")
        _require_identifier(self.event_type, "event_type")
        _require_datetime(self.event_time, "event_time")
        _require_datetime(self.available_at, "available_at")
        _assert_time_order("event_time", self.event_time, "available_at", self.available_at)
        _require_text(self.summary, "summary")
        _assert_no_forbidden_visible_payload({"summary": self.summary})
        _require_hash(self.provenance_hash, "provenance_hash")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FinancialAuditFinding:
    finding_id: str
    severity: str
    reason_code: str
    available_at: datetime
    summary: str
    evidence_hashes: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.finding_id, "finding_id")
        _require_identifier(self.severity, "severity")
        _require_identifier(self.reason_code, "reason_code")
        _require_datetime(self.available_at, "available_at")
        _require_text(self.summary, "summary")
        _assert_no_forbidden_visible_payload({"summary": self.summary})
        if not self.evidence_hashes:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "evidence_hashes is required")
        for item in self.evidence_hashes:
            _require_hash(item, "evidence_hashes")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class DataAvailabilitySummary:
    dataset_id: str
    universe: str
    decision_time: datetime
    field_coverage: Mapping[str, float]
    missing_fields: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.dataset_id, "dataset_id")
        _require_identifier(self.universe, "universe")
        _require_datetime(self.decision_time, "decision_time")
        if not isinstance(self.field_coverage, Mapping) or not self.field_coverage:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "field_coverage is required")
        for field_name, value in self.field_coverage.items():
            _require_identifier(field_name, "field_coverage field")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "coverage must be finite in [0, 1]", field=field_name)
        object.__setattr__(self, "missing_fields", tuple(sorted(self.missing_fields)))

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class VisibleEvidenceSummary:
    candidate_id: str
    evidence_hash: str
    split: str
    reason_codes: tuple[str, ...]
    summary: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.candidate_id, "candidate_id")
        _require_hash(self.evidence_hash, "evidence_hash")
        if self.split not in {"train", "validation"}:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "visible evidence split must be train or validation")
        if not self.reason_codes:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "reason_codes is required")
        for code in self.reason_codes:
            _require_identifier(code, "reason_code")
        _require_text(self.summary, "summary")
        _assert_no_forbidden_visible_payload({"summary": self.summary, "reason_codes": self.reason_codes})

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class ExperimentDesignPlan:
    plan_id: str
    hypothesis_id: str
    horizon: str
    universe: str
    decision_time: datetime
    planned_tests: tuple[str, ...]
    official_evidence_requested: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.plan_id, "plan_id")
        _require_identifier(self.hypothesis_id, "hypothesis_id")
        _require_identifier(self.horizon, "horizon")
        _require_identifier(self.universe, "universe")
        _require_datetime(self.decision_time, "decision_time")
        if not self.planned_tests:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "planned_tests is required")
        for test_id in self.planned_tests:
            _require_identifier(test_id, "planned_tests")
        if type(self.official_evidence_requested) is not bool:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "official_evidence_requested must be boolean")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class ResearchToolResult:
    request: ResearchToolRequest
    result_id: str
    visibility: ResearchToolVisibility
    capability: ResearchToolCapability
    payload: Mapping[str, Any]
    artifacts: tuple[FrozenResearchArtifact, ...] = ()
    failure_code: ResearchToolFailureCode | None = None
    message: str = ""
    generated_at: datetime | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, "unsupported result schema")
        if not isinstance(self.request, ResearchToolRequest):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "request must be ResearchToolRequest")
        _require_identifier(self.result_id, "result_id")
        if not isinstance(self.visibility, ResearchToolVisibility):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "invalid result visibility")
        if not isinstance(self.capability, ResearchToolCapability):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "invalid result capability")
        if not self.capability.is_read_only or self.capability.is_privileged_financial_authority:
            _fail(ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY, "research tools must be read-only")
        if self.request.offline and self.capability is ResearchToolCapability.NETWORK_PROVIDER:
            _fail(ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED, "offline requests cannot use a network provider")
        if not isinstance(self.payload, Mapping):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "payload must be a mapping")
        _assert_no_forbidden_visible_payload(self.payload)
        for artifact in self.artifacts:
            if not isinstance(artifact, FrozenResearchArtifact):
                _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "artifacts must be FrozenResearchArtifact")
            if artifact.provenance.available_at > self.request.decision_time:
                _fail(
                    ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF,
                    "artifact is not available at decision_time",
                    artifact_id=artifact.artifact_id,
                )
        if self.failure_code is not None and not isinstance(self.failure_code, ResearchToolFailureCode):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "failure_code is invalid")
        if self.message:
            _assert_no_forbidden_visible_payload({"message": self.message})
        if self.generated_at is not None:
            _require_datetime(self.generated_at, "generated_at")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class DeskToolPolicy:
    campaign_id: str
    campaign_hash: str
    desk_id: str
    allowed_data: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    tool_capabilities: Mapping[str, tuple[ResearchToolCapability, ...]]
    offline_required: bool = True
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(ResearchToolFailureCode.INVALID_SCHEMA_VERSION, "unsupported policy schema")
        _require_identifier(self.campaign_id, "campaign_id")
        _require_hash(self.campaign_hash, "campaign_hash")
        _require_identifier(self.desk_id, "desk_id")
        object.__setattr__(self, "allowed_data", _tuple_of_text(self.allowed_data, "allowed_data"))
        if not self.allowed_tools:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "allowed_tools is required")
        for tool in self.allowed_tools:
            _require_tool_name(tool)
        if not isinstance(self.tool_capabilities, Mapping):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "tool_capabilities must be a mapping")
        for tool, capabilities in self.tool_capabilities.items():
            _require_tool_name(tool)
            if tool not in self.allowed_tools:
                _fail(ResearchToolFailureCode.UNAUTHORIZED_TOOL, "capability declared for undeclared tool", tool=tool)
            if not capabilities:
                _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "tool capability is required", tool=tool)
            for capability in capabilities:
                if not isinstance(capability, ResearchToolCapability):
                    _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "invalid tool capability", tool=tool)
                if not capability.is_read_only or capability.is_privileged_financial_authority:
                    _fail(ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY, "privileged tool capability is forbidden", tool=tool)

    def authorize(self, request: ResearchToolRequest, capability: ResearchToolCapability) -> None:
        if request.campaign_id != self.campaign_id or request.campaign_hash != self.campaign_hash:
            _fail(ResearchToolFailureCode.CAMPAIGN_MISMATCH, "request campaign does not match policy")
        if request.desk_id != self.desk_id:
            _fail(ResearchToolFailureCode.DESK_MISMATCH, "request desk does not match policy")
        if request.tool_name not in self.allowed_tools:
            _fail(ResearchToolFailureCode.UNAUTHORIZED_TOOL, "request tool is not allowed", tool=request.tool_name)
        if set(request.allowed_data) - set(self.allowed_data):
            _fail(ResearchToolFailureCode.ALLOWED_DATA_MISMATCH, "request allowed_data exceeds policy")
        capabilities = self.tool_capabilities.get(request.tool_name, ())
        if capability not in capabilities:
            _fail(ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY, "capability is not allowed for tool")
        if self.offline_required and not request.offline:
            _fail(ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED, "policy requires offline request")
        if request.offline and capability is ResearchToolCapability.NETWORK_PROVIDER:
            _fail(ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED, "offline request cannot use network provider")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)
