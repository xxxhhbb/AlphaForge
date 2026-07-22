from __future__ import annotations

import json
from datetime import datetime, timezone

from veriquant.qlib_expression_runtime import (
    FactorScoreArtifact,
    FormulaExecutionFailure,
    FormulaExecutionRequest,
    QlibRuntimeManifest,
    ReferenceExpressionProgram,
    ReferenceOperation,
    ReferenceQlibExpressionRuntime,
    RuntimeFailureCode,
    RuntimeFeatureBatch,
    RuntimeFeatureRow,
)


UTC = timezone.utc
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def at(day: int) -> datetime:
    return datetime(2024, 1, day, 16, 0, tzinfo=UTC)


class Provider:
    def __init__(self, batch: RuntimeFeatureBatch) -> None:
        self.batch = batch

    def load_features(self, request: FormulaExecutionRequest) -> RuntimeFeatureBatch:
        del request
        return self.batch


def manifest() -> QlibRuntimeManifest:
    return QlibRuntimeManifest.reference(operator_policy_hash=HASH_A, code_hash=HASH_B)


def program(operation: ReferenceOperation = ReferenceOperation.SUBTRACT) -> ReferenceExpressionProgram:
    return ReferenceExpressionProgram(
        program_id=f"reference-{operation.value.lower()}",
        operator_manifest_hash=HASH_A,
        operation=operation,
        input_fields=("close", "open"),
    )


def request(
    runtime_manifest: QlibRuntimeManifest,
    compiled: ReferenceExpressionProgram,
    *,
    expected: int = 2,
    timeout_ms: int = 1_000,
) -> FormulaExecutionRequest:
    return FormulaExecutionRequest(
        request_id="reference-request",
        program_hash=compiled.artifact_hash,
        operator_manifest_hash=compiled.operator_manifest_hash,
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        runtime_hash=runtime_manifest.artifact_hash,
        required_fields=("open", "close"),
        start_time=at(1),
        end_time=at(2),
        decision_time=at(3),
        expected_row_count=expected,
        max_output_rows=10,
        timeout_ms=timeout_ms,
        seed=7,
    )


def batch(*, complete: bool = True, future_available: bool = False) -> RuntimeFeatureBatch:
    return RuntimeFeatureBatch(
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        rows=(
            RuntimeFeatureRow(
                event_time=at(2),
                available_at=at(4) if future_available else at(2),
                symbol="BBB",
                values={"close": 12.0, "open": 10.0},
            ),
            RuntimeFeatureRow(
                event_time=at(1),
                available_at=at(1),
                symbol="AAA",
                values={"close": 10.0, "open": 9.0},
            ),
        ),
        complete=complete,
    )


def test_reference_runtime_is_deterministic_and_stably_sorted() -> None:
    runtime_manifest = manifest()
    compiled = program()
    execution_request = request(runtime_manifest, compiled)
    runtime = ReferenceQlibExpressionRuntime(manifest=runtime_manifest)

    first = runtime.execute(
        request=execution_request,
        program=compiled,
        provider=Provider(batch()),
    )
    second = runtime.execute(
        request=execution_request,
        program=compiled,
        provider=Provider(batch()),
    )

    assert isinstance(first, FactorScoreArtifact)
    assert isinstance(second, FactorScoreArtifact)
    assert first.artifact_hash == second.artifact_hash
    assert first.scores_hash == second.scores_hash
    assert tuple((row.symbol, row.score) for row in first.rows) == (("AAA", 1.0), ("BBB", 2.0))
    rendered = json.dumps(first.to_dict(), sort_keys=True).lower()
    assert "label" not in rendered
    assert "sealed" not in rendered
    assert "official_metrics" not in rendered


def test_partial_provider_result_fails_closed_without_score_artifact() -> None:
    runtime_manifest = manifest()
    compiled = program()
    result = ReferenceQlibExpressionRuntime(manifest=runtime_manifest).execute(
        request=request(runtime_manifest, compiled),
        program=compiled,
        provider=Provider(batch(complete=False)),
    )

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.PARTIAL_RESULT
    assert not hasattr(result, "rows")


def test_row_count_mismatch_fails_closed() -> None:
    runtime_manifest = manifest()
    compiled = program()
    result = ReferenceQlibExpressionRuntime(manifest=runtime_manifest).execute(
        request=request(runtime_manifest, compiled, expected=1),
        program=compiled,
        provider=Provider(batch()),
    )

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.PARTIAL_RESULT


def test_future_available_feature_fails_closed() -> None:
    runtime_manifest = manifest()
    compiled = program()
    result = ReferenceQlibExpressionRuntime(manifest=runtime_manifest).execute(
        request=request(runtime_manifest, compiled),
        program=compiled,
        provider=Provider(batch(future_available=True)),
    )

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.INVALID_REQUEST


def test_timeout_is_typed_and_carries_no_partial_rows() -> None:
    runtime_manifest = manifest()
    compiled = program()
    ticks = iter((0.0, 0.1))
    runtime = ReferenceQlibExpressionRuntime(
        manifest=runtime_manifest,
        monotonic=lambda: next(ticks),
    )

    result = runtime.execute(
        request=request(runtime_manifest, compiled, timeout_ms=50),
        program=compiled,
        provider=Provider(batch()),
    )

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.TIMEOUT
    assert result.retryable is True
    assert not hasattr(result, "rows")


def test_runtime_and_program_identity_mismatches_fail_closed() -> None:
    runtime_manifest = manifest()
    compiled = program()
    mismatched = FormulaExecutionRequest(
        request_id="mismatch",
        program_hash=HASH_B,
        operator_manifest_hash=compiled.operator_manifest_hash,
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        runtime_hash=runtime_manifest.artifact_hash,
        required_fields=("open", "close"),
        start_time=at(1),
        end_time=at(2),
        decision_time=at(3),
        expected_row_count=2,
        max_output_rows=2,
        timeout_ms=100,
        seed=0,
    )

    result = ReferenceQlibExpressionRuntime(manifest=runtime_manifest).execute(
        request=mismatched,
        program=compiled,
        provider=Provider(batch()),
    )
    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.INVALID_REQUEST


def test_operator_failure_returns_typed_terminal_failure() -> None:
    runtime_manifest = manifest()
    compiled = program(ReferenceOperation.DIVIDE)
    zero_batch = RuntimeFeatureBatch(
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        rows=(
            RuntimeFeatureRow(event_time=at(1), available_at=at(1), symbol="AAA", values={"close": 1.0, "open": 0.0}),
            RuntimeFeatureRow(event_time=at(2), available_at=at(2), symbol="BBB", values={"close": 2.0, "open": 1.0}),
        ),
        complete=True,
    )
    result = ReferenceQlibExpressionRuntime(manifest=runtime_manifest).execute(
        request=request(runtime_manifest, compiled),
        program=compiled,
        provider=Provider(zero_batch),
    )

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.EXECUTION_FAILED
    assert not hasattr(result, "rows")

