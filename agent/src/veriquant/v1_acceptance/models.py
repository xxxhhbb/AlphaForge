"""Typed, deterministic models for VeriQuant V1 mechanical acceptance.

This package records evidence about financial services.  It never performs
factor calculation, promotion, portfolio construction, execution, or ledger
mutation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.v1-acceptance/v2"


class AcceptanceStatus(str, Enum):
    IMPLEMENTED_AND_TESTED = "IMPLEMENTED_AND_TESTED"
    IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class V1GateId(str, Enum):
    UI_WORKBENCH_READONLY = "ui_workbench_readonly"
    OPERATOR_API_SECURITY = "operator_api_security"
    DATA_TO_PROMOTION_CHAIN = "data_to_promotion_chain"
    SHADOW_REPLAY_20_SESSION = "shadow_replay_20_session"
    SECURITY_SCAN = "security_scan"
    REPRODUCIBILITY_RESTART_RESUME = "reproducibility_restart_resume"
    DOCS_RUNBOOK_REPORT = "docs_runbook_report"
    # Compatibility name used by the earlier five-gate integration input.
    # It is an Enum alias, so the v2 evaluator still has exactly seven unique
    # gates and applies the full data-to-promotion proof requirements.
    REAL_ASHARE_SMOKE = "data_to_promotion_chain"


class DataSourceClass(str, Enum):
    OFFICIAL_TUSHARE = "OFFICIAL_TUSHARE"
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"
    AUXILIARY = "AUXILIARY"


class ArtifactKind(str, Enum):
    CODE = "CODE"
    DATA = "DATA"
    POLICY = "POLICY"
    TEST_LOG = "TEST_LOG"
    MANIFEST = "MANIFEST"
    REPORT = "REPORT"


class AcceptanceFailureCode(str, Enum):
    MISSING_GATE_EVIDENCE = "MISSING_GATE_EVIDENCE"
    DUPLICATE_GATE_EVIDENCE = "DUPLICATE_GATE_EVIDENCE"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    EMPTY_ARTIFACT = "EMPTY_ARTIFACT"
    INVALID_ARTIFACT_HASH = "INVALID_ARTIFACT_HASH"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    ARTIFACT_PATH_ESCAPE = "ARTIFACT_PATH_ESCAPE"
    MISSING_REQUIRED_ARTIFACT = "MISSING_REQUIRED_ARTIFACT"
    MISSING_TEST = "MISSING_TEST"
    EMPTY_TEST_LOG = "EMPTY_TEST_LOG"
    TEST_NOT_EXECUTED = "TEST_NOT_EXECUTED"
    TEST_FAILED = "TEST_FAILED"
    TEST_SKIP_OR_XFAIL = "TEST_SKIP_OR_XFAIL"
    FORGED_OR_INCONSISTENT_STATUS = "FORGED_OR_INCONSISTENT_STATUS"
    UNTRACEABLE_CLAIM = "UNTRACEABLE_CLAIM"
    MISSING_REQUIRED_FACT = "MISSING_REQUIRED_FACT"
    UI_WORKBENCH_NOT_READONLY = "UI_WORKBENCH_NOT_READONLY"
    FRONTEND_RUNTIME_NOT_RUN = "FRONTEND_RUNTIME_NOT_RUN"
    WORKBENCH_RUNTIME_NOT_STARTED = "WORKBENCH_RUNTIME_NOT_STARTED"
    OPERATOR_API_NOT_STARTED = "OPERATOR_API_NOT_STARTED"
    OPERATOR_API_CONTROL_MISSING = "OPERATOR_API_CONTROL_MISSING"
    OFFLINE_PIPELINE_INCOMPLETE = "OFFLINE_PIPELINE_INCOMPLETE"
    MANUAL_ARTIFACT_COPY = "MANUAL_ARTIFACT_COPY"
    SYNTHETIC_DATA_AS_REAL = "SYNTHETIC_DATA_AS_REAL"
    DATA_SOURCE_NOT_APPROVED = "DATA_SOURCE_NOT_APPROVED"
    APPROVED_PROXY_MASQUERADE_AS_OFFICIAL = "APPROVED_PROXY_MASQUERADE_AS_OFFICIAL"
    OFFICIAL_EVIDENCE_NOT_RUN = "OFFICIAL_EVIDENCE_NOT_RUN"
    PROMOTION_NOT_RUN = "PROMOTION_NOT_RUN"
    CHALLENGER_MISSING = "CHALLENGER_MISSING"
    REAL_SNAPSHOT_NOT_FROZEN = "REAL_SNAPSHOT_NOT_FROZEN"
    REAL_CALENDAR_OR_UNIVERSE_MISSING = "REAL_CALENDAR_OR_UNIVERSE_MISSING"
    REAL_FACTOR_RUN_MISSING = "REAL_FACTOR_RUN_MISSING"
    QLIB_AUXILIARY_AS_OFFICIAL = "QLIB_AUXILIARY_AS_OFFICIAL"
    SINGLE_DAY_REPLAY_AS_TWENTY_DAY = "SINGLE_DAY_REPLAY_AS_TWENTY_DAY"
    INSUFFICIENT_REPLAY_SESSIONS = "INSUFFICIENT_REPLAY_SESSIONS"
    REPLAY_INVARIANT_FAILED = "REPLAY_INVARIANT_FAILED"
    LIVE_ROUTE_PRESENT = "LIVE_ROUTE_PRESENT"
    SEALED_CONTENT_EXPOSED = "SEALED_CONTENT_EXPOSED"
    SECRET_OR_TOKEN_EXPOSURE = "SECRET_OR_TOKEN_EXPOSURE"
    PATH_TRAVERSAL_ALLOWED = "PATH_TRAVERSAL_ALLOWED"
    SECURITY_PROBE_FAILED = "SECURITY_PROBE_FAILED"
    RESUME_DRILL_MISSING = "RESUME_DRILL_MISSING"
    RECOVERY_INVARIANT_FAILED = "RECOVERY_INVARIANT_FAILED"
    DOCS_INCOMPLETE = "DOCS_INCOMPLETE"
    RUNBOOK_MISSING = "RUNBOOK_MISSING"
    REPORT_INCOMPLETE = "REPORT_INCOMPLETE"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _canonicalize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json(item))
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical serialization rejects non-finite floats")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    kind: ArtifactKind
    relative_path: str
    sha256: str
    required: bool = True


@dataclass(frozen=True)
class TestRunEvidence:
    test_id: str
    command: str
    log_artifact_id: str
    exit_code: int | None
    passed_count: int
    failed_count: int
    skipped_count: int = 0
    xfailed_count: int = 0
    declared_status: str = "PASSED"


@dataclass(frozen=True)
class TraceableClaim:
    claim_id: str
    statement: str
    code_artifact_ids: tuple[str, ...]
    data_artifact_ids: tuple[str, ...]
    policy_artifact_ids: tuple[str, ...]
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class V1GateEvidence:
    gate_id: V1GateId
    implementation_present: bool
    run_completed: bool
    available: bool = True
    deferred: bool = False
    declared_status: AcceptanceStatus = AcceptanceStatus.IMPLEMENTED_NOT_RUN
    artifacts: tuple[ArtifactReference, ...] = ()
    tests: tuple[TestRunEvidence, ...] = ()
    claims: tuple[TraceableClaim, ...] = ()
    required_artifact_ids: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)
    package_ids: tuple[str, ...] = ()
    reason: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class GateVerdict:
    gate_id: V1GateId
    status: AcceptanceStatus
    failure_codes: tuple[AcceptanceFailureCode, ...]
    reasons: tuple[str, ...]
    verified_artifact_ids: tuple[str, ...]
    evidence_hash: str
    earliest_blocker: str | None = None
    missing_evidence: tuple[str, ...] = ()
    data_source_class: DataSourceClass | None = None

    def to_dict(self) -> dict[str, Any]:
        return _canonicalize(self)


@dataclass(frozen=True)
class V1AcceptanceVerdict:
    gates: tuple[GateVerdict, ...]
    overall_status: AcceptanceStatus
    input_hash: str
    report_hash: str
    earliest_blocker: str | None = None
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def build(cls, *, gates: tuple[GateVerdict, ...], input_hash: str) -> "V1AcceptanceVerdict":
        ordered = tuple(sorted(gates, key=lambda item: item.gate_id.value))
        overall = (
            AcceptanceStatus.IMPLEMENTED_AND_TESTED
            if len(ordered) == len(V1GateId)
            and all(item.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for item in ordered)
            else AcceptanceStatus.FAILED
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "gates": [_canonicalize(item) for item in ordered],
            "overall_status": overall.value,
            "input_hash": input_hash,
        }
        earliest = next((item.earliest_blocker for item in ordered if item.earliest_blocker), None)
        return cls(
            gates=ordered,
            overall_status=overall,
            input_hash=input_hash,
            report_hash=canonical_hash(payload),
            earliest_blocker=earliest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overall_status": self.overall_status.value,
            "input_hash": self.input_hash,
            "report_hash": self.report_hash,
            "earliest_blocker": self.earliest_blocker,
            "gates": [item.to_dict() for item in self.gates],
        }
