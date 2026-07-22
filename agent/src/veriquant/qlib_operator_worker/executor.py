"""Deterministic in-memory execution of certified Qlib operator trees."""

from __future__ import annotations

import math

from .compiler import StructuredQlibCompiler
from .models import (
    QlibFactorScoreArtifact,
    QlibFactorScoreRow,
    QlibOperatorExecutionRequest,
    QlibOperatorWorkerError,
    QlibOperatorWorkerFailure,
    QlibOperatorWorkerFailureCode,
    build_score_artifact,
)
from .runtime import QlibRuntimeLoadFailure, load_pinned_qlib_runtime


def _failure(
    request: QlibOperatorExecutionRequest,
    code: QlibOperatorWorkerFailureCode,
    message: str,
    *,
    retryable: bool = False,
) -> QlibOperatorWorkerFailure:
    return QlibOperatorWorkerFailure(
        code=code,
        message=message,
        program_hash=request.program.expression_hash,
        input_hash=request.input_hash,
        retryable=retryable,
    )


def _input_columns(request: QlibOperatorExecutionRequest) -> tuple[tuple[object, ...], dict[str, dict[str, tuple[float, ...]]]]:
    ordered = sorted(request.rows, key=lambda row: (row.symbol, row.event_time))
    calendar = tuple(sorted({row.event_time for row in request.rows}))
    by_key = {(row.symbol, row.event_time): row for row in ordered}
    symbols = tuple(sorted({row.symbol for row in ordered}))
    values: dict[str, dict[str, tuple[float, ...]]] = {}
    for field in request.program.required_fields:
        values[field] = {
            symbol: tuple(
                float("nan") if by_key[(symbol, event_time)].values[field] is None else float(by_key[(symbol, event_time)].values[field])
                for event_time in calendar
            )
            for symbol in symbols
        }
    return calendar, values


def execute_certified_qlib_operators(
    request: QlibOperatorExecutionRequest,
) -> QlibFactorScoreArtifact | QlibOperatorWorkerFailure:
    """Execute a typed request; no expression string is accepted by this API."""

    if type(request) is not QlibOperatorExecutionRequest:
        raise QlibOperatorWorkerError(
            QlibOperatorWorkerFailureCode.INVALID_REQUEST,
            "worker accepts only QlibOperatorExecutionRequest",
        )
    bindings = load_pinned_qlib_runtime()
    if isinstance(bindings, QlibRuntimeLoadFailure):
        return _failure(request, bindings.code, bindings.message)
    calendar, values = _input_columns(request)
    namespace = request.input_hash.removeprefix("sha256:")[:24]
    bindings.clear_cache()
    try:
        compiled = StructuredQlibCompiler(
            bindings=bindings,
            values_by_field=values,
            namespace=namespace,
        ).compile(request.program.root)
        scores: list[QlibFactorScoreRow] = []
        for symbol in request.output_symbols:
            series = compiled.load(symbol, 0, len(calendar) - 1)
            raw_values = tuple(series.tolist())
            if len(raw_values) != len(calendar):
                return _failure(
                    request,
                    QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT,
                    "Qlib operator output length does not match frozen calendar",
                )
            for event_time, raw in zip(calendar, raw_values, strict=True):
                if raw is None:
                    score = None
                else:
                    try:
                        converted = float(raw)
                    except (TypeError, ValueError):
                        return _failure(
                            request,
                            QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT,
                            "Qlib operator emitted a non-numeric value",
                        )
                    score = converted if math.isfinite(converted) else None
                scores.append(QlibFactorScoreRow(event_time=event_time, symbol=symbol, score=score))
        return build_score_artifact(request, bindings.identity, tuple(scores))
    except QlibOperatorWorkerError as exc:
        return _failure(request, exc.code, str(exc))
    except Exception as exc:
        return _failure(
            request,
            QlibOperatorWorkerFailureCode.EXECUTION_FAILED,
            f"pinned Qlib operator execution failed: {type(exc).__name__}",
        )
    finally:
        bindings.clear_cache()
