"""Typed boundaries for Campaign candidate to certified Qlib translation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import fail, freeze_mapping, require_hash, require_identifier, require_text
from ..qlib_expression.api import CertifiedQlibExpression, QlibExpressionFailureCode
from ..research_campaign import CampaignState, ResearchCampaign, require_campaign_executable


SCHEMA_VERSION = "veriquant.qlib-candidate-translation/v1"
LLM_SCHEMA_VERSION = "veriquant.qlib-candidate-llm-schema/v1"
_PRIVILEGED_FIELD = re.compile(
    r"(?i)(?:label|sealed|future_return|official|promotion|risk|orders?|fills?|ledger|broker)"
)


class QlibCandidateTranslationStatus(str, Enum):
    CERTIFIED = "CERTIFIED"
    REVISE = "REVISE"
    UNSUPPORTED = "UNSUPPORTED"


class QlibCandidateFailureCode(str, Enum):
    FORBIDDEN_CANDIDATE_PAYLOAD = "FORBIDDEN_CANDIDATE_PAYLOAD"
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    REQUESTED_LOOKBACK_NOT_REPRESENTED = "REQUESTED_LOOKBACK_NOT_REPRESENTED"
    EXPRESSION_REJECTED = "EXPRESSION_REJECTED"


@dataclass(frozen=True, kw_only=True)
class QlibCandidateDraftEnvelope(CanonicalContract):
    """System-wrapped candidate; the LLM never controls Campaign authority fields."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset(
        {Component.LLM_RESEARCHER, Component.RESEARCH_DESK}
    )

    campaign_hash: str
    campaign_state: CampaignState
    group_id: str
    candidate_id: str
    expression: str
    confirmed_field_whitelist: tuple[str, ...]
    requested_lookback_sessions: int | None
    source_draft_hash: str
    proposed_by: Component = Component.RESEARCH_DESK

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        if self.campaign_state is not CampaignState.CONFIRMED:
            fail(FailureCode.INVALID_STATE, "Qlib candidate requires a CONFIRMED Campaign")
        require_identifier(self.group_id, "group_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_text(self.expression, "expression")
        require_hash(self.source_draft_hash, "source_draft_hash")
        if self.proposed_by not in {Component.LLM_RESEARCHER, Component.RESEARCH_DESK}:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "candidate proposer is not a research role")
        if not isinstance(self.confirmed_field_whitelist, tuple) or not self.confirmed_field_whitelist:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "confirmed Campaign field whitelist is required")
        fields = tuple(sorted(self.confirmed_field_whitelist))
        if len(set(fields)) != len(fields):
            fail(FailureCode.INVALID_VALUE, "confirmed field whitelist contains duplicates")
        for field_name in fields:
            require_identifier(field_name, "confirmed_field_whitelist")
            if _PRIVILEGED_FIELD.search(field_name):
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "privileged field cannot enter the Campaign feature whitelist",
                    field=field_name,
                )
        object.__setattr__(self, "confirmed_field_whitelist", fields)
        value = self.requested_lookback_sessions
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            fail(FailureCode.INVALID_VALUE, "requested_lookback_sessions must be positive")

    @classmethod
    def from_campaign(
        cls,
        campaign: ResearchCampaign,
        *,
        group_id: str,
        candidate_id: str,
        expression: str,
        requested_lookback_sessions: int | None,
        source_draft_hash: str,
        proposed_by: Component = Component.RESEARCH_DESK,
    ) -> "QlibCandidateDraftEnvelope":
        require_campaign_executable(campaign)
        return cls(
            campaign_hash=campaign.artifact_hash,
            campaign_state=campaign.state,
            group_id=group_id,
            candidate_id=candidate_id,
            expression=expression,
            confirmed_field_whitelist=campaign.subject.allowed_data,
            requested_lookback_sessions=requested_lookback_sessions,
            source_draft_hash=source_draft_hash,
            proposed_by=proposed_by,
        )

    @classmethod
    def from_llm_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        campaign: ResearchCampaign,
        group_id: str,
        requested_lookback_sessions: int | None,
        source_draft_hash: str,
        proposed_by: Component = Component.RESEARCH_DESK,
    ) -> "QlibCandidateDraftEnvelope":
        """Close the real-LLM payload before adding system-owned authority fields."""

        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "LLM candidate payload must be an object")
        keys = set(payload)
        if keys != {"candidate_id", "expression"}:
            forbidden = tuple(
                sorted(
                    key
                    for key in keys - {"candidate_id", "expression"}
                    if _PRIVILEGED_FIELD.search(str(key))
                    or str(key).lower() in {
                        "code",
                        "python",
                        "factor.py",
                        "custom_operator",
                    }
                )
            )
            fail(
                FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                "LLM candidate payload must contain only candidate_id and expression",
                forbidden_fields=forbidden,
                fields=tuple(sorted(str(key) for key in keys)),
            )
        return cls.from_campaign(
            campaign,
            group_id=group_id,
            candidate_id=payload["candidate_id"],
            expression=payload["expression"],
            requested_lookback_sessions=requested_lookback_sessions,
            source_draft_hash=source_draft_hash,
            proposed_by=proposed_by,
        )


@dataclass(frozen=True, kw_only=True)
class QlibCandidateTranslationResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    draft_hash: str
    candidate_id: str
    confirmed_field_whitelist: tuple[str, ...]
    status: QlibCandidateTranslationStatus
    policy_hash: str
    manifest_hash: str
    canonical_expression: str | None = None
    certified_expression: CertifiedQlibExpression | None = field(default=None, repr=False)
    expression_hash: str | None = None
    program_hash: str | None = None
    required_fields: tuple[str, ...] = ()
    maximum_lookback: int | None = None
    operators: tuple[str, ...] = ()
    failure_code: QlibCandidateFailureCode | None = None
    expression_failure_code: QlibExpressionFailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_hash(self.draft_hash, "draft_hash")
        require_identifier(self.candidate_id, "candidate_id")
        if (
            not isinstance(self.confirmed_field_whitelist, tuple)
            or not self.confirmed_field_whitelist
            or tuple(sorted(set(self.confirmed_field_whitelist)))
            != self.confirmed_field_whitelist
        ):
            fail(FailureCode.INVALID_VALUE, "confirmed field whitelist must be sorted and unique")
        if not isinstance(self.status, QlibCandidateTranslationStatus):
            fail(FailureCode.INVALID_VALUE, "translation status is invalid")
        require_hash(self.policy_hash, "policy_hash")
        require_hash(self.manifest_hash, "manifest_hash")
        if self.status is QlibCandidateTranslationStatus.CERTIFIED:
            for name in ("expression_hash", "program_hash"):
                value = getattr(self, name)
                if value is None:
                    fail(FailureCode.MISSING_REQUIRED_FIELD, f"certified result requires {name}")
                require_hash(value, name)
            if not self.canonical_expression or not self.required_fields:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "certified result requires expression and fields")
            if not set(self.required_fields).issubset(self.confirmed_field_whitelist):
                fail(FailureCode.UNKNOWN_FIELD, "certified fields escape the confirmed whitelist")
            if not isinstance(self.certified_expression, CertifiedQlibExpression):
                fail(FailureCode.MISSING_REQUIRED_FIELD, "certified result requires certified expression AST")
            if self.certified_expression.canonical_expression != self.canonical_expression:
                fail(FailureCode.INVALID_HASH, "certified expression does not match canonical expression")
            if self.certified_expression.expression_hash != self.expression_hash:
                fail(FailureCode.INVALID_HASH, "certified expression hash mismatch")
            if self.maximum_lookback is None or self.maximum_lookback < 0:
                fail(FailureCode.INVALID_VALUE, "certified result has invalid lookback")
            if self.failure_code is not None or self.expression_failure_code is not None:
                fail(FailureCode.INVALID_STATE, "certified result cannot carry failure")
        else:
            if self.failure_code is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "non-certified result requires failure_code")
            if any(
                value is not None
                for value in (
                    self.canonical_expression,
                    self.certified_expression,
                    self.expression_hash,
                    self.program_hash,
                    self.maximum_lookback,
                )
            ) or self.required_fields or self.operators:
                fail(FailureCode.INVALID_STATE, "rejected result cannot masquerade as a program")
        require_text(self.message, "message")

    @property
    def root(self) -> object | None:
        """The already-certified safe AST; never reconstructed from a string."""

        return None if self.certified_expression is None else self.certified_expression.root


@dataclass(frozen=True, kw_only=True)
class QlibCandidateLLMContract:
    schema: Mapping[str, Any]
    capability_summary: Mapping[str, Any]
    manifest_hash: str
    contract_hash: str
    schema_version: str = LLM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LLM_SCHEMA_VERSION:
            fail(FailureCode.INVALID_SCHEMA_VERSION, "LLM candidate schema version mismatch")
        object.__setattr__(self, "schema", freeze_mapping(self.schema, "schema"))
        object.__setattr__(
            self,
            "capability_summary",
            freeze_mapping(self.capability_summary, "capability_summary"),
        )
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.contract_hash, "contract_hash")


__all__ = [
    "LLM_SCHEMA_VERSION",
    "QlibCandidateDraftEnvelope",
    "QlibCandidateFailureCode",
    "QlibCandidateLLMContract",
    "QlibCandidateTranslationResult",
    "QlibCandidateTranslationStatus",
    "SCHEMA_VERSION",
]
