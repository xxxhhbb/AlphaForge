"""Typed contracts for deterministic statistical promotion.

This package is intentionally self-contained for parallel development.  It can
be wired to the main Evidence Service later through the EvidenceReader protocol
without changing existing VeriQuant authority modules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import MISSING, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


SCHEMA_VERSION = "statistical-promotion/v1"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_FORBIDDEN_SEALED_TOKENS = (
    "sealed_metric",
    "sealed_rank",
    "sealed_ranking",
    "sealed_rows",
    "raw_sealed",
    "private_promotion_reason",
    "test_metric",
    "holdout_metric",
)


class StatisticalPromotionFailureCode(str, Enum):
    INVALID_CONTRACT = "INVALID_CONTRACT"
    INVALID_HASH = "INVALID_HASH"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    INVALID_METRIC = "INVALID_METRIC"
    INVALID_P_VALUE = "INVALID_P_VALUE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    UNAUTHORIZED_EVIDENCE_VIEW = "UNAUTHORIZED_EVIDENCE_VIEW"
    FAMILY_MANIFEST_MISMATCH = "FAMILY_MANIFEST_MISMATCH"
    DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
    MULTIPLE_TESTING_FAILED = "MULTIPLE_TESTING_FAILED"
    ROBUSTNESS_FAILED = "ROBUSTNESS_FAILED"
    COST_FAILED = "COST_FAILED"
    CAPACITY_FAILED = "CAPACITY_FAILED"
    COVERAGE_FAILED = "COVERAGE_FAILED"
    PARAMETER_SENSITIVITY_FAILED = "PARAMETER_SENSITIVITY_FAILED"
    FORGED_SEALED_HANDLE = "FORGED_SEALED_HANDLE"
    SEALED_REPLAY = "SEALED_REPLAY"
    SEALED_DECISION_REJECTED = "SEALED_DECISION_REJECTED"
    POLICY_TAMPERED = "POLICY_TAMPERED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    SELF_APPROVAL = "SELF_APPROVAL"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    THRESHOLD_TAMPERING = "THRESHOLD_TAMPERING"
    LEAKAGE_DETECTED = "LEAKAGE_DETECTED"


class StatisticalPromotionError(ValueError):
    """Fail-closed error with a typed promotion failure code."""

    def __init__(
        self,
        code: StatisticalPromotionFailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class PromotionState(str, Enum):
    EXPLORATORY = "EXPLORATORY"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"
    CHALLENGER = "CHALLENGER"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"


class PromotionVerdict(str, Enum):
    PROMOTE_CHALLENGER = "PROMOTE_CHALLENGER"
    REJECT = "REJECT"
    INCOMPLETE = "INCOMPLETE"


class MultipleTestingMethod(str, Enum):
    BENJAMINI_HOCHBERG = "BENJAMINI_HOCHBERG"
    FAMILY_BH_FDR = "FAMILY_BH_FDR"
    BONFERRONI = "BONFERRONI"
    HOLM = "HOLM"


ALLOWED_TRANSITIONS: Mapping[PromotionState, frozenset[PromotionState]] = {
    PromotionState.EXPLORATORY: frozenset(
        {PromotionState.REJECTED, PromotionState.INCOMPLETE}
    ),
    PromotionState.VALIDATED_RESEARCH: frozenset(
        {PromotionState.CHALLENGER, PromotionState.REJECTED, PromotionState.INCOMPLETE}
    ),
    PromotionState.CHALLENGER: frozenset(),
    PromotionState.REJECTED: frozenset(),
    PromotionState.INCOMPLETE: frozenset(),
}


def _fail(
    code: StatisticalPromotionFailureCode,
    message: str,
    **details: Any,
) -> None:
    raise StatisticalPromotionError(code, message, details=details)


def _require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(
            StatisticalPromotionFailureCode.INVALID_IDENTIFIER,
            f"{field_name} must be a stable identifier",
            field=field_name,
        )


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _fail(
            StatisticalPromotionFailureCode.INVALID_HASH,
            f"{field_name} must be sha256:<64 lowercase hex>",
            field=field_name,
        )


def _require_finite(value: float | int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(
            StatisticalPromotionFailureCode.INVALID_METRIC,
            f"{field_name} must be numeric",
            field=field_name,
        )
    if not math.isfinite(float(value)):
        _fail(
            StatisticalPromotionFailureCode.INVALID_METRIC,
            f"{field_name} must be finite",
            field=field_name,
        )


def _require_p_value(value: float, field_name: str) -> None:
    _require_finite(value, field_name)
    if not 0.0 <= float(value) <= 1.0:
        _fail(
            StatisticalPromotionFailureCode.INVALID_P_VALUE,
            f"{field_name} must be in [0, 1]",
            field=field_name,
        )


def _require_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(
            StatisticalPromotionFailureCode.INVALID_CONTRACT,
            f"{field_name} must be timezone-aware",
            field=field_name,
        )


def _freeze_numeric_mapping(
    value: Mapping[str, Any],
    field_name: str,
    *,
    p_value: bool = False,
) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        _fail(
            StatisticalPromotionFailureCode.INVALID_CONTRACT,
            f"{field_name} must be a mapping",
            field=field_name,
        )
    frozen: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                f"{field_name} keys must be non-empty strings",
                field=field_name,
            )
        lowered = key.lower()
        if any(token in lowered for token in _FORBIDDEN_SEALED_TOKENS):
            _fail(
                StatisticalPromotionFailureCode.LEAKAGE_DETECTED,
                f"{field_name} contains sealed or private material",
                field=field_name,
                key=key,
            )
        if p_value:
            _require_p_value(float(item), f"{field_name}.{key}")
        else:
            _require_finite(item, f"{field_name}.{key}")
        frozen[key] = float(item)
    return MappingProxyType(frozen)


def _freeze_partition_metrics(
    value: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, float]]:
    if not isinstance(value, Mapping):
        _fail(
            StatisticalPromotionFailureCode.INVALID_CONTRACT,
            "visible_metrics must be a mapping",
        )
    allowed = {"TRAIN", "VALIDATION"}
    frozen: dict[str, Mapping[str, float]] = {}
    for partition, metrics in value.items():
        if partition not in allowed:
            _fail(
                StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
                "only public train/validation evidence may be exposed",
                partition=partition,
            )
        frozen[partition] = _freeze_numeric_mapping(metrics, f"visible_metrics.{partition}")
    return MappingProxyType(frozen)


def _canonical_datetime(value: datetime) -> str:
    _require_datetime(value, "datetime")
    utc_value = value.astimezone(timezone.utc)
    rendered = utc_value.isoformat(timespec="microseconds")
    return rendered[:-6] + "Z"


def canonicalize(value: Any) -> Any:
    if isinstance(value, PromotionContract):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, float):
        _require_finite(value, "float")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        normalized = {str(key): canonicalize(item) for key, item in value.items()}
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if is_dataclass(value):
        return {item.name: canonicalize(getattr(value, item.name)) for item in fields(value)}
    _fail(
        StatisticalPromotionFailureCode.INVALID_CONTRACT,
        f"unsupported canonical type {type(value).__name__}",
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class PromotionContract:
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    @property
    def contract_type(self) -> str:
        return type(self).__name__

    @property
    def schema_version(self) -> str:
        return self.SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "PromotionContract subclasses must be dataclasses",
            )
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            **{
                item.name: canonicalize(getattr(self, item.name))
                for item in fields(self)
                if item.init or item.default is not MISSING
            },
        }

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, kw_only=True)
class CandidateVersionRef(PromotionContract):
    candidate_id: str
    candidate_version: str

    def __post_init__(self) -> None:
        _require_identifier(self.candidate_id, "candidate_id")
        _require_identifier(self.candidate_version, "candidate_version")

    @property
    def key(self) -> str:
        return f"{self.candidate_id}@{self.candidate_version}"


@dataclass(frozen=True, kw_only=True)
class CandidateEvidenceView(PromotionContract):
    candidate_id: str
    candidate_version: str
    family_id: str
    family_manifest_hash: str
    identity_hash: str
    evidence_hash: str
    input_hash: str
    proposer_id: str
    state: PromotionState
    visible_metrics: Mapping[str, Mapping[str, float]]
    robustness_metrics: Mapping[str, float]
    p_value: float
    produced_by: str = "EVIDENCE_SERVICE"
    evidence_class: str = "OFFICIAL_EVIDENCE"

    def __post_init__(self) -> None:
        for field_name in ("candidate_id", "candidate_version", "family_id", "proposer_id"):
            _require_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "family_manifest_hash",
            "identity_hash",
            "evidence_hash",
            "input_hash",
        ):
            _require_hash(getattr(self, field_name), field_name)
        if self.state not in PromotionState:
            _fail(StatisticalPromotionFailureCode.INVALID_CONTRACT, "unknown state")
        if self.produced_by != "EVIDENCE_SERVICE" or self.evidence_class != "OFFICIAL_EVIDENCE":
            _fail(
                StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
                "promotion accepts only official Evidence Service summaries",
            )
        _require_p_value(self.p_value, "p_value")
        object.__setattr__(
            self,
            "visible_metrics",
            _freeze_partition_metrics(self.visible_metrics),
        )
        object.__setattr__(
            self,
            "robustness_metrics",
            _freeze_numeric_mapping(self.robustness_metrics, "robustness_metrics"),
        )

    @property
    def key(self) -> str:
        return f"{self.candidate_id}@{self.candidate_version}"


@dataclass(frozen=True, kw_only=True)
class FactorFamilyManifest(PromotionContract):
    family_id: str
    manifest_version: str
    frozen_at: datetime
    candidate_versions: tuple[CandidateVersionRef, ...]
    created_by: str = "FAMILY_REGISTRY"

    def __post_init__(self) -> None:
        _require_identifier(self.family_id, "family_id")
        _require_identifier(self.manifest_version, "manifest_version")
        _require_datetime(self.frozen_at, "frozen_at")
        if self.created_by != "FAMILY_REGISTRY":
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "family manifests must come from the frozen registry",
            )
        if not isinstance(self.candidate_versions, tuple) or not self.candidate_versions:
            _fail(
                StatisticalPromotionFailureCode.MISSING_EVIDENCE,
                "family manifest must bind at least one candidate",
            )
        seen: set[str] = set()
        for item in self.candidate_versions:
            if not isinstance(item, CandidateVersionRef):
                _fail(
                    StatisticalPromotionFailureCode.INVALID_CONTRACT,
                    "candidate_versions must contain CandidateVersionRef",
                )
            if item.key in seen:
                _fail(
                    StatisticalPromotionFailureCode.DUPLICATE_CANDIDATE,
                    "duplicate candidate/version in frozen family manifest",
                    candidate=item.key,
                )
            seen.add(item.key)

    def contains(self, view: CandidateEvidenceView) -> bool:
        return view.key in {item.key for item in self.candidate_versions}


@dataclass(frozen=True, kw_only=True)
class RobustnessPolicy(PromotionContract):
    policy_id: str = "robustness-policy"
    policy_version: str = "1.0.0"
    min_observations: int = 20
    min_rank_ic: float = 0.0
    min_icir: float = 0.0
    min_subperiod_positive_fraction: float = 0.55
    min_subperiod_min_ic: float = -0.01
    min_regime_positive_fraction: float = 0.50
    min_regime_min_ic: float = -0.01
    max_abs_industry_exposure: float = 0.20
    max_abs_market_cap_exposure: float = 0.25
    max_turnover: float = 0.80
    min_net_return_after_cost: float = 0.0
    max_cost_sensitivity_loss: float = 0.025
    max_capacity_utilization: float = 0.20
    min_coverage: float = 0.85
    max_parameter_sensitivity: float = 0.20

    def __post_init__(self) -> None:
        _require_identifier(self.policy_id, "policy_id")
        _require_identifier(self.policy_version, "policy_version")
        if isinstance(self.min_observations, bool) or self.min_observations < 1:
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "min_observations must be positive",
            )
        for item in fields(self):
            if item.name in {"policy_id", "policy_version", "min_observations"}:
                continue
            _require_finite(getattr(self, item.name), item.name)


@dataclass(frozen=True, kw_only=True)
class MultipleTestingPolicy(PromotionContract):
    policy_id: str = "multiple-testing-policy"
    policy_version: str = "1.0.0"
    method: MultipleTestingMethod = MultipleTestingMethod.FAMILY_BH_FDR
    alpha: float = 0.05

    def __post_init__(self) -> None:
        _require_identifier(self.policy_id, "policy_id")
        _require_identifier(self.policy_version, "policy_version")
        if not isinstance(self.method, MultipleTestingMethod):
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "unknown multiple-testing method",
            )
        _require_p_value(self.alpha, "alpha")
        if self.alpha <= 0.0:
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "alpha must be positive",
            )


@dataclass(frozen=True, kw_only=True)
class PromotionDecision(PromotionContract):
    decision_id: str
    candidate_id: str
    candidate_version: str
    family_id: str
    from_state: PromotionState
    to_state: PromotionState
    verdict: PromotionVerdict
    reason_codes: tuple[str, ...]
    failure_code: StatisticalPromotionFailureCode | None
    proposer_id: str
    approver_id: str
    decided_at: datetime
    policy_version: str
    input_hash: str
    produced_by: str = "STATISTICAL_PROMOTION_GATE"
    redacted: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id",
            "candidate_id",
            "candidate_version",
            "family_id",
            "proposer_id",
            "approver_id",
            "policy_version",
        ):
            _require_identifier(getattr(self, field_name), field_name)
        if self.proposer_id == self.approver_id:
            _fail(
                StatisticalPromotionFailureCode.SELF_APPROVAL,
                "proposer cannot approve itself",
            )
        _require_hash(self.input_hash, "input_hash")
        _require_datetime(self.decided_at, "decided_at")
        if self.produced_by != "STATISTICAL_PROMOTION_GATE" or not self.redacted:
            _fail(
                StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
                "promotion decision must be redacted gate output",
            )
        if self.to_state not in ALLOWED_TRANSITIONS[self.from_state]:
            _fail(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "invalid promotion state transition",
                from_state=self.from_state.value,
                to_state=self.to_state.value,
            )
        if self.to_state is PromotionState.CHALLENGER and self.verdict is not PromotionVerdict.PROMOTE_CHALLENGER:
            _fail(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "only a promote verdict may produce CHALLENGER",
            )
        if not self.reason_codes:
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "promotion decision requires reason codes",
            )
        if any(
            token in canonical_json(self.reason_codes).lower()
            for token in ("sealed_metric", "sealed_rank", "raw_sealed", "private")
        ):
            _fail(
                StatisticalPromotionFailureCode.LEAKAGE_DETECTED,
                "decision reason codes leak sealed information",
            )

    @property
    def decision_hash(self) -> str:
        return self.artifact_hash


@dataclass(frozen=True, kw_only=True)
class PromotionAuditManifest(PromotionContract):
    audit_id: str
    decision_hash: str
    input_hash: str
    candidate_evidence_hash: str
    family_manifest_hash: str
    robustness_policy_hash: str
    multiple_testing_policy_hash: str
    policy_version: str
    idempotency_key_hash: str
    sealed_handle_hash: str | None
    reader_id: str
    gate_id: str
    status: str
    failure_code: StatisticalPromotionFailureCode | None
    produced_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("audit_id", "policy_version", "reader_id", "gate_id", "status"):
            _require_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "decision_hash",
            "input_hash",
            "candidate_evidence_hash",
            "family_manifest_hash",
            "robustness_policy_hash",
            "multiple_testing_policy_hash",
            "idempotency_key_hash",
        ):
            _require_hash(getattr(self, field_name), field_name)
        if self.sealed_handle_hash is not None:
            _require_hash(self.sealed_handle_hash, "sealed_handle_hash")
        _require_datetime(self.produced_at, "produced_at")
        if self.status not in {"PROMOTED", "REJECTED", "INCOMPLETE"}:
            _fail(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "unknown audit status",
            )

