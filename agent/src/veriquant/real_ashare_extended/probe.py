"""Fail-closed qualification for the larger real A-share V1 smoke target.

This package does not fetch data and does not calculate financial truth.  It is
the mechanical acceptance probe that an Integration Agent can run against a
frozen approved-proxy snapshot before it is allowed to claim the 30-symbol /
180-session real-data gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    data = json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


class AshareExtendedFailureCode(str, Enum):
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    APPROVED_PROXY_REQUIRED = "APPROVED_PROXY_REQUIRED"
    OFFICIAL_TUSHARE_MISLABEL = "OFFICIAL_TUSHARE_MISLABEL"
    INSUFFICIENT_SYMBOLS = "INSUFFICIENT_SYMBOLS"
    EXCESS_SYMBOLS = "EXCESS_SYMBOLS"
    INSUFFICIENT_SESSIONS = "INSUFFICIENT_SESSIONS"
    EXCESS_SESSIONS = "EXCESS_SESSIONS"
    MISSING_REQUIRED_FIELDS = "MISSING_REQUIRED_FIELDS"
    FUTURE_RETURN_AS_FEATURE = "FUTURE_RETURN_AS_FEATURE"
    FACTOR_COUNT_OUT_OF_RANGE = "FACTOR_COUNT_OUT_OF_RANGE"
    MISSING_REQUIRED_HASH = "MISSING_REQUIRED_HASH"
    TOKEN_OR_SECRET_LEAKAGE = "TOKEN_OR_SECRET_LEAKAGE"


@dataclass(frozen=True)
class AshareExtendedScaleTarget:
    min_symbols: int = 20
    max_symbols: int = 50
    min_sessions: int = 120
    max_sessions: int = 250
    min_factors: int = 1
    max_factors: int = 3
    required_fields: tuple[str, ...] = (
        "close",
        "volume",
        "amount",
        "adv",
        "market_cap",
        "industry",
        "benchmark_return",
    )


@dataclass(frozen=True)
class AshareExtendedSnapshotEvidence:
    data_source_class: str
    source_class: str
    official_tushare_source: bool
    symbol_count: int
    session_count: int
    row_count: int
    feature_fields: tuple[str, ...]
    label_fields: tuple[str, ...]
    factor_count: int
    calendar_hash: str
    universe_hash: str
    snapshot_hash: str
    manifest_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            canonical_hash({
                "data_source_class": self.data_source_class,
                "source_class": self.source_class,
                "official_tushare_source": self.official_tushare_source,
                "symbol_count": self.symbol_count,
                "session_count": self.session_count,
                "row_count": self.row_count,
                "feature_fields": self.feature_fields,
                "label_fields": self.label_fields,
                "factor_count": self.factor_count,
                "calendar_hash": self.calendar_hash,
                "universe_hash": self.universe_hash,
                "snapshot_hash": self.snapshot_hash,
                "manifest_hash": self.manifest_hash,
            }),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AshareExtendedSnapshotEvidence":
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), Mapping) else {}
        official = payload.get("official_evidence") if isinstance(payload.get("official_evidence"), Mapping) else {}
        symbols = payload.get("symbols", snapshot.get("symbols", ()))
        trading_days = payload.get("trading_days", payload.get("sessions", ()))
        row_count = int(payload.get("row_count", snapshot.get("row_count", 0)) or 0)
        symbol_count = int(payload.get("symbol_count", len(symbols) if isinstance(symbols, Sequence) and not isinstance(symbols, str) else 0) or 0)
        inferred_sessions = row_count // symbol_count if symbol_count and row_count else 0
        session_count = int(
            payload.get(
                "session_count",
                len(trading_days) if isinstance(trading_days, Sequence) and not isinstance(trading_days, str) else inferred_sessions,
            )
            or 0
        )
        feature_fields = tuple(str(item) for item in payload.get("feature_fields", payload.get("fields", ())) or ())
        label_fields = tuple(str(item) for item in payload.get("label_fields", ("future_return_5d",)) or ())
        return cls(
            data_source_class=str(payload.get("data_source_class", "")),
            source_class=str(payload.get("source_class", "")),
            official_tushare_source=bool(payload.get("official_tushare_source", False)),
            symbol_count=symbol_count,
            session_count=session_count,
            row_count=row_count,
            feature_fields=feature_fields,
            label_fields=label_fields,
            factor_count=int(payload.get("factor_count", official.get("factor_count", 0)) or 0),
            calendar_hash=str(payload.get("calendar_hash", snapshot.get("calendar_hash", official.get("calendar_hash", "")))),
            universe_hash=str(payload.get("universe_hash", snapshot.get("universe_hash", official.get("universe_hash", "")))),
            snapshot_hash=str(payload.get("snapshot_hash", snapshot.get("snapshot_hash", official.get("snapshot_hash", "")))),
            manifest_hash=str(payload.get("manifest_hash", snapshot.get("snapshot_manifest_hash", official.get("snapshot_manifest_hash", "")))),
        )


@dataclass(frozen=True)
class AshareExtendedVerdict:
    status: str
    evidence_hash: str
    failure_codes: tuple[AshareExtendedFailureCode, ...]
    message: str


class AshareExtendedScaleProbe:
    secret_pattern = re.compile(
        r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|api[_-]?key\s*[:=]|token\s*[:=]|authorization\s*:|secret\s*[:=])"
    )

    def __init__(self, target: AshareExtendedScaleTarget | None = None) -> None:
        self.target = target or AshareExtendedScaleTarget()

    def evaluate(self, payload: Mapping[str, Any] | None, *, log_text: str = "") -> AshareExtendedVerdict:
        if payload is None:
            return AshareExtendedVerdict(
                status="BLOCKED",
                evidence_hash=canonical_hash({"missing": True}),
                failure_codes=(AshareExtendedFailureCode.MISSING_ARTIFACT,),
                message="No extended real A-share snapshot artifact was supplied.",
            )
        evidence = AshareExtendedSnapshotEvidence.from_mapping(payload)
        failures: list[AshareExtendedFailureCode] = []
        if evidence.data_source_class != "APPROVED_PROXY_DATA_VENDOR" or evidence.source_class != "TUSHARE_COMPATIBLE_PROXY":
            failures.append(AshareExtendedFailureCode.APPROVED_PROXY_REQUIRED)
        if evidence.official_tushare_source:
            failures.append(AshareExtendedFailureCode.OFFICIAL_TUSHARE_MISLABEL)
        if evidence.symbol_count < self.target.min_symbols:
            failures.append(AshareExtendedFailureCode.INSUFFICIENT_SYMBOLS)
        if evidence.symbol_count > self.target.max_symbols:
            failures.append(AshareExtendedFailureCode.EXCESS_SYMBOLS)
        if evidence.session_count < self.target.min_sessions:
            failures.append(AshareExtendedFailureCode.INSUFFICIENT_SESSIONS)
        if evidence.session_count > self.target.max_sessions:
            failures.append(AshareExtendedFailureCode.EXCESS_SESSIONS)
        missing = set(self.target.required_fields) - set(evidence.feature_fields)
        if missing:
            failures.append(AshareExtendedFailureCode.MISSING_REQUIRED_FIELDS)
        if any("future_return" in name.lower() for name in evidence.feature_fields):
            failures.append(AshareExtendedFailureCode.FUTURE_RETURN_AS_FEATURE)
        if not (self.target.min_factors <= evidence.factor_count <= self.target.max_factors):
            failures.append(AshareExtendedFailureCode.FACTOR_COUNT_OUT_OF_RANGE)
        if not all(value.startswith("sha256:") for value in (evidence.calendar_hash, evidence.universe_hash, evidence.snapshot_hash, evidence.manifest_hash)):
            failures.append(AshareExtendedFailureCode.MISSING_REQUIRED_HASH)
        if self.secret_pattern.search(json.dumps(_normalize(payload), ensure_ascii=False)) or self.secret_pattern.search(log_text):
            failures.append(AshareExtendedFailureCode.TOKEN_OR_SECRET_LEAKAGE)
        unique = tuple(dict.fromkeys(failures))
        return AshareExtendedVerdict(
            status="READY" if not unique else "BLOCKED",
            evidence_hash=evidence.content_hash,
            failure_codes=unique,
            message="Extended real A-share target satisfied." if not unique else "Extended real A-share target is not satisfied.",
        )
