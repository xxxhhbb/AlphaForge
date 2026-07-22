"""Frozen expected semantics and conservative certification dispositions."""

from __future__ import annotations

from .models import OperatorSemanticPolicy, SemanticParityStatus, stable_hash


_COMMON = {"warmup": "partial_window_min_periods_1", "missing_output": "None"}

OPERATOR_POLICIES = {
    "Max": OperatorSemanticPolicy("Max", {**_COMMON, "nan": "ignore", "extreme": "maximum"}),
    "Min": OperatorSemanticPolicy("Min", {**_COMMON, "nan": "ignore", "extreme": "minimum"}),
    "IdxMax": OperatorSemanticPolicy(
        "IdxMax", {**_COMMON, "nan": "ignore", "ties": "first", "index_origin": "one_based_current_window"}
    ),
    "IdxMin": OperatorSemanticPolicy(
        "IdxMin", {**_COMMON, "nan": "ignore", "ties": "first", "index_origin": "one_based_current_window"}
    ),
    "Rank": OperatorSemanticPolicy(
        "Rank", {**_COMMON, "nan": "exclude_current_missing_propagates", "ties": "average", "scale": "percentile_0_1"}
    ),
    "WMA": OperatorSemanticPolicy(
        "WMA", {**_COMMON, "weights": "oldest_1_to_newest_N", "nan": "exclude_and_renormalize_surviving_weights"}
    ),
    "Cov": OperatorSemanticPolicy("Cov", {**_COMMON, "nan": "pairwise_complete", "ddof": 1}),
    "Corr": OperatorSemanticPolicy(
        "Corr", {**_COMMON, "nan": "pairwise_complete", "ddof": 1, "constant_atol": 0.00002}
    ),
    "Quantile": OperatorSemanticPolicy(
        "Quantile", {**_COMMON, "nan": "ignore", "interpolation": "linear", "q_range": "closed_0_1"}
    ),
    "Rsquare": OperatorSemanticPolicy(
        "Rsquare", {**_COMMON, "x": "one_based_window_position", "nan": "pairwise_complete", "constant_atol": 0.00002}
    ),
    "If": OperatorSemanticPolicy(
        "If", {"condition_true": "left", "condition_false": "right", "condition_missing": "propagate_missing"}
    ),
}

EXPECTED_DISPOSITIONS = {
    "Corr": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "Cov": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "IdxMax": SemanticParityStatus.KNOWN_DIFFERENCE,
    "IdxMin": SemanticParityStatus.KNOWN_DIFFERENCE,
    "If": SemanticParityStatus.KNOWN_DIFFERENCE,
    "Max": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "Min": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "Quantile": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "Rank": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "Rsquare": SemanticParityStatus.CERTIFIED_EQUIVALENT,
    "WMA": SemanticParityStatus.KNOWN_DIFFERENCE,
}

DIFFERENCE_CODES = {
    "IdxMax": ("QLIB_ARGMAX_NAN_POSITION",),
    "IdxMin": ("QLIB_ARGMIN_NAN_POSITION",),
    "If": ("QLIB_NAN_CONDITION_SELECTS_LEFT",),
    "WMA": ("QLIB_WMA_NANMEAN_SCALES_AND_DOES_NOT_RENORMALIZE",),
}

SEMANTIC_POLICY_HASH = stable_hash(
    {
        "policies": [OPERATOR_POLICIES[name].to_canonical() for name in sorted(OPERATOR_POLICIES)],
        "expected_dispositions": {name: EXPECTED_DISPOSITIONS[name].value for name in sorted(EXPECTED_DISPOSITIONS)},
        "difference_codes": {name: list(DIFFERENCE_CODES[name]) for name in sorted(DIFFERENCE_CODES)},
    }
)
