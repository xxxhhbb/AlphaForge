"""Independent deterministic reference semantics for parity-sensitive operators.

These routines do not import Qlib or pandas.  Missing values are represented by
``None`` and outputs are finite floats or ``None``.
"""

from __future__ import annotations

import math
from typing import Sequence


Value = float | None


def _window(values: Sequence[Value], index: int, size: int) -> tuple[Value, ...]:
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("window must be a positive integer")
    return tuple(values[max(0, index - size + 1) : index + 1])


def _finite(value: object) -> float | None:
    if value is None:
        return None
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("reference inputs must be finite or None")
    return converted


def _validate(values: Sequence[Value]) -> tuple[Value, ...]:
    if not values:
        raise ValueError("reference input must be non-empty")
    return tuple(_finite(value) for value in values)


def rolling_extreme(values: Sequence[Value], window: int, *, maximum: bool) -> tuple[Value, ...]:
    source = _validate(values)
    result: list[Value] = []
    for index in range(len(source)):
        valid = [value for value in _window(source, index, window) if value is not None]
        result.append((max(valid) if maximum else min(valid)) if valid else None)
    return tuple(result)


def rolling_extreme_index(values: Sequence[Value], window: int, *, maximum: bool) -> tuple[Value, ...]:
    """First tie, 1-based position in the current window, missing values ignored."""

    source = _validate(values)
    result: list[Value] = []
    for index in range(len(source)):
        current = _window(source, index, window)
        valid = [(offset + 1, value) for offset, value in enumerate(current) if value is not None]
        if not valid:
            result.append(None)
            continue
        target = max(value for _, value in valid) if maximum else min(value for _, value in valid)
        result.append(float(next(offset for offset, value in valid if value == target)))
    return tuple(result)


def rolling_rank(values: Sequence[Value], window: int) -> tuple[Value, ...]:
    """Percentile rank of the current value with average ties and NaNs excluded."""

    source = _validate(values)
    result: list[Value] = []
    for index, current_value in enumerate(source):
        if current_value is None:
            result.append(None)
            continue
        valid = [value for value in _window(source, index, window) if value is not None]
        lower = sum(value < current_value for value in valid)
        equal = sum(value == current_value for value in valid)
        result.append((lower + (equal + 1) / 2.0) / len(valid))
    return tuple(result)


def rolling_wma(values: Sequence[Value], window: int) -> tuple[Value, ...]:
    """Oldest-to-newest weights 1..N, renormalized over non-missing values."""

    source = _validate(values)
    result: list[Value] = []
    for index in range(len(source)):
        current = _window(source, index, window)
        weighted = [(offset + 1, value) for offset, value in enumerate(current) if value is not None]
        if not weighted:
            result.append(None)
            continue
        denominator = sum(weight for weight, _ in weighted)
        result.append(sum(weight * value for weight, value in weighted) / denominator)
    return tuple(result)


def _paired(left: Sequence[Value], right: Sequence[Value]) -> tuple[tuple[Value, ...], tuple[Value, ...]]:
    lhs, rhs = _validate(left), _validate(right)
    if len(lhs) != len(rhs):
        raise ValueError("pair inputs must have equal length")
    return lhs, rhs


def rolling_cov(left: Sequence[Value], right: Sequence[Value], window: int) -> tuple[Value, ...]:
    """Pairwise-complete sample covariance (ddof=1)."""

    lhs, rhs = _paired(left, right)
    result: list[Value] = []
    for index in range(len(lhs)):
        pairs = [
            (x, y)
            for x, y in zip(_window(lhs, index, window), _window(rhs, index, window), strict=True)
            if x is not None and y is not None
        ]
        if len(pairs) < 2:
            result.append(None)
            continue
        mean_x = sum(x for x, _ in pairs) / len(pairs)
        mean_y = sum(y for _, y in pairs) / len(pairs)
        result.append(sum((x - mean_x) * (y - mean_y) for x, y in pairs) / (len(pairs) - 1))
    return tuple(result)


def rolling_corr(
    left: Sequence[Value], right: Sequence[Value], window: int, *, constant_atol: float = 2e-5
) -> tuple[Value, ...]:
    lhs, rhs = _paired(left, right)
    result: list[Value] = []
    for index in range(len(lhs)):
        left_window, right_window = _window(lhs, index, window), _window(rhs, index, window)
        pairs = [(x, y) for x, y in zip(left_window, right_window, strict=True) if x is not None and y is not None]
        left_valid = [x for x in left_window if x is not None]
        right_valid = [y for y in right_window if y is not None]
        if len(pairs) < 2 or len(left_valid) < 2 or len(right_valid) < 2:
            result.append(None)
            continue
        def sample_std(items: Sequence[float]) -> float:
            mean = sum(items) / len(items)
            return math.sqrt(sum((item - mean) ** 2 for item in items) / (len(items) - 1))
        if math.isclose(sample_std(left_valid), 0.0, abs_tol=constant_atol) or math.isclose(
            sample_std(right_valid), 0.0, abs_tol=constant_atol
        ):
            result.append(None)
            continue
        mean_x = sum(x for x, _ in pairs) / len(pairs)
        mean_y = sum(y for _, y in pairs) / len(pairs)
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        denominator = math.sqrt(
            sum((x - mean_x) ** 2 for x, _ in pairs) * sum((y - mean_y) ** 2 for _, y in pairs)
        )
        result.append(numerator / denominator if denominator else None)
    return tuple(result)


def rolling_quantile(values: Sequence[Value], window: int, quantile: float) -> tuple[Value, ...]:
    source = _validate(values)
    if isinstance(quantile, bool) or not 0.0 <= float(quantile) <= 1.0:
        raise ValueError("quantile must be from 0 to 1")
    result: list[Value] = []
    for index in range(len(source)):
        valid = sorted(value for value in _window(source, index, window) if value is not None)
        if not valid:
            result.append(None)
            continue
        position = float(quantile) * (len(valid) - 1)
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            result.append(valid[lower])
        else:
            fraction = position - lower
            result.append(valid[lower] + fraction * (valid[upper] - valid[lower]))
    return tuple(result)


def rolling_rsquare(values: Sequence[Value], window: int, *, constant_atol: float = 2e-5) -> tuple[Value, ...]:
    source = _validate(values)
    result: list[Value] = []
    for index in range(len(source)):
        current = _window(source, index, window)
        pairs = [(offset + 1.0, value) for offset, value in enumerate(current) if value is not None]
        valid = [value for _, value in pairs]
        if len(pairs) < 2:
            result.append(None)
            continue
        mean_y = sum(valid) / len(valid)
        std_y = math.sqrt(sum((value - mean_y) ** 2 for value in valid) / (len(valid) - 1))
        if math.isclose(std_y, 0.0, abs_tol=constant_atol):
            result.append(None)
            continue
        mean_x = sum(position for position, _ in pairs) / len(pairs)
        numerator = sum((position - mean_x) * (value - mean_y) for position, value in pairs)
        denominator = math.sqrt(
            sum((position - mean_x) ** 2 for position, _ in pairs)
            * sum((value - mean_y) ** 2 for _, value in pairs)
        )
        result.append((numerator / denominator) ** 2 if denominator else None)
    return tuple(result)


def conditional(
    condition: Sequence[Value], left: Sequence[Value], right: Sequence[Value]
) -> tuple[Value, ...]:
    conditions, lhs, rhs = _validate(condition), _validate(left), _validate(right)
    if not (len(conditions) == len(lhs) == len(rhs)):
        raise ValueError("conditional inputs must have equal length")
    return tuple(None if flag is None else (x if bool(flag) else y) for flag, x, y in zip(conditions, lhs, rhs, strict=True))
