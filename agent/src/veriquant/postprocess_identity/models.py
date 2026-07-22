"""Static identities for formula programs with cross-sectional postprocessing.

The program identity is deliberately computable before a score exists.  Runtime
scores are bound by a separate output identity and can therefore never redefine
the identity registered in the Trial Ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
)
from ..data import canonical_content_hash


POSTPROCESS_IDENTITY_SCHEMA_VERSION = "veriquant.postprocess-identity/v1"
MAX_STATIC_POSTPROCESS_STEPS = 16

RANK_POLICY_VERSION = "rank.average-tie-percentile-0-to-1/v1"
ZSCORE_POLICY_VERSION = "zscore.population-standard-deviation/v1"
WINSORIZE_POLICY_VERSION = "winsorize.linear-quantile/v1"
INDUSTRY_NEUTRALIZATION_VERSION = "industry.within-group-mean-residual/v1"
SIZE_NEUTRALIZATION_VERSION = "size.ols-intercept-log-market-cap-residual/v1"


class StaticPostprocessOperator(str, Enum):
    RANK = "RANK"
    ZSCORE = "ZSCORE"
    WINSORIZE = "WINSORIZE"
    INDUSTRY_NEUTRALIZE = "INDUSTRY_NEUTRALIZE"
    SIZE_NEUTRALIZE = "SIZE_NEUTRALIZE"


_OPERATOR_VERSIONS = {
    StaticPostprocessOperator.RANK: RANK_POLICY_VERSION,
    StaticPostprocessOperator.ZSCORE: ZSCORE_POLICY_VERSION,
    StaticPostprocessOperator.WINSORIZE: WINSORIZE_POLICY_VERSION,
    StaticPostprocessOperator.INDUSTRY_NEUTRALIZE: INDUSTRY_NEUTRALIZATION_VERSION,
    StaticPostprocessOperator.SIZE_NEUTRALIZE: SIZE_NEUTRALIZATION_VERSION,
}
_FORBIDDEN_FIELD_TOKENS = (
    "label",
    "sealed",
    "future_return",
    "forward_return",
    "target_return",
)


def _reject_privileged_field(field: str, field_name: str) -> None:
    require_identifier(field, field_name)
    lowered = field.casefold()
    if any(token in lowered for token in _FORBIDDEN_FIELD_TOKENS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "label or sealed fields cannot enter a postprocess identity",
            field=field,
        )


@dataclass(frozen=True, kw_only=True)
class StaticPostprocessStep(CanonicalContract):
    """One closed, versioned postprocess operation; no executable callback exists."""

    operator: StaticPostprocessOperator
    policy_version: str
    field: str | None = None
    lower: float | None = None
    upper: float | None = None

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operator, StaticPostprocessOperator):
            fail(FailureCode.INVALID_VALUE, "postprocess operator is not registered")
        expected_version = _OPERATOR_VERSIONS[self.operator]
        if self.policy_version != expected_version:
            fail(
                FailureCode.INVALID_VALUE,
                "postprocess policy version is not the registered version",
                operator=self.operator.value,
                expected=expected_version,
            )
        if self.operator is StaticPostprocessOperator.WINSORIZE:
            if self.field is not None:
                fail(FailureCode.INVALID_VALUE, "winsorize does not accept a field")
            if (
                isinstance(self.lower, bool)
                or isinstance(self.upper, bool)
                or not isinstance(self.lower, (int, float))
                or not isinstance(self.upper, (int, float))
                or not math.isfinite(float(self.lower))
                or not math.isfinite(float(self.upper))
                or not 0.0 <= float(self.lower) < float(self.upper) <= 1.0
            ):
                fail(FailureCode.INVALID_VALUE, "winsorize requires finite 0 <= lower < upper <= 1")
            object.__setattr__(self, "lower", float(self.lower))
            object.__setattr__(self, "upper", float(self.upper))
            return
        if self.lower is not None or self.upper is not None:
            fail(FailureCode.INVALID_VALUE, "only winsorize accepts lower and upper")
        if self.operator in {StaticPostprocessOperator.RANK, StaticPostprocessOperator.ZSCORE}:
            if self.field is not None:
                fail(FailureCode.INVALID_VALUE, "rank and zscore do not accept fields")
            return
        expected_field = (
            "industry"
            if self.operator is StaticPostprocessOperator.INDUSTRY_NEUTRALIZE
            else "market_cap"
        )
        field = self.field or expected_field
        _reject_privileged_field(field, "field")
        object.__setattr__(self, "field", field)


@dataclass(frozen=True, kw_only=True)
class CrossSectionalPostprocessPlan(CanonicalContract):
    plan_id: str
    steps: tuple[StaticPostprocessStep, ...] = ()

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.plan_id, "plan_id")
        if not isinstance(self.steps, tuple) or len(self.steps) > MAX_STATIC_POSTPROCESS_STEPS:
            fail(FailureCode.INVALID_VALUE, "postprocess plan exceeds its static step limit")
        if any(not isinstance(step, StaticPostprocessStep) for step in self.steps):
            fail(FailureCode.INVALID_VALUE, "postprocess plan must contain typed steps")


@dataclass(frozen=True, kw_only=True)
class NeutralizationPolicy(CanonicalContract):
    """Frozen policy for every neutralization operation referenced by the plan."""

    policy_id: str
    industry_policy_version: str = INDUSTRY_NEUTRALIZATION_VERSION
    industry_field: str = "industry"
    size_policy_version: str = SIZE_NEUTRALIZATION_VERSION
    size_field: str = "market_cap"
    minimum_size_observations: int = 3

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        if self.industry_policy_version != INDUSTRY_NEUTRALIZATION_VERSION:
            fail(FailureCode.INVALID_VALUE, "industry neutralization version is unsupported")
        if self.size_policy_version != SIZE_NEUTRALIZATION_VERSION:
            fail(FailureCode.INVALID_VALUE, "size neutralization version is unsupported")
        _reject_privileged_field(self.industry_field, "industry_field")
        _reject_privileged_field(self.size_field, "size_field")
        if (
            isinstance(self.minimum_size_observations, bool)
            or not isinstance(self.minimum_size_observations, int)
            or self.minimum_size_observations < 2
        ):
            fail(FailureCode.INVALID_VALUE, "minimum_size_observations must be at least 2")


@dataclass(frozen=True, kw_only=True)
class PostprocessRuntimePolicy(CanonicalContract):
    policy_id: str
    runtime_version: str
    missing_value_policy: str = "propagate-or-operator-defined/v1"
    cross_section_ordering: str = "event-time-then-symbol/v1"

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        require_identifier(self.runtime_version, "runtime_version")
        require_identifier(self.missing_value_policy, "missing_value_policy")
        require_identifier(self.cross_section_ordering, "cross_section_ordering")


@dataclass(frozen=True, kw_only=True)
class ProgramIdentity(CanonicalContract):
    """Complete static identity, suitable for pre-reservation registration."""

    time_series_formula_ir_hash: str
    cross_sectional_postprocess_plan_hash: str
    neutralization_policy_hash: str
    runtime_policy_hash: str
    compiler_version: str
    program_hash: str

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "time_series_formula_ir_hash",
            "cross_sectional_postprocess_plan_hash",
            "neutralization_policy_hash",
            "runtime_policy_hash",
            "program_hash",
        ):
            require_hash(getattr(self, name), name)
        require_identifier(self.compiler_version, "compiler_version")
        if self.program_hash != _program_hash(
            time_series_formula_ir_hash=self.time_series_formula_ir_hash,
            cross_sectional_postprocess_plan_hash=self.cross_sectional_postprocess_plan_hash,
            neutralization_policy_hash=self.neutralization_policy_hash,
            runtime_policy_hash=self.runtime_policy_hash,
            compiler_version=self.compiler_version,
        ):
            fail(FailureCode.INVALID_HASH, "static ProgramIdentity is inconsistent")

    @property
    def experiment_factor_hash(self) -> str:
        """The immutable factor hash an ExperimentIdentity must bind."""

        return self.program_hash


def _program_hash(
    *,
    time_series_formula_ir_hash: str,
    cross_sectional_postprocess_plan_hash: str,
    neutralization_policy_hash: str,
    runtime_policy_hash: str,
    compiler_version: str,
) -> str:
    return canonical_content_hash(
        {
            "kind": "STATIC_FACTOR_PROGRAM",
            "schema_version": POSTPROCESS_IDENTITY_SCHEMA_VERSION,
            "time_series_formula_ir_hash": time_series_formula_ir_hash,
            "cross_sectional_postprocess_plan_hash": cross_sectional_postprocess_plan_hash,
            "neutralization_policy_hash": neutralization_policy_hash,
            "runtime_policy_hash": runtime_policy_hash,
            "compiler_version": compiler_version,
        }
    )


def derive_program_identity(
    *,
    time_series_formula_ir_hash: str,
    postprocess_plan: CrossSectionalPostprocessPlan,
    neutralization_policy: NeutralizationPolicy,
    runtime_policy: PostprocessRuntimePolicy,
    compiler_version: str,
) -> ProgramIdentity:
    require_hash(time_series_formula_ir_hash, "time_series_formula_ir_hash")
    if not isinstance(postprocess_plan, CrossSectionalPostprocessPlan):
        fail(FailureCode.INVALID_VALUE, "postprocess_plan must be typed")
    if not isinstance(neutralization_policy, NeutralizationPolicy):
        fail(FailureCode.INVALID_VALUE, "neutralization_policy must be typed")
    if not isinstance(runtime_policy, PostprocessRuntimePolicy):
        fail(FailureCode.INVALID_VALUE, "runtime_policy must be typed")
    require_identifier(compiler_version, "compiler_version")
    for step in postprocess_plan.steps:
        if step.operator is StaticPostprocessOperator.INDUSTRY_NEUTRALIZE:
            if step.field != neutralization_policy.industry_field:
                fail(FailureCode.INVALID_VALUE, "industry field disagrees with neutralization policy")
        if step.operator is StaticPostprocessOperator.SIZE_NEUTRALIZE:
            if step.field != neutralization_policy.size_field:
                fail(FailureCode.INVALID_VALUE, "size field disagrees with neutralization policy")
    values = {
        "time_series_formula_ir_hash": time_series_formula_ir_hash,
        "cross_sectional_postprocess_plan_hash": postprocess_plan.artifact_hash,
        "neutralization_policy_hash": neutralization_policy.artifact_hash,
        "runtime_policy_hash": runtime_policy.artifact_hash,
        "compiler_version": compiler_version,
    }
    return ProgramIdentity(**values, program_hash=_program_hash(**values))


@dataclass(frozen=True, kw_only=True)
class ActualScore(CanonicalContract):
    event_time: datetime
    symbol: str
    score: float | None

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                fail(FailureCode.INVALID_VALUE, "score must be numeric or missing")
            if not math.isfinite(float(self.score)):
                fail(FailureCode.NON_FINITE_NUMBER, "score must be finite or missing")
            object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True, kw_only=True)
class ScoreArtifactIdentity(CanonicalContract):
    """Runtime output identity; it is never a program or Experiment identity."""

    program_hash: str
    frozen_snapshot_hash: str
    universe_hash: str
    actual_scores_hash: str
    score_artifact_hash: str

    SCHEMA_VERSION = POSTPROCESS_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "program_hash",
            "frozen_snapshot_hash",
            "universe_hash",
            "actual_scores_hash",
            "score_artifact_hash",
        ):
            require_hash(getattr(self, name), name)
        expected = canonical_content_hash(
            {
                "kind": "POSTPROCESSED_SCORE_ARTIFACT",
                "schema_version": POSTPROCESS_IDENTITY_SCHEMA_VERSION,
                "program_hash": self.program_hash,
                "frozen_snapshot_hash": self.frozen_snapshot_hash,
                "universe_hash": self.universe_hash,
                "actual_scores_hash": self.actual_scores_hash,
            }
        )
        if self.score_artifact_hash != expected:
            fail(FailureCode.INVALID_HASH, "ScoreArtifactIdentity is inconsistent")


def derive_score_artifact_identity(
    *,
    program_identity: ProgramIdentity,
    frozen_snapshot_hash: str,
    universe_hash: str,
    actual_scores: tuple[ActualScore, ...],
) -> ScoreArtifactIdentity:
    if not isinstance(program_identity, ProgramIdentity):
        fail(FailureCode.INVALID_VALUE, "program_identity must be frozen before score output")
    require_hash(frozen_snapshot_hash, "frozen_snapshot_hash")
    require_hash(universe_hash, "universe_hash")
    if not isinstance(actual_scores, tuple) or not actual_scores:
        fail(FailureCode.INVALID_VALUE, "actual_scores must be a non-empty tuple")
    if any(not isinstance(item, ActualScore) for item in actual_scores):
        fail(FailureCode.INVALID_VALUE, "actual_scores must contain ActualScore")
    keys = tuple((item.event_time, item.symbol) for item in actual_scores)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        fail(FailureCode.INVALID_VALUE, "actual_scores must have unique stable event_time/symbol order")
    actual_scores_hash = canonical_content_hash(actual_scores)
    values = {
        "program_hash": program_identity.program_hash,
        "frozen_snapshot_hash": frozen_snapshot_hash,
        "universe_hash": universe_hash,
        "actual_scores_hash": actual_scores_hash,
    }
    score_artifact_hash = canonical_content_hash(
        {
            "kind": "POSTPROCESSED_SCORE_ARTIFACT",
            "schema_version": POSTPROCESS_IDENTITY_SCHEMA_VERSION,
            **values,
        }
    )
    return ScoreArtifactIdentity(**values, score_artifact_hash=score_artifact_hash)


__all__ = [
    "ActualScore",
    "CrossSectionalPostprocessPlan",
    "INDUSTRY_NEUTRALIZATION_VERSION",
    "MAX_STATIC_POSTPROCESS_STEPS",
    "NeutralizationPolicy",
    "POSTPROCESS_IDENTITY_SCHEMA_VERSION",
    "PostprocessRuntimePolicy",
    "ProgramIdentity",
    "RANK_POLICY_VERSION",
    "SIZE_NEUTRALIZATION_VERSION",
    "ScoreArtifactIdentity",
    "StaticPostprocessOperator",
    "StaticPostprocessStep",
    "WINSORIZE_POLICY_VERSION",
    "ZSCORE_POLICY_VERSION",
    "derive_program_identity",
    "derive_score_artifact_identity",
]
