"""Closed built-in policies used by the static compiler."""

from __future__ import annotations

from .models import COMPILER_VERSION, OperatorPolicy, PostprocessStep, SemanticAdapterPolicy, canonical_hash
from ..formula_semantic_adapters import BUILTIN_ADAPTER_POLICIES
from ..formula_semantic_adapters.models import canonical_hash as adapter_canonical_hash


NATIVE_OPERATORS = frozenset({
    "Abs", "Sign", "Log", "Not", "Add", "Sub", "Mul", "Div", "Power", "Gt", "Ge", "Lt", "Le", "Eq", "Ne", "And", "Or",
    "Ref", "Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med", "Mad", "Slope", "Rsquare", "Resi", "Rank", "Count",
    "Delta", "Quantile", "EMA", "Corr", "Cov",
})
ADAPTER_OPERATORS = frozenset({"SafeIf", "SafeIdxMax", "SafeIdxMin", "SafeWMA"})
NATIVE_TO_SAFE_ADAPTER = {
    "If": "SafeIf",
    "IdxMax": "SafeIdxMax",
    "IdxMin": "SafeIdxMin",
    "WMA": "SafeWMA",
}

ALIASES = {
    "abs": "Abs", "sign": "Sign", "log": "Log", "ref": "Ref", "delay": "Ref",
    "ts_max": "Max", "ts_min": "Min", "ts_sum": "Sum", "ts_mean": "Mean", "ts_std": "Std",
    "ts_var": "Var", "ts_rank": "Rank", "delta": "Delta", "ts_corr": "Corr", "ts_cov": "Cov",
    "ts_argmax": "SafeIdxMax", "ts_argmin": "SafeIdxMin", "decay_linear": "SafeWMA",
    "where": "SafeIf", "safe_if": "SafeIf", "safe_idxmax": "SafeIdxMax", "safe_idxmin": "SafeIdxMin", "safe_wma": "SafeWMA",
}

WINDOW_ARGUMENT = {
    "Ref": 1, "Max": 1, "Min": 1, "Sum": 1, "Mean": 1, "Std": 1, "Var": 1, "Skew": 1, "Kurt": 1,
    "Med": 1, "Mad": 1, "Slope": 1, "Rsquare": 1, "Resi": 1, "Rank": 1, "Count": 1, "Delta": 1,
    "EMA": 1, "Corr": 2, "Cov": 2, "Quantile": 1, "SafeIdxMax": 1, "SafeIdxMin": 1, "SafeWMA": 1,
}

ARGUMENT_COUNTS = {
    **{name: 1 for name in ("Abs", "Sign", "Log", "Not")},
    **{name: 2 for name in ("Add", "Sub", "Mul", "Div", "Power", "Gt", "Ge", "Lt", "Le", "Eq", "Ne", "And", "Or")},
    **{name: 2 for name in ("Ref", "Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med", "Mad", "Slope", "Rsquare", "Resi", "Rank", "Count", "Delta", "EMA", "SafeIdxMax", "SafeIdxMin", "SafeWMA")},
    **{name: 3 for name in ("Corr", "Cov", "Quantile", "SafeIf")},
}

POSTPROCESS_ALIASES = {
    "rank": "rank_cross_section", "rank_cross_section": "rank_cross_section",
    "zscore": "zscore_cross_section", "zscore_cross_section": "zscore_cross_section",
    "winsorize": "winsorize_cross_section", "winsorize_cross_section": "winsorize_cross_section",
    "industry_neutralize": "industry_neutralize", "size_neutralize": "size_neutralize",
}

ADAPTER_SEMANTICS = {
    "SafeIf": "true selects left; false selects right; missing condition propagates missing",
    "SafeIdxMax": "ignore missing; one-based original position; first tie; all missing returns missing",
    "SafeIdxMin": "ignore missing; one-based original position; first tie; all missing returns missing",
    "SafeWMA": "oldest-to-newest increasing weights; remove missing then renormalize; empty returns missing",
}


def operator_policy(name: str) -> OperatorPolicy:
    return OperatorPolicy(name, f"factor-ir.operator.{name}.v1", "1", "pinned deterministic FactorIR v2 semantics")


def adapter_policy(name: str) -> SemanticAdapterPolicy:
    installed = BUILTIN_ADAPTER_POLICIES[name]
    if installed.policy_hash != adapter_canonical_hash(installed.to_canonical()):
        raise ValueError("installed semantic adapter policy identity is invalid")
    return SemanticAdapterPolicy(
        name,
        installed.name,
        installed.catalog_version,
        canonical_hash(installed.to_canonical()),
        installed.policy_hash,
        installed.adapter_hash,
    )


def postprocess_step(name: str, parameters: dict[str, str | int | float | bool] | None = None) -> PostprocessStep:
    normalized = POSTPROCESS_ALIASES[name]
    return PostprocessStep(normalized, f"factor-ir.postprocess.{normalized}.v1", "1", parameters or {})


COMPILER_HASH = canonical_hash({
    "compiler_version": COMPILER_VERSION,
    "native_operators": sorted(NATIVE_OPERATORS),
    "adapter_semantics": ADAPTER_SEMANTICS,
    "aliases": ALIASES,
    "native_to_safe_adapter": NATIVE_TO_SAFE_ADAPTER,
    "postprocess": POSTPROCESS_ALIASES,
})
