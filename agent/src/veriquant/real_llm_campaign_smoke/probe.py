"""Fail-closed checks for a real provider Campaign smoke.

The probe validates evidence produced elsewhere.  It does not call the provider,
does not execute tools, and never grants Evidence, Promotion, Portfolio, Risk,
Execution, broker, order, fill, or ledger authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    payload = json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RealLLMSmokeFailureCode(str, Enum):
    NO_REAL_PROVIDER_CALL = "NO_REAL_PROVIDER_CALL"
    EXCLUDED_DESK_CALLED = "EXCLUDED_DESK_CALLED"
    UNSELECTED_CONTEXT_EXPOSED = "UNSELECTED_CONTEXT_EXPOSED"
    PRIVILEGED_OUTPUT_AUTHORITY = "PRIVILEGED_OUTPUT_AUTHORITY"
    SEALED_OR_SECRET_LEAKAGE = "SEALED_OR_SECRET_LEAKAGE"
    TIMEOUT_COLLAPSED_INDEPENDENT_DESKS = "TIMEOUT_COLLAPSED_INDEPENDENT_DESKS"
    RESUME_REPEATED_COMMITTED_STAGE = "RESUME_REPEATED_COMMITTED_STAGE"
    DETERMINISTIC_IMPERSONATED_REAL = "DETERMINISTIC_IMPERSONATED_REAL"


@dataclass(frozen=True)
class RealLLMSmokeVerdict:
    status: str
    evidence_hash: str
    failure_codes: tuple[RealLLMSmokeFailureCode, ...]
    message: str


class RealLLMSmokeProbe:
    forbidden_text = re.compile(
        r"(?i)(sealed[_ -]?(metric|test|rank|result)|封闭测试|api[_-]?key|authorization\s*:|bearer\s+[a-z0-9._~+/=-]{8,}|secret|official[_ -]?verdict|promotion[_ -]?decision|target[_ -]?weights|orders?|ledger)"
    )
    privileged_authorities = {
        "OFFICIAL_EVIDENCE",
        "PROMOTION",
        "PROMOTE_CHALLENGER",
        "RISK_APPROVAL",
        "PORTFOLIO",
        "ORDER",
        "LEDGER",
        "EXECUTION",
    }

    def evaluate(self, artifact: Mapping[str, Any] | None) -> RealLLMSmokeVerdict:
        if artifact is None:
            return RealLLMSmokeVerdict(
                status="UNAVAILABLE",
                evidence_hash=canonical_hash({"missing": True}),
                failure_codes=(RealLLMSmokeFailureCode.NO_REAL_PROVIDER_CALL,),
                message="No real-LLM Campaign smoke artifact was supplied.",
            )
        selected = set(str(item) for item in artifact.get("selected_desks", ()) or ())
        provider_calls = tuple(dict(item) for item in artifact.get("provider_calls", ()) or ())
        real_calls = [call for call in provider_calls if call.get("provider_mode") == "REAL_LLM"]
        failures: list[RealLLMSmokeFailureCode] = []
        if not real_calls:
            failures.append(RealLLMSmokeFailureCode.NO_REAL_PROVIDER_CALL)
        if provider_calls and not real_calls and artifact.get("status") == "READY_FOR_INTEGRATION":
            failures.append(RealLLMSmokeFailureCode.DETERMINISTIC_IMPERSONATED_REAL)
        for call in provider_calls:
            desk_id = str(call.get("desk_id", ""))
            if desk_id not in selected:
                failures.append(RealLLMSmokeFailureCode.EXCLUDED_DESK_CALLED)
        exposed = set(str(item) for item in artifact.get("context_exposed_desks", ()) or ())
        if exposed - selected:
            failures.append(RealLLMSmokeFailureCode.UNSELECTED_CONTEXT_EXPOSED)
        output_authorities = set(str(item).upper() for item in artifact.get("output_authorities", ()) or ())
        if output_authorities & self.privileged_authorities:
            failures.append(RealLLMSmokeFailureCode.PRIVILEGED_OUTPUT_AUTHORITY)
        rendered = json.dumps(_normalize(artifact), ensure_ascii=False)
        if self.forbidden_text.search(rendered):
            failures.append(RealLLMSmokeFailureCode.SEALED_OR_SECRET_LEAKAGE)
        if artifact.get("timeout_injected") and not artifact.get("independent_desks_completed_after_timeout"):
            failures.append(RealLLMSmokeFailureCode.TIMEOUT_COLLAPSED_INDEPENDENT_DESKS)
        if artifact.get("resume_repeated_committed_stage"):
            failures.append(RealLLMSmokeFailureCode.RESUME_REPEATED_COMMITTED_STAGE)
        unique = tuple(dict.fromkeys(failures))
        if RealLLMSmokeFailureCode.NO_REAL_PROVIDER_CALL in unique:
            status = "UNAVAILABLE"
        elif unique:
            status = "BLOCKED"
        else:
            status = "READY"
        return RealLLMSmokeVerdict(
            status=status,
            evidence_hash=canonical_hash(artifact),
            failure_codes=unique,
            message="Real-LLM Campaign smoke satisfied." if not unique else "Real-LLM Campaign smoke is not satisfied.",
        )
