"""Dependency-free, offline reference expression runtime."""

from __future__ import annotations

import math
import time
from typing import Callable

from ..contracts import ContractValidationError
from .models import (
    FactorScoreArtifact,
    FactorScoreRow,
    FormulaExecutionFailure,
    FormulaExecutionRequest,
    QlibRuntimeManifest,
    ReferenceExpressionProgram,
    ReferenceOperation,
    RuntimeFailureCode,
    RuntimeFeatureBatch,
    RuntimeFeatureRow,
    RuntimeResult,
    scores_hash,
)
from .protocols import CertifiedExpressionIdentity, FrozenFeatureProvider


class ReferenceQlibExpressionRuntime:
    """Safe default runtime over a tiny repository-owned structured formula set.

    It is an integration/reference backend, not a replacement for pyqlib's
    operator implementations.  It proves the request, PIT provider, stable
    score artifact, timeout, and fail-closed contracts without an optional
    dependency.
    """

    def __init__(
        self,
        *,
        manifest: QlibRuntimeManifest,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manifest = manifest
        self._monotonic = monotonic

    @property
    def manifest(self) -> QlibRuntimeManifest:
        return self._manifest

    def _failure(
        self,
        request: FormulaExecutionRequest,
        code: RuntimeFailureCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> FormulaExecutionFailure:
        return FormulaExecutionFailure(
            request_hash=request.artifact_hash,
            runtime_hash=self.manifest.artifact_hash,
            code=code,
            message=message,
            retryable=retryable,
        )

    def _timed_out(self, started: float, request: FormulaExecutionRequest) -> bool:
        return (self._monotonic() - started) * 1000.0 > request.timeout_ms

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: CertifiedExpressionIdentity,
        provider: FrozenFeatureProvider,
    ) -> RuntimeResult:
        started = self._monotonic()
        if not self.manifest.enabled:
            return self._failure(request, RuntimeFailureCode.RUNTIME_DISABLED, "runtime is disabled")
        if request.runtime_hash != self.manifest.artifact_hash:
            return self._failure(request, RuntimeFailureCode.RUNTIME_MISMATCH, "runtime identity mismatch")
        if not isinstance(program, ReferenceExpressionProgram):
            return self._failure(
                request,
                RuntimeFailureCode.INVALID_REQUEST,
                "reference backend requires a structured reference program",
            )
        if (
            request.program_hash != program.artifact_hash
            or request.operator_manifest_hash != program.operator_manifest_hash
            or request.required_fields != tuple(sorted(program.input_fields))
        ):
            return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "program identity mismatch")
        try:
            batch = provider.load_features(request)
        except Exception as exc:
            return self._failure(
                request,
                RuntimeFailureCode.PROVIDER_UNAVAILABLE,
                f"frozen provider failed: {type(exc).__name__}",
                retryable=True,
            )
        if self._timed_out(started, request):
            return self._failure(
                request,
                RuntimeFailureCode.TIMEOUT,
                "formula execution timed out",
                retryable=True,
            )
        validation = self._validate_batch(request, batch)
        if validation is not None:
            return validation
        output: list[FactorScoreRow] = []
        try:
            for row in batch.rows:
                if self._timed_out(started, request):
                    return self._failure(
                        request,
                        RuntimeFailureCode.TIMEOUT,
                        "formula execution timed out",
                        retryable=True,
                    )
                output.append(
                    FactorScoreRow(
                        event_time=row.event_time,
                        symbol=row.symbol,
                        score=self._score(program, row),
                    )
                )
        except (ArithmeticError, ContractValidationError, KeyError, ValueError) as exc:
            return self._failure(
                request,
                RuntimeFailureCode.EXECUTION_FAILED,
                f"reference operator failed: {type(exc).__name__}",
            )
        if len(output) != request.expected_row_count:
            return self._failure(
                request,
                RuntimeFailureCode.PARTIAL_RESULT,
                "runtime produced an incomplete score set",
            )
        rows = tuple(sorted(output, key=lambda row: (row.event_time, row.symbol)))
        return FactorScoreArtifact(
            request_hash=request.artifact_hash,
            program_hash=program.artifact_hash,
            operator_manifest_hash=program.operator_manifest_hash,
            snapshot_hash=request.snapshot_hash,
            universe_hash=request.universe_hash,
            runtime_hash=self.manifest.artifact_hash,
            rows=rows,
            scores_hash=scores_hash(rows),
        )

    def _validate_batch(
        self,
        request: FormulaExecutionRequest,
        batch: object,
    ) -> FormulaExecutionFailure | None:
        if not isinstance(batch, RuntimeFeatureBatch):
            return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "provider returned invalid batch")
        if batch.snapshot_hash != request.snapshot_hash or batch.universe_hash != request.universe_hash:
            return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "frozen provider identity mismatch")
        if not batch.complete or len(batch.rows) != request.expected_row_count:
            return self._failure(
                request,
                RuntimeFailureCode.PARTIAL_RESULT,
                "frozen provider returned a partial batch",
            )
        if len(batch.rows) > request.max_output_rows:
            return self._failure(
                request,
                RuntimeFailureCode.OUTPUT_LIMIT_EXCEEDED,
                "frozen provider exceeded output policy",
            )
        for row in batch.rows:
            if row.event_time < request.start_time or row.event_time > request.end_time:
                return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "feature row is out of range")
            if row.available_at > request.decision_time:
                return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "feature row is not PIT-visible")
            if set(row.values) != set(request.required_fields):
                return self._failure(request, RuntimeFailureCode.INVALID_REQUEST, "feature fields mismatch")
        return None

    @staticmethod
    def _score(program: ReferenceExpressionProgram, row: RuntimeFeatureRow) -> float:
        values = tuple(row.values[field] for field in program.input_fields)
        operation = program.operation
        if operation is ReferenceOperation.FIELD:
            result = values[0]
        elif operation is ReferenceOperation.ADD:
            result = values[0] + values[1]
        elif operation is ReferenceOperation.SUBTRACT:
            result = values[0] - values[1]
        elif operation is ReferenceOperation.MULTIPLY:
            result = values[0] * values[1]
        elif operation is ReferenceOperation.DIVIDE:
            if values[1] == 0.0:
                raise ZeroDivisionError("reference division by zero")
            result = values[0] / values[1]
        elif operation is ReferenceOperation.SIGNED_POWER:
            assert program.exponent is not None
            result = math.copysign(abs(values[0]) ** program.exponent, values[0])
        else:  # pragma: no cover - enum validation makes this unreachable
            raise ValueError("unsupported reference operation")
        if not math.isfinite(result):
            raise ValueError("non-finite reference result")
        return float(result)


def default_expression_runtime(
    *,
    operator_policy_hash: str,
    code_hash: str,
) -> ReferenceQlibExpressionRuntime:
    """Return the network-free, dependency-free default backend."""

    return ReferenceQlibExpressionRuntime(
        manifest=QlibRuntimeManifest.reference(
            operator_policy_hash=operator_policy_hash,
            code_hash=code_hash,
        )
    )

