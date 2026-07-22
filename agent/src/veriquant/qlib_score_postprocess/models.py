"""Versioned contracts for deterministic cross-sectional score postprocessing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..data import canonical_content_hash


SCORE_POSTPROCESS_SCHEMA_VERSION = "veriquant.qlib-score-postprocess/v1"
MAX_POSTPROCESS_STEPS = 16


class ScorePostprocessOperator(str, Enum):
    RANK_CROSS_SECTION = "rank_cross_section"
    ZSCORE_CROSS_SECTION = "zscore_cross_section"
    WINSORIZE = "winsorize"
    NEUTRALIZE_BY_GROUP = "neutralize_by_group"
    NEUTRALIZE_BY_INDUSTRY = "neutralize_by_industry"
    NEUTRALIZE_BY_MARKET_CAP = "neutralize_by_market_cap"


_FORBIDDEN_FIELD_TOKENS = (
    "future_return",
    "forward_return",
    "label",
    "sealed",
    "target_return",
)


@dataclass(frozen=True, kw_only=True)
class ScorePostprocessStep(CanonicalContract):
    operator: ScorePostprocessOperator
    field: str | None = None
    lower: float | None = None
    upper: float | None = None

    SCHEMA_VERSION = SCORE_POSTPROCESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operator, ScorePostprocessOperator):
            fail(FailureCode.INVALID_VALUE, "postprocess operator is not registered")
        if self.operator is ScorePostprocessOperator.WINSORIZE:
            if self.field is not None:
                fail(FailureCode.INVALID_VALUE, "winsorize does not accept a field")
            lower = 0.01 if self.lower is None else self.lower
            upper = 0.99 if self.upper is None else self.upper
            if (
                isinstance(lower, bool)
                or isinstance(upper, bool)
                or not isinstance(lower, (int, float))
                or not isinstance(upper, (int, float))
                or not 0.0 <= float(lower) < float(upper) <= 1.0
            ):
                fail(FailureCode.INVALID_VALUE, "winsorize bounds must satisfy 0 <= lower < upper <= 1")
            object.__setattr__(self, "lower", float(lower))
            object.__setattr__(self, "upper", float(upper))
            return
        if self.lower is not None or self.upper is not None:
            fail(FailureCode.INVALID_VALUE, "only winsorize accepts lower/upper bounds")
        if self.operator in {
            ScorePostprocessOperator.RANK_CROSS_SECTION,
            ScorePostprocessOperator.ZSCORE_CROSS_SECTION,
        }:
            if self.field is not None:
                fail(FailureCode.INVALID_VALUE, "rank and zscore do not accept a field")
            return
        field = self.field
        if self.operator is ScorePostprocessOperator.NEUTRALIZE_BY_INDUSTRY and field is None:
            field = "industry"
        if self.operator is ScorePostprocessOperator.NEUTRALIZE_BY_MARKET_CAP and field is None:
            field = "market_cap"
        if field is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "neutralize_by_group requires a PIT group field")
        require_identifier(field, "field")
        _reject_forbidden_field(field)
        object.__setattr__(self, "field", field)


@dataclass(frozen=True, kw_only=True)
class ScorePostprocessProgram(CanonicalContract):
    program_id: str
    steps: tuple[ScorePostprocessStep, ...]

    SCHEMA_VERSION = SCORE_POSTPROCESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.program_id, "program_id")
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= MAX_POSTPROCESS_STEPS:
            fail(
                FailureCode.INVALID_VALUE,
                f"postprocess program must contain 1..{MAX_POSTPROCESS_STEPS} steps",
            )
        if any(not isinstance(step, ScorePostprocessStep) for step in self.steps):
            fail(FailureCode.INVALID_VALUE, "postprocess steps must be typed")

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(sorted({step.field for step in self.steps if step.field is not None}))


@dataclass(frozen=True, kw_only=True)
class ScorePostprocessPolicy(CanonicalContract):
    policy_id: str
    allowed_fields: tuple[str, ...]
    max_steps: int = 8
    min_market_cap_observations: int = 3

    SCHEMA_VERSION = SCORE_POSTPROCESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        if not isinstance(self.allowed_fields, tuple):
            fail(FailureCode.INVALID_VALUE, "allowed_fields must be a tuple")
        for field in self.allowed_fields:
            require_identifier(field, "allowed_fields")
            _reject_forbidden_field(field)
        if self.allowed_fields != tuple(sorted(set(self.allowed_fields))):
            fail(FailureCode.INVALID_VALUE, "allowed_fields must be sorted and unique")
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or not 1 <= self.max_steps <= MAX_POSTPROCESS_STEPS:
            fail(FailureCode.INVALID_VALUE, "max_steps is outside the supported range")
        if (
            isinstance(self.min_market_cap_observations, bool)
            or not isinstance(self.min_market_cap_observations, int)
            or self.min_market_cap_observations < 2
        ):
            fail(FailureCode.INVALID_VALUE, "min_market_cap_observations must be at least 2")


POSTPROCESS_RUNTIME_HASH = canonical_content_hash(
    {
        "runtime": "veriquant.qlib-score-postprocess",
        "schema_version": SCORE_POSTPROCESS_SCHEMA_VERSION,
        "rank": "average-tie-percentile-0-to-1/v1",
        "zscore": "population-standard-deviation/v1",
        "winsorize": "linear-quantile/v1",
        "group_neutralization": "within-group-mean-residual/v1",
        "market_cap_neutralization": "ols-intercept-log-market-cap-residual/v1",
        "constant_zscore": "missing/v1",
        "constant_market_cap_exposure": "missing/v1",
    }
)


@dataclass(frozen=True, kw_only=True)
class PostprocessedProgramIdentity(CanonicalContract):
    upstream_score_hash: str
    upstream_program_hash: str
    postprocess_program_hash: str
    postprocess_policy_hash: str
    runtime_hash: str
    derived_program_hash: str

    SCHEMA_VERSION = SCORE_POSTPROCESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "upstream_score_hash",
            "upstream_program_hash",
            "postprocess_program_hash",
            "postprocess_policy_hash",
            "runtime_hash",
            "derived_program_hash",
        ):
            require_hash(getattr(self, name), name)
        expected = canonical_content_hash(
            {
                "kind": "POSTPROCESSED_FACTOR_SCORE",
                "schema_version": SCORE_POSTPROCESS_SCHEMA_VERSION,
                "upstream_score_hash": self.upstream_score_hash,
                "upstream_program_hash": self.upstream_program_hash,
                "postprocess_program_hash": self.postprocess_program_hash,
                "postprocess_policy_hash": self.postprocess_policy_hash,
                "runtime_hash": self.runtime_hash,
            }
        )
        if self.derived_program_hash != expected:
            fail(FailureCode.INVALID_HASH, "derived postprocess program identity is inconsistent")


def derive_postprocessed_program_identity(
    *,
    upstream_score_hash: str,
    upstream_program_hash: str,
    program: ScorePostprocessProgram,
    policy: ScorePostprocessPolicy,
) -> PostprocessedProgramIdentity:
    for name, value in (
        ("upstream_score_hash", upstream_score_hash),
        ("upstream_program_hash", upstream_program_hash),
    ):
        require_hash(value, name)
    payload = {
        "kind": "POSTPROCESSED_FACTOR_SCORE",
        "schema_version": SCORE_POSTPROCESS_SCHEMA_VERSION,
        "upstream_score_hash": upstream_score_hash,
        "upstream_program_hash": upstream_program_hash,
        "postprocess_program_hash": program.artifact_hash,
        "postprocess_policy_hash": policy.artifact_hash,
        "runtime_hash": POSTPROCESS_RUNTIME_HASH,
    }
    return PostprocessedProgramIdentity(
        upstream_score_hash=upstream_score_hash,
        upstream_program_hash=upstream_program_hash,
        postprocess_program_hash=program.artifact_hash,
        postprocess_policy_hash=policy.artifact_hash,
        runtime_hash=POSTPROCESS_RUNTIME_HASH,
        derived_program_hash=canonical_content_hash(payload),
    )


def _reject_forbidden_field(field: str) -> None:
    normalized = field.casefold()
    if any(token in normalized for token in _FORBIDDEN_FIELD_TOKENS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "label or sealed fields cannot enter score postprocessing",
            field=field,
        )


__all__ = [
    "MAX_POSTPROCESS_STEPS",
    "POSTPROCESS_RUNTIME_HASH",
    "SCORE_POSTPROCESS_SCHEMA_VERSION",
    "PostprocessedProgramIdentity",
    "ScorePostprocessOperator",
    "ScorePostprocessPolicy",
    "ScorePostprocessProgram",
    "ScorePostprocessStep",
    "derive_postprocessed_program_identity",
]
