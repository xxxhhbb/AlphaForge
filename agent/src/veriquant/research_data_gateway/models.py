"""Stable, credential-free contracts for the AlphaForge research data gateway."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "alphaforge.research-data-gateway/v1"


class GatewayStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class DataAuthority(str, Enum):
    OFFICIAL_INPUT = "OFFICIAL_INPUT"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"


class GatewayFailureCode(str, Enum):
    NETWORK_DISABLED = "NETWORK_DISABLED"
    REFERENCE_DATA_UNAVAILABLE = "REFERENCE_DATA_UNAVAILABLE"
    HISTORICAL_UNIVERSE_EMPTY = "HISTORICAL_UNIVERSE_EMPTY"
    REQUIRED_SOURCE_FAILED = "REQUIRED_SOURCE_FAILED"
    MANIFEST_CORRUPT = "MANIFEST_CORRUPT"
    CAMPAIGN_DATASET_UNAVAILABLE = "CAMPAIGN_DATASET_UNAVAILABLE"
    AUXILIARY_TOOL_UNAVAILABLE = "AUXILIARY_TOOL_UNAVAILABLE"
    AUXILIARY_RESPONSE_INVALID = "AUXILIARY_RESPONSE_INVALID"
    SECRET_LEAKAGE = "SECRET_LEAKAGE"


def canonical_hash(value: Any) -> str:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value if isinstance(item, Enum) else item,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchDataGatewayRequest:
    start_date: str
    end_date: str
    index_codes: tuple[str, ...] = ("000001.SH", "399001.SZ")
    label_horizon_sessions: int = 5
    allow_network: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if len(self.start_date) != 8 or not self.start_date.isdigit():
            raise ValueError("start_date must be YYYYMMDD")
        if len(self.end_date) != 8 or not self.end_date.isdigit():
            raise ValueError("end_date must be YYYYMMDD")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not follow end_date")
        indexes = tuple(sorted(set(self.index_codes)))
        if not indexes or any(not item or "." not in item for item in indexes):
            raise ValueError("index_codes must be non-empty market identifiers")
        object.__setattr__(self, "index_codes", indexes)
        if type(self.allow_network) is not bool:
            raise ValueError("allow_network must be boolean")
        if isinstance(self.label_horizon_sessions, bool) or self.label_horizon_sessions < 1:
            raise ValueError("label_horizon_sessions must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class DataSourceReceipt:
    source_id: str
    authority: DataAuthority
    status: str
    row_count: int
    content_hash: str | None
    failure_code: GatewayFailureCode | None = None

    def __post_init__(self) -> None:
        if not self.source_id or any(token in self.source_id for token in ("/", "\\", "..")):
            raise ValueError("invalid source_id")
        if self.row_count < 0:
            raise ValueError("row_count cannot be negative")
        if self.status in {"SUCCESS", "EMPTY"} and self.content_hash is None:
            raise ValueError("terminal source receipt requires content_hash")
        if self.authority is DataAuthority.AUXILIARY_RESEARCH_EVIDENCE and self.status == "OFFICIAL":
            raise ValueError("auxiliary data cannot masquerade as official")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "authority": self.authority.value,
            "status": self.status,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
            "failure_code": self.failure_code.value if self.failure_code else None,
        }


@dataclass(frozen=True, slots=True)
class ResearchDataGatewayManifest:
    request_hash: str
    status: GatewayStatus
    source_label: str
    official_tushare: bool
    historical_symbol_count: int
    historical_symbols_hash: str
    calendar_exchanges: tuple[str, ...]
    official_freeze_hash: str | None
    campaign_dataset_hash: str | None
    receipts: tuple[DataSourceReceipt, ...]
    failures: tuple[GatewayFailureCode, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.source_label != "APPROVED_PROXY_DATA_VENDOR" or self.official_tushare:
            raise ValueError("gateway source identity violates approved proxy policy")
        if self.historical_symbol_count < 0:
            raise ValueError("historical_symbol_count cannot be negative")
        if self.calendar_exchanges != ("SSE", "SZSE"):
            raise ValueError("both frozen SSE and SZSE calendars are required")
        ids = tuple(item.source_id for item in self.receipts)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("receipts must be unique and stable-sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "source_label": self.source_label,
            "official_tushare": self.official_tushare,
            "historical_symbol_count": self.historical_symbol_count,
            "historical_symbols_hash": self.historical_symbols_hash,
            "calendar_exchanges": list(self.calendar_exchanges),
            "official_freeze_hash": self.official_freeze_hash,
            "campaign_dataset_hash": self.campaign_dataset_hash,
            "receipts": [item.to_dict() for item in self.receipts],
            "failures": [item.value for item in self.failures],
        }

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResearchDataGatewayResult:
    manifest: ResearchDataGatewayManifest
    manifest_path: str
    official_freeze_root: str | None
    campaign_dataset_hash: str | None
    auxiliary_manifest_hash: str | None


__all__ = [
    "DataAuthority", "DataSourceReceipt", "GatewayFailureCode", "GatewayStatus",
    "ResearchDataGatewayManifest", "ResearchDataGatewayRequest",
    "ResearchDataGatewayResult", "SCHEMA_VERSION", "canonical_hash",
]
