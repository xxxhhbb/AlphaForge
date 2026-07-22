from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.qlib_expression_runtime import (
    FactorScoreArtifact,
    FormulaExecutionBackend,
    FormulaExecutionRequest,
    QlibRuntimeManifest,
    ReferenceExpressionProgram,
    ReferenceOperation,
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


def runtime_manifest() -> QlibRuntimeManifest:
    return QlibRuntimeManifest.reference(operator_policy_hash=HASH_A, code_hash=HASH_B)


def program() -> ReferenceExpressionProgram:
    return ReferenceExpressionProgram(
        program_id="reference-subtraction",
        operator_manifest_hash=HASH_A,
        operation=ReferenceOperation.SUBTRACT,
        input_fields=("close", "open"),
    )


def request(manifest: QlibRuntimeManifest, compiled: ReferenceExpressionProgram) -> FormulaExecutionRequest:
    return FormulaExecutionRequest(
        request_id="formula-request-1",
        program_hash=compiled.artifact_hash,
        operator_manifest_hash=compiled.operator_manifest_hash,
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        runtime_hash=manifest.artifact_hash,
        required_fields=("open", "close"),
        start_time=at(1),
        end_time=at(2),
        decision_time=at(3),
        expected_row_count=2,
        max_output_rows=10,
        timeout_ms=1_000,
        seed=7,
    )


def test_request_has_no_raw_expression_or_executable_surface() -> None:
    names = {field.name for field in fields(FormulaExecutionRequest)}

    assert "expression" not in names
    assert "python" not in names
    assert "module" not in names
    assert "file" not in names
    assert names >= {"program_hash", "snapshot_hash", "runtime_hash", "required_fields"}


@pytest.mark.parametrize("field_name", ["future_return_5d", "target_label", "sealed_feature"])
def test_request_rejects_label_and_sealed_fields(field_name: str) -> None:
    manifest = runtime_manifest()
    compiled = program()
    with pytest.raises(ContractValidationError) as caught:
        FormulaExecutionRequest(
            request_id="forbidden-request",
            program_hash=compiled.artifact_hash,
            operator_manifest_hash=compiled.operator_manifest_hash,
            snapshot_hash=HASH_C,
            universe_hash=HASH_D,
            runtime_hash=manifest.artifact_hash,
            required_fields=(field_name,),
            start_time=at(1),
            end_time=at(2),
            decision_time=at(3),
            expected_row_count=1,
            max_output_rows=1,
            timeout_ms=100,
            seed=0,
        )

    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_runtime_manifest_hash_binds_versions_and_security_policy() -> None:
    left = runtime_manifest()
    same = runtime_manifest()
    changed = QlibRuntimeManifest(
        runtime_id=left.runtime_id,
        backend=left.backend,
        implementation=left.implementation,
        implementation_version="1.0.1",
        python_version=left.python_version,
        dependency_versions=left.dependency_versions,
        operator_policy_hash=left.operator_policy_hash,
        code_hash=left.code_hash,
        enabled=True,
    )

    assert left.artifact_hash == same.artifact_hash
    assert changed.artifact_hash != left.artifact_hash
    with pytest.raises(ContractValidationError) as caught:
        QlibRuntimeManifest(
            runtime_id="unsafe-runtime",
            backend=left.backend,
            implementation="unsafe-runtime",
            implementation_version="1",
            python_version=left.python_version,
            dependency_versions={},
            operator_policy_hash=HASH_A,
            code_hash=HASH_B,
            enabled=True,
            native_string_parser_enabled=True,
        )
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_feature_batch_sorts_rows_and_rejects_duplicate_keys() -> None:
    later = RuntimeFeatureRow(
        event_time=at(2),
        available_at=at(2),
        symbol="BBB",
        values={"close": 12.0, "open": 10.0},
    )
    earlier = RuntimeFeatureRow(
        event_time=at(1),
        available_at=at(1),
        symbol="AAA",
        values={"close": 10.0, "open": 9.0},
    )
    batch = RuntimeFeatureBatch(
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        rows=(later, earlier),
        complete=True,
    )

    assert tuple(row.symbol for row in batch.rows) == ("AAA", "BBB")
    with pytest.raises(ContractValidationError):
        RuntimeFeatureBatch(
            snapshot_hash=HASH_C,
            universe_hash=HASH_D,
            rows=(earlier, earlier),
            complete=True,
        )


def test_runtime_protocol_is_structural() -> None:
    from veriquant.qlib_expression_runtime import (
        CertifiedExpressionIdentity,
        ReferenceQlibExpressionRuntime,
    )

    runtime = ReferenceQlibExpressionRuntime(manifest=runtime_manifest())
    assert isinstance(runtime, FormulaExecutionBackend)
    assert isinstance(program(), CertifiedExpressionIdentity)


def test_score_artifact_rejects_wrong_rows_hash() -> None:
    from veriquant.qlib_expression_runtime import FactorScoreRow

    row = FactorScoreRow(event_time=at(1), symbol="AAA", score=1.0)
    with pytest.raises(ContractValidationError) as caught:
        FactorScoreArtifact(
            request_hash=HASH_A,
            program_hash=HASH_B,
            operator_manifest_hash=HASH_C,
            snapshot_hash=HASH_D,
            universe_hash=HASH_A,
            runtime_hash=HASH_B,
            rows=(row,),
            scores_hash=HASH_C,
        )
    assert caught.value.code is FailureCode.INVALID_HASH
