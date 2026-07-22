"""Deterministic, system-owned implementations of parity-sensitive semantics."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Sequence

from .models import (
    AdapterFailureCode,
    SemanticAdapterError,
    SemanticAdapterPolicy,
    SemanticAdapterResult,
    canonical_hash,
)


Value = float | int | None


_COMMON_WINDOW = {
    "warmup": "partial_window_min_periods_1",
    "missing_input": "None_or_nan",
    "missing_output": "None",
}

_POLICIES = {
    "SafeIf": SemanticAdapterPolicy(
        name="SafeIf",
        arity=3,
        windowed=False,
        semantics={
            "condition_true": "left",
            "condition_false": "right",
            "condition_missing": "propagate_missing",
            "native_pyqlib_0_9_7": "KNOWN_DIFFERENCE_DO_NOT_SUBSTITUTE",
        },
    ),
    "SafeIdxMax": SemanticAdapterPolicy(
        name="SafeIdxMax",
        arity=1,
        windowed=True,
        semantics={
            **_COMMON_WINDOW,
            "extreme": "maximum",
            "missing": "ignore_positions_retained",
            "ties": "first",
            "index_origin": "one_based_current_window",
            "all_missing": "None",
            "native_pyqlib_0_9_7": "KNOWN_DIFFERENCE_DO_NOT_SUBSTITUTE",
        },
    ),
    "SafeIdxMin": SemanticAdapterPolicy(
        name="SafeIdxMin",
        arity=1,
        windowed=True,
        semantics={
            **_COMMON_WINDOW,
            "extreme": "minimum",
            "missing": "ignore_positions_retained",
            "ties": "first",
            "index_origin": "one_based_current_window",
            "all_missing": "None",
            "native_pyqlib_0_9_7": "KNOWN_DIFFERENCE_DO_NOT_SUBSTITUTE",
        },
    ),
    "SafeWMA": SemanticAdapterPolicy(
        name="SafeWMA",
        arity=1,
        windowed=True,
        semantics={
            **_COMMON_WINDOW,
            "weights": "oldest_1_to_newest_N",
            "missing": "exclude_and_renormalize_surviving_weights",
            "all_missing": "None",
            "native_pyqlib_0_9_7": "KNOWN_DIFFERENCE_DO_NOT_SUBSTITUTE",
        },
    ),
}

BUILTIN_ADAPTER_POLICIES = MappingProxyType(_POLICIES)
ADAPTER_POLICY_HASH = canonical_hash(
    {"adapters": [_POLICIES[name].to_canonical() for name in sorted(_POLICIES)]}
)


def _fail(code: AdapterFailureCode, message: str) -> None:
    raise SemanticAdapterError(code, message)


def _series(values: Sequence[Value], label: str) -> tuple[float | None, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        _fail(AdapterFailureCode.INVALID_INPUT, f"{label} must be a non-empty numeric sequence")
    normalized: list[float | None] = []
    for value in values:
        if value is None:
            normalized.append(None)
            continue
        if isinstance(value, bool):
            _fail(AdapterFailureCode.INVALID_INPUT, f"{label} contains a boolean")
        try:
            number = float(value)
        except (TypeError, ValueError):
            _fail(AdapterFailureCode.INVALID_INPUT, f"{label} contains a non-numeric value")
        if math.isnan(number):
            normalized.append(None)
        elif not math.isfinite(number):
            _fail(AdapterFailureCode.INVALID_INPUT, f"{label} contains an infinite value")
        else:
            normalized.append(number)
    return tuple(normalized)


def _window_size(window: object) -> int:
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        _fail(AdapterFailureCode.INVALID_WINDOW, "window must be a positive integer")
    return window


def _request_hash(name: str, series: tuple[tuple[float | None, ...], ...], window: int | None) -> str:
    return canonical_hash({"adapter": name, "series": series, "window": window})


def _result(
    name: str,
    source: tuple[tuple[float | None, ...], ...],
    values: tuple[float | None, ...],
    window: int | None,
) -> SemanticAdapterResult:
    policy = _POLICIES[name]
    return SemanticAdapterResult(
        adapter_name=name,
        values=values,
        policy_hash=policy.policy_hash,
        adapter_hash=policy.adapter_hash,
        request_hash=_request_hash(name, source, window),
    )


def safe_if(condition: Sequence[Value], left: Sequence[Value], right: Sequence[Value]) -> SemanticAdapterResult:
    source = (_series(condition, "condition"), _series(left, "left"), _series(right, "right"))
    if len({len(values) for values in source}) != 1:
        _fail(AdapterFailureCode.SHAPE_MISMATCH, "SafeIf inputs must have equal length")
    values = tuple(
        None if flag is None else (lhs if bool(flag) else rhs)
        for flag, lhs, rhs in zip(*source, strict=True)
    )
    return _result("SafeIf", source, values, None)


def _safe_extreme_index(values: Sequence[Value], window: object, *, maximum: bool) -> SemanticAdapterResult:
    source = (_series(values, "values"),)
    size = _window_size(window)
    output: list[float | None] = []
    for index in range(len(source[0])):
        current = source[0][max(0, index - size + 1) : index + 1]
        present = tuple((offset + 1, value) for offset, value in enumerate(current) if value is not None)
        if not present:
            output.append(None)
            continue
        target = (max if maximum else min)(value for _, value in present)
        output.append(float(next(position for position, value in present if value == target)))
    name = "SafeIdxMax" if maximum else "SafeIdxMin"
    return _result(name, source, tuple(output), size)


def safe_idx_max(values: Sequence[Value], window: object) -> SemanticAdapterResult:
    return _safe_extreme_index(values, window, maximum=True)


def safe_idx_min(values: Sequence[Value], window: object) -> SemanticAdapterResult:
    return _safe_extreme_index(values, window, maximum=False)


def safe_wma(values: Sequence[Value], window: object) -> SemanticAdapterResult:
    source = (_series(values, "values"),)
    size = _window_size(window)
    output: list[float | None] = []
    for index in range(len(source[0])):
        current = source[0][max(0, index - size + 1) : index + 1]
        weighted = tuple((offset + 1, value) for offset, value in enumerate(current) if value is not None)
        if not weighted:
            output.append(None)
            continue
        denominator = sum(weight for weight, _ in weighted)
        output.append(sum(weight * value for weight, value in weighted) / denominator)
    return _result("SafeWMA", source, tuple(output), size)


def apply_builtin_adapter(
    name: str,
    *series: Sequence[Value],
    window: object | None = None,
) -> SemanticAdapterResult:
    """Apply one closed-catalog adapter; no registration or callback surface exists."""

    if name not in _POLICIES:
        _fail(AdapterFailureCode.UNKNOWN_ADAPTER, "adapter is not in the system-owned catalog")
    policy = _POLICIES[name]
    if len(series) != policy.arity:
        _fail(AdapterFailureCode.INVALID_INPUT, "adapter input arity does not match its policy")
    if policy.windowed and window is None:
        _fail(AdapterFailureCode.INVALID_WINDOW, "windowed adapter requires a fixed window")
    if not policy.windowed and window is not None:
        _fail(AdapterFailureCode.INVALID_WINDOW, "non-windowed adapter rejects a window")
    if name == "SafeIf":
        return safe_if(series[0], series[1], series[2])
    if name == "SafeIdxMax":
        return safe_idx_max(series[0], window)
    if name == "SafeIdxMin":
        return safe_idx_min(series[0], window)
    return safe_wma(series[0], window)


__all__ = [
    "ADAPTER_POLICY_HASH",
    "BUILTIN_ADAPTER_POLICIES",
    "apply_builtin_adapter",
    "safe_idx_max",
    "safe_idx_min",
    "safe_if",
    "safe_wma",
]
