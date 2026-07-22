"""Safe, draft-only adapters for QuantaAlpha-shaped research payloads.

This package intentionally imports no QuantaAlpha runtime modules.  It treats
legacy payloads as inert mappings and converts only the safe parts into
VeriQuant contracts or auxiliary-only envelopes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..adapters.proposal import ProposalContext, QuantaAlphaProposalAdapter
from ..adapters.qlib import (
    QlibCostConfig,
    QlibDateSegment,
    QlibManifest,
    VisibleQlibManifest,
)
from ..adapters.regulator import FactorLintReport, FactorRegulatorAdapter
from ..contracts import (
    CanonicalContract,
    Component,
    ContractValidationError,
    FailureCode,
    Hypothesis,
    ResearchDirection,
    ResearchState,
)
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from ..data import FieldRole
from ..factors import CompiledFactorDAG


_KEY_RE = re.compile(r"[^a-z0-9]+")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_FORBIDDEN_KEYS = {
    "code",
    "sourcecode",
    "python",
    "pythoncode",
    "callable",
    "factorpy",
    "factorpath",
    "script",
    "implementation",
    "official",
    "officialmetric",
    "officialmetrics",
    "promotion",
    "promotiondecision",
    "sealed",
    "sealedref",
    "sealedtest",
    "sealedmetrics",
    "portfolio",
    "portfolioplan",
    "targetweights",
    "weights",
    "risk",
    "riskapproval",
    "order",
    "orders",
    "orderplan",
    "ledger",
    "fills",
    "broker",
}

_CODE_MARKERS = (
    "factor.py",
    "```python",
    "import ",
    " from ",
    "def ",
    "lambda ",
    "eval(",
    "exec(",
    "__import__",
    "subprocess",
    "os.system",
    "open(",
)

_RANKIC_ONLY_MARKERS = (
    "rankic>0",
    "rank_ic>0",
    "rankic > 0",
    "rank_ic > 0",
    "replace best result",
)


def _stable_id(prefix: str, payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:20]}"


def _normalized_key(value: str) -> str:
    return _KEY_RE.sub("", value.lower())


def _require_hash_tuple(value: Sequence[str], field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field_name} is required", field=field_name)
    hashes: list[str] = []
    for item in value:
        require_hash(item, field_name)
        hashes.append(item)
    return tuple(sorted(dict.fromkeys(hashes)))


def _require_text_tuple(value: Sequence[str], field_name: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (nonempty and not value):
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field_name} is required", field=field_name)
    items: list[str] = []
    for item in value:
        require_text(item, field_name)
        items.append(item.strip())
    return tuple(items)


def _reject_forbidden_payload(value: Any, path: str = "payload") -> None:
    if callable(value):
        fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "Python callable payloads are forbidden", path=path)
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                fail(FailureCode.UNKNOWN_FIELD, "payload keys must be strings", path=path)
            if _normalized_key(raw_key) in _FORBIDDEN_KEYS:
                fail(
                    FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                    "payload contains executable or privileged authority fields",
                    path=f"{path}.{raw_key}",
                )
            _reject_forbidden_payload(child, f"{path}.{raw_key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_payload(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = f" {value.lower()} "
        marker = next((item for item in _CODE_MARKERS if item in lowered), None)
        if marker is not None:
            fail(
                FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                "payload contains generated Python or executable text",
                path=path,
                marker=marker.strip(),
            )
        if any(marker in lowered.replace(" ", "") for marker in _RANKIC_ONLY_MARKERS[:2]):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "RankIC-only success rules are forbidden", path=path)
        if any(marker in lowered for marker in _RANKIC_ONLY_MARKERS[2:]):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "RankIC-only or LLM replacement rules are forbidden", path=path)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    fail(
        FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
        f"unsupported executable-capable value type: {type(value).__name__}",
        path=path,
    )


def _allowed_fields(payload: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        fail(FailureCode.UNKNOWN_FIELD, f"{path} contains unknown fields", path=path, fields=tuple(sorted(unknown)))


@dataclass(frozen=True, kw_only=True)
class QuantaAdapterContext(CanonicalContract):
    """Frozen user/Campaign context required before adapting legacy payloads."""

    direction_hash: str
    question_hash: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    field_roles: Mapping[str, FieldRole]

    def __post_init__(self) -> None:
        require_hash(self.direction_hash, "direction_hash")
        require_hash(self.question_hash, "question_hash")
        require_nonempty_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.availability_assumptions, "availability_assumptions")
        if not isinstance(self.field_roles, Mapping) or not self.field_roles:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "field_roles is required")
        for name, role in self.field_roles.items():
            require_text(name, "field_roles")
            if not isinstance(role, FieldRole):
                fail(FailureCode.INVALID_VALUE, "field_roles must contain FieldRole values")
        object.__setattr__(self, "field_roles", MappingProxyType(dict(sorted(self.field_roles.items()))))

    def proposal_context(self) -> ProposalContext:
        return ProposalContext(
            direction_hash=self.direction_hash,
            universe=self.universe,
            horizon=self.horizon,
            decision_time=self.decision_time,
            availability_assumptions=self.availability_assumptions,
            field_roles=self.field_roles,
        )


class QuantaDirectionAdapter:
    """Map a Quanta planning direction into a DRAFT ResearchDirection."""

    def adapt(self, payload: Mapping[str, Any], *, context: QuantaAdapterContext) -> ResearchDirection:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "direction payload must be a mapping")
        _reject_forbidden_payload(payload)
        _allowed_fields(payload, {"direction_id", "thesis", "constraints"}, "direction")
        thesis = payload.get("thesis")
        require_text(thesis, "thesis")
        constraints = _require_text_tuple(payload.get("constraints", ()), "constraints", nonempty=False)
        return ResearchDirection(
            direction_id=str(payload.get("direction_id") or _stable_id("quanta-dir", payload)),
            question_hash=context.question_hash,
            thesis=str(thesis),
            constraints=constraints,
            universe=context.universe,
            horizon=context.horizon,
            decision_time=context.decision_time,
            availability_assumptions=context.availability_assumptions,
            state=ResearchState.DRAFT,
        )


class QuantaHypothesisAdapter:
    """Map an AlphaAgentHypothesis-like object into a DRAFT Hypothesis."""

    def adapt(self, payload: Mapping[str, Any], *, context: QuantaAdapterContext) -> Hypothesis:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "hypothesis payload must be a mapping")
        _reject_forbidden_payload(payload)
        _allowed_fields(
            payload,
            {
                "hypothesis_id",
                "statement",
                "hypothesis",
                "economic_mechanism",
                "reason",
                "justification",
                "expected_sign",
                "expected_direction",
                "falsification_tests",
                "falsification",
            },
            "hypothesis",
        )
        statement = payload.get("statement", payload.get("hypothesis"))
        mechanism = payload.get("economic_mechanism", payload.get("reason", payload.get("justification")))
        sign = str(payload.get("expected_sign", payload.get("expected_direction", ""))).upper()
        sign = {"UP": "POSITIVE", "DOWN": "NEGATIVE", "NONMONOTONIC": "NON_MONOTONIC"}.get(sign, sign)
        falsification = payload.get("falsification_tests", payload.get("falsification"))
        require_text(statement, "statement")
        require_text(mechanism, "economic_mechanism")
        return Hypothesis(
            hypothesis_id=str(payload.get("hypothesis_id") or _stable_id("quanta-hyp", payload)),
            direction_hash=context.direction_hash,
            statement=str(statement),
            economic_mechanism=str(mechanism),
            expected_sign=sign,
            falsification_tests=_require_text_tuple(falsification, "falsification_tests"),
            universe=context.universe,
            horizon=context.horizon,
            decision_time=context.decision_time,
            availability_assumptions=context.availability_assumptions,
            state=ResearchState.DRAFT,
        )


class QuantaFactorTaskAdapter:
    """Convert a safe Quanta FactorTask-like payload into internal DRAFT artifacts."""

    def __init__(self) -> None:
        self._adapter = QuantaAlphaProposalAdapter()

    def adapt(
        self,
        payload: Mapping[str, Any],
        *,
        hypothesis_payload: Mapping[str, Any],
        context: QuantaAdapterContext,
    ):
        if not isinstance(payload, Mapping) or not isinstance(hypothesis_payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "factor and hypothesis payloads must be mappings")
        _reject_forbidden_payload({"hypothesis": hypothesis_payload, "factor_task": payload})
        return self._adapter.adapt(
            {"hypothesis": hypothesis_payload, "factor_task": payload},
            context=context.proposal_context(),
        )


class QuantaStaticLintAdapter:
    """Run parser/complexity/redundancy lint only; never PIT or validation."""

    def __init__(self) -> None:
        self._adapter = FactorRegulatorAdapter()

    def lint(
        self,
        payload: Mapping[str, Any],
        *,
        field_roles: Mapping[str, FieldRole],
        factor_zoo: Sequence[CompiledFactorDAG] = (),
    ) -> FactorLintReport:
        _reject_forbidden_payload(payload)
        report = self._adapter.lint(payload, field_roles=field_roles, factor_zoo=factor_zoo)
        if report.report_kind != "lint" or report.state is not ResearchState.DRAFT:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "lint cannot claim PIT or validation authority")
        return report


@dataclass(frozen=True, kw_only=True)
class QuantaReportDraft(CanonicalContract):
    draft_id: str
    title: str
    claims: tuple[str, ...]
    citation_hashes: tuple[str, ...]
    source_document_hash: str
    state: ResearchState = ResearchState.DRAFT
    produced_by: Component = Component.QUANTAALPHA

    def __post_init__(self) -> None:
        require_identifier(self.draft_id, "draft_id")
        require_text(self.title, "title")
        object.__setattr__(self, "claims", _require_text_tuple(self.claims, "claims"))
        object.__setattr__(self, "citation_hashes", _require_hash_tuple(self.citation_hashes, "citation_hashes"))
        require_hash(self.source_document_hash, "source_document_hash")
        if self.state is not ResearchState.DRAFT or self.produced_by is not Component.QUANTAALPHA:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "report extraction can only produce QuantaAlpha DRAFT")


class QuantaReportDraftAdapter:
    """Turn frozen cited report-extraction payloads into DRAFT-only artifacts."""

    def adapt(self, payload: Mapping[str, Any]) -> QuantaReportDraft:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "report payload must be a mapping")
        _reject_forbidden_payload(payload)
        _allowed_fields(payload, {"draft_id", "title", "claims", "citation_hashes", "source_document_hash"}, "report")
        if "citation_hashes" not in payload:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "report draft must include citations")
        return QuantaReportDraft(
            draft_id=str(payload.get("draft_id") or _stable_id("quanta-report", payload)),
            title=str(payload.get("title", "")),
            claims=tuple(payload.get("claims", ())),
            citation_hashes=tuple(payload.get("citation_hashes", ())),
            source_document_hash=str(payload.get("source_document_hash", "")),
        )


@dataclass(frozen=True, kw_only=True)
class QuantaQlibManifestDraft(CanonicalContract):
    manifest_hash: str
    visible_manifest: VisibleQlibManifest
    auxiliary_only: bool = True
    complete_trading_chain: bool = False
    produced_by: Component = Component.QLIB

    def __post_init__(self) -> None:
        require_hash(self.manifest_hash, "manifest_hash")
        if not isinstance(self.visible_manifest, VisibleQlibManifest):
            fail(FailureCode.INVALID_VALUE, "visible_manifest must be VisibleQlibManifest")
        if self.visible_manifest.manifest_hash != self.manifest_hash:
            fail(FailureCode.INVALID_HASH, "visible manifest must bind the full frozen manifest hash")
        if self.auxiliary_only is not True or self.complete_trading_chain is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Qlib manifest is auxiliary-only and not a trading chain")
        if self.produced_by is not Component.QLIB:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "Qlib manifest draft must be produced by QLIB")


class QuantaQlibManifestAdapter:
    """Build a frozen local Qlib manifest view without exposing sealed test fields."""

    def adapt(self, payload: Mapping[str, Any]) -> QuantaQlibManifestDraft:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "Qlib manifest payload must be a mapping")
        _reject_forbidden_payload(payload)
        _allowed_fields(
            payload,
            {
                "provider_uri",
                "region",
                "universe",
                "train",
                "validation",
                "test",
                "label",
                "model",
                "strategy",
                "cost",
                "seed",
                "complete_trading_chain",
            },
            "qlib_manifest",
        )
        if payload.get("complete_trading_chain", False):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Qlib TopK cannot claim the complete trading chain")
        manifest = QlibManifest(
            provider_uri=str(payload.get("provider_uri", "")),
            region=str(payload.get("region", "")),
            universe=tuple(payload.get("universe", ())),
            train=QlibDateSegment.from_payload(payload["train"]),
            validation=QlibDateSegment.from_payload(payload["validation"]),
            test=QlibDateSegment.from_payload(payload["test"]),
            label=str(payload.get("label", "")),
            model=str(payload.get("model", "")),
            strategy=str(payload.get("strategy", "")),
            cost=QlibCostConfig.from_payload(payload["cost"]),
            seed=payload.get("seed"),
        )
        return QuantaQlibManifestDraft(
            manifest_hash=manifest.artifact_hash,
            visible_manifest=manifest.visible(),
        )


@dataclass(frozen=True, kw_only=True)
class QuantaLineageDraft(CanonicalContract):
    lineage_id: str
    parent_ids: tuple[str, ...]
    operator: str
    generation: int
    reason: str
    prompt_hash: str
    artifact_hashes: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT
    produced_by: Component = Component.QUANTAALPHA

    def __post_init__(self) -> None:
        require_identifier(self.lineage_id, "lineage_id")
        object.__setattr__(self, "parent_ids", _require_text_tuple(self.parent_ids, "parent_ids", nonempty=False))
        if self.operator not in {"ORIGINAL", "MUTATION", "CROSSOVER"}:
            fail(FailureCode.INVALID_VALUE, "unsupported lineage operator")
        required = {"ORIGINAL": 0, "MUTATION": 1, "CROSSOVER": 2}[self.operator]
        if len(self.parent_ids) != required:
            fail(FailureCode.INVALID_VALUE, "lineage operator has the wrong parent count")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            fail(FailureCode.INVALID_VALUE, "generation must be a non-negative integer")
        require_text(self.reason, "reason")
        require_hash(self.prompt_hash, "prompt_hash")
        object.__setattr__(self, "artifact_hashes", _require_hash_tuple(self.artifact_hashes, "artifact_hashes"))
        if self.state is not ResearchState.DRAFT or self.produced_by is not Component.QUANTAALPHA:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Quanta lineage cannot write promotion authority")


class QuantaLineageAdapter:
    """Adapt mutation/crossover lineage only as DRAFT generation metadata."""

    def adapt(self, payload: Mapping[str, Any]) -> QuantaLineageDraft:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "lineage payload must be a mapping")
        _reject_forbidden_payload(payload)
        _allowed_fields(
            payload,
            {
                "lineage_id",
                "parent_ids",
                "operator",
                "generation",
                "reason",
                "prompt_hash",
                "artifact_hashes",
                "metrics",
                "success",
                "success_rule",
            },
            "lineage",
        )
        metrics = payload.get("metrics")
        if payload.get("success") is True:
            if isinstance(metrics, Mapping) and set(_normalized_key(key) for key in metrics) <= {"rankic", "rank_ic"}:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "RankIC-only success cannot select lineage parents")
            rule = str(payload.get("success_rule", "")).lower().replace(" ", "")
            if rule in {"rankic>0", "rank_ic>0"}:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "RankIC-only success cannot select lineage parents")
        prompt_hash = payload.get("prompt_hash")
        if not isinstance(prompt_hash, str) or not _HASH_RE.fullmatch(prompt_hash):
            prompt_hash = "sha256:" + hashlib.sha256(str(payload.get("reason", "")).encode("utf-8")).hexdigest()
        return QuantaLineageDraft(
            lineage_id=str(payload.get("lineage_id") or _stable_id("quanta-lineage", payload)),
            parent_ids=tuple(payload.get("parent_ids", ())),
            operator=str(payload.get("operator", "")),
            generation=payload.get("generation"),
            reason=str(payload.get("reason", "")),
            prompt_hash=prompt_hash,
            artifact_hashes=tuple(payload.get("artifact_hashes", ())),
        )
