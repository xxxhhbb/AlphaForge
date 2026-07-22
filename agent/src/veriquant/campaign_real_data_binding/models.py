"""Bind a Campaign to a verified approved-proxy frozen A-share snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..research_closure_contracts import (
    APPROVED_PROXY_VENDOR_LABEL,
    ResearchClosureFailureCode,
    ResearchabilityStatus,
    ResearchabilityVerdict,
    VerifiedDatasetReceipt,
)


CAMPAIGN_REAL_DATA_BINDING_SCHEMA_VERSION = "alphaforge.campaign-real-data-binding/v1"
APPROVED_PROXY_SNAPSHOT_ID = "tushare-proxy-operational-smoke"
PINNED_SNAPSHOT_HASH = "sha256:dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a"
PINNED_QUALIFICATION_HASH = "sha256:9ba9b4bca0e4592a1141dd3d8bcf41b0672ee698eadd9129b5e9f4af6f68af65"
PINNED_AUXILIARY_RESULT_HASH = "sha256:47c517aeb39ecf212f5cd34819ed1728a4d8abd6404891f8c20bd4a68b0fc88e"


class RealDataBindingStatus(str, Enum):
    BOUND = "BOUND"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class RealDataBindingRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_REAL_DATA_BINDING_SCHEMA_VERSION

    request_id: str
    campaign_hash: str
    question_hash: str
    snapshot_id: str = APPROVED_PROXY_SNAPSHOT_ID
    provider_source_label: str = APPROVED_PROXY_VENDOR_LABEL
    expected_snapshot_hash: str = PINNED_SNAPSHOT_HASH
    required_fields: tuple[str, ...] = ("close", "volume")

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.question_hash, "question_hash")
        require_identifier(self.snapshot_id, "snapshot_id")
        if self.snapshot_id != APPROVED_PROXY_SNAPSHOT_ID:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unknown or unapproved snapshot id")
        require_identifier(self.provider_source_label, "provider_source_label")
        if self.provider_source_label != APPROVED_PROXY_VENDOR_LABEL or "OFFICIAL_TUSHARE" in self.provider_source_label:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "approved proxy data must not be labeled official Tushare")
        require_hash(self.expected_snapshot_hash, "expected_snapshot_hash")
        if not isinstance(self.required_fields, tuple) or not self.required_fields:
            fail(FailureCode.INVALID_VALUE, "required_fields must be a non-empty tuple")
        for field in self.required_fields:
            require_identifier(field, "required_fields")


@dataclass(frozen=True, kw_only=True)
class RealDataBindingResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_REAL_DATA_BINDING_SCHEMA_VERSION

    request_hash: str
    status: RealDataBindingStatus
    receipt: VerifiedDatasetReceipt | None
    researchability: ResearchabilityVerdict
    message: str

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if isinstance(self.status, str):
            object.__setattr__(self, "status", RealDataBindingStatus(self.status))
        if self.status is RealDataBindingStatus.BOUND:
            if self.receipt is None:
                fail(FailureCode.INCOMPLETE_RESULT, "bound real data result requires receipt")
            if self.researchability.status is not ResearchabilityStatus.RESEARCHABLE:
                fail(FailureCode.INVALID_STATE, "bound data must be researchable")
        elif self.receipt is not None:
            fail(FailureCode.INVALID_STATE, "non-bound real data result cannot carry receipt")


class ApprovedProxyRealDataBinder:
    """Verify the pinned approved-proxy frozen snapshot and issue a receipt."""

    def __init__(self, *, repo_root: Path | None = None) -> None:
        root = repo_root or Path(__file__).resolve().parents[4]
        self.repo_root = root.resolve()

    def bind(self, request: RealDataBindingRequest) -> RealDataBindingResult:
        manifest_path = self._manifest_path(request.snapshot_id)
        qualification_path = self.repo_root / ".autobuild/veriquant/integration/v1_real_data/approved_proxy_qualification.json"
        auxiliary_path = self.repo_root / ".autobuild/veriquant/integration/v1_real_data/real_auxiliary_smoke.json"
        manifest = _read_json(manifest_path)
        qualification = _read_json(qualification_path)
        auxiliary = _read_json(auxiliary_path)
        self._verify_hashes(request, manifest, qualification, auxiliary)
        dataset_card = manifest["dataset_card"]
        fields = tuple(dataset_card["fields"])
        missing = tuple(field for field in request.required_fields if field not in fields)
        if missing:
            return self._blocked(
                request,
                failure=ResearchClosureFailureCode.MISSING_DATA_SCOPE,
                message=f"缺少字段：{', '.join(missing)}",
            )
        coverage = {field: float(manifest["coverage"].get(field, 0.0)) for field in fields}
        if any(coverage[field] <= 0.0 for field in request.required_fields):
            return self._blocked(
                request,
                failure=ResearchClosureFailureCode.DATASET_NOT_VERIFIED,
                message="必需字段覆盖率不足",
            )
        request_payload = manifest["request"]
        receipt = VerifiedDatasetReceipt(
            receipt_id=f"dataset.{request.snapshot_id}",
            provider_source_label=APPROVED_PROXY_VENDOR_LABEL,
            official_tushare_source=False,
            snapshot_hash=manifest["snapshot_hash"],
            manifest_hash=manifest["manifest_hash"],
            calendar_hash=qualification["calendar_hash"],
            universe_hash=qualification["universe_hash"],
            content_hash=manifest["raw_content_hash"],
            calendar_id=manifest["trading_calendar"]["calendar_id"],
            universe=tuple(dataset_card["universe"]),
            fields=fields,
            row_count=int(dataset_card["record_count"]),
            session_count=_session_count(dataset_card),
            symbol_count=len(tuple(dataset_card["universe"])),
            decision_time=_parse_time(dataset_card["decision_time"]),
            start_time=_parse_time(request_payload["start_time"]),
            end_time=_parse_time(request_payload["end_time"]),
            coverage_by_field=coverage,
            research_visible_summary_hash=request.artifact_hash,
        )
        researchability = ResearchabilityVerdict(
            verdict_id=f"researchability.{request.snapshot_id}",
            status=ResearchabilityStatus.RESEARCHABLE,
            question_hash=request.question_hash,
            dataset_ready=True,
            calendar_id=receipt.calendar_id,
            universe=receipt.universe,
            horizon=dataset_card["horizon"],
            decision_time=receipt.decision_time,
            available_fields=receipt.fields,
            message="真实数据版本已绑定，可进入机器验证。",
        )
        return RealDataBindingResult(
            request_hash=request.artifact_hash,
            status=RealDataBindingStatus.BOUND,
            receipt=receipt,
            researchability=researchability,
            message="真实数据版本绑定完成",
        )

    def _manifest_path(self, snapshot_id: str) -> Path:
        if snapshot_id != APPROVED_PROXY_SNAPSHOT_ID:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unapproved snapshot id")
        target = (
            self.repo_root
            / ".autobuild/veriquant/integration/v1_tushare_proxy_stage/snapshots"
            / snapshot_id
            / PINNED_SNAPSHOT_HASH.removeprefix("sha256:")
            / "manifest.json"
        ).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("manifest path traversal rejected") from exc
        return target

    def _verify_hashes(
        self,
        request: RealDataBindingRequest,
        manifest: dict[str, object],
        qualification: dict[str, object],
        auxiliary: dict[str, object],
    ) -> None:
        if qualification.get("artifact_hash") != PINNED_QUALIFICATION_HASH:
            fail(FailureCode.INVALID_HASH, "approved proxy qualification hash changed")
        if auxiliary.get("artifact_hash") != PINNED_AUXILIARY_RESULT_HASH:
            fail(FailureCode.INVALID_HASH, "approved proxy auxiliary result hash changed")
        if qualification.get("provider_class") != APPROVED_PROXY_VENDOR_LABEL:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider is not approved proxy")
        if qualification.get("official_tushare_source") is not False:
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy cannot claim official Tushare")
        if manifest.get("snapshot_hash") != request.expected_snapshot_hash:
            fail(FailureCode.INVALID_HASH, "snapshot hash mismatch")
        if manifest.get("snapshot_hash") != qualification.get("snapshot_hash"):
            fail(FailureCode.INVALID_HASH, "qualification snapshot hash mismatch")
        if manifest.get("snapshot_hash") != auxiliary.get("snapshot_hash"):
            fail(FailureCode.INVALID_HASH, "auxiliary snapshot hash mismatch")
        if manifest.get("dataset_card", {}).get("source") == "OFFICIAL_TUSHARE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "manifest source mislabels official Tushare")

    def _blocked(
        self,
        request: RealDataBindingRequest,
        *,
        failure: ResearchClosureFailureCode,
        message: str,
    ) -> RealDataBindingResult:
        return RealDataBindingResult(
            request_hash=request.artifact_hash,
            status=RealDataBindingStatus.BLOCKED,
            receipt=None,
            researchability=ResearchabilityVerdict(
                verdict_id=f"researchability.{request.snapshot_id}.blocked",
                status=ResearchabilityStatus.BLOCKED,
                question_hash=request.question_hash,
                dataset_ready=False,
                calendar_id=None,
                failure=failure,
                message=message,
            ),
            message=message,
        )


def _read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(FailureCode.INCOMPLETE_RESULT, "approved proxy artifact is unavailable", error=type(exc).__name__)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        fail(FailureCode.INVALID_TIMESTAMP, "timestamp must be string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _session_count(dataset_card: dict[str, object]) -> int:
    record_count = int(dataset_card["record_count"])
    universe = tuple(dataset_card["universe"])
    if not universe:
        fail(FailureCode.INVALID_VALUE, "dataset universe is empty")
    return record_count // len(universe)
