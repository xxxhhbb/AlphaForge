"""Typed outputs for mechanical Qlib migration acceptance.

The package verifies build evidence.  It never imports a factor, executes a
formula, calculates a score, or creates official evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.qlib-migration-acceptance/v1"


class AcceptanceStatus(str, Enum):
    IMPLEMENTED_AND_TESTED = "IMPLEMENTED_AND_TESTED"
    IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class QlibMigrationGate(str, Enum):
    CONSTITUTION_V2 = "constitution_v2"
    PACKAGE_READINESS = "package_readiness"
    CATALOG_CLASSIFICATION = "catalog_461_classification"
    WHOLE_FORMULA_QUALIFICATION = "whole_formula_qualification"
    REAL_PYQLIB_SMOKE = "real_pyqlib_smoke"
    FROZEN_PIT_PROVIDER = "frozen_pit_provider"
    UNIFIED_FACTOR_SCORE = "unified_factor_score_artifact"
    TRIAL_EVIDENCE_AUTHORITY = "trial_and_evidence_authority"
    KNOWN_DIFFERENCE_BLOCKING = "known_difference_blocking"
    POSTPROCESS_IDENTITY = "postprocess_identity"
    SECURITY_SCAN = "security_scan"
    DEDICATED_PROCESS = "dedicated_process_capability"
    SPECIALIST_QLIB_PRIMARY = "specialist_qlib_primary"
    LEGACY_REPLAY = "legacy_replay"


class AcceptanceFailureCode(str, Enum):
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    INVALID_JSON = "INVALID_JSON"
    INVALID_HASH = "INVALID_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"
    PATH_ESCAPE = "PATH_ESCAPE"
    PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
    TEST_MISSING = "TEST_MISSING"
    TEST_FAILED = "TEST_FAILED"
    CONSTITUTION_MISMATCH = "CONSTITUTION_MISMATCH"
    COVERAGE_MISMATCH = "COVERAGE_MISMATCH"
    REAL_SMOKE_MISSING = "REAL_SMOKE_MISSING"
    PIT_PROOF_MISSING = "PIT_PROOF_MISSING"
    UNIFIED_ARTIFACT_UNPROVEN = "UNIFIED_ARTIFACT_UNPROVEN"
    AUTHORITY_UNPROVEN = "AUTHORITY_UNPROVEN"
    KNOWN_DIFFERENCE_NOT_BLOCKED = "KNOWN_DIFFERENCE_NOT_BLOCKED"
    POSTPROCESS_IDENTITY_UNPROVEN = "POSTPROCESS_IDENTITY_UNPROVEN"
    SECURITY_INVARIANT_FAILED = "SECURITY_INVARIANT_FAILED"
    PROCESS_ISOLATION_UNAVAILABLE = "PROCESS_ISOLATION_UNAVAILABLE"
    SPECIALIST_PROMPT_REGRESSION = "SPECIALIST_PROMPT_REGRESSION"
    QLIB_PRIMARY_UNPROVEN = "QLIB_PRIMARY_UNPROVEN"
    LEGACY_REPLAY_UNPROVEN = "LEGACY_REPLAY_UNPROVEN"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateVerdict:
    gate: QlibMigrationGate
    status: AcceptanceStatus
    failure_codes: tuple[AcceptanceFailureCode, ...] = ()
    reasons: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    facts: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gate"] = self.gate.value
        payload["status"] = self.status.value
        payload["failure_codes"] = [item.value for item in self.failure_codes]
        payload["facts"] = dict(sorted((self.facts or {}).items()))
        return payload


@dataclass(frozen=True)
class QlibMigrationAcceptanceVerdict:
    gates: tuple[GateVerdict, ...]
    overall_status: AcceptanceStatus
    input_hash: str
    report_hash: str
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def build(cls, gates: tuple[GateVerdict, ...], input_hash: str) -> "QlibMigrationAcceptanceVerdict":
        ordered = tuple(sorted(gates, key=lambda item: item.gate.value))
        overall = (
            AcceptanceStatus.IMPLEMENTED_AND_TESTED
            if len(ordered) == len(QlibMigrationGate)
            and all(item.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for item in ordered)
            else AcceptanceStatus.FAILED
        )
        body = {
            "schema_version": SCHEMA_VERSION,
            "input_hash": input_hash,
            "overall_status": overall.value,
            "gates": [item.to_dict() for item in ordered],
        }
        return cls(
            gates=ordered,
            overall_status=overall,
            input_hash=input_hash,
            report_hash=canonical_hash(body),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_hash": self.input_hash,
            "overall_status": self.overall_status.value,
            "report_hash": self.report_hash,
            "gates": [item.to_dict() for item in self.gates],
        }
