from __future__ import annotations

from datetime import datetime, timezone

from veriquant.qlib_expression_runtime import (
    ENABLE_ENV,
    FormulaExecutionFailure,
    FormulaExecutionRequest,
    OptionalPyQlibExpressionRuntime,
    ReferenceExpressionProgram,
    ReferenceOperation,
    RuntimeFailureCode,
)


UTC = timezone.utc
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def at(day: int) -> datetime:
    return datetime(2024, 1, day, 16, 0, tzinfo=UTC)


def program() -> ReferenceExpressionProgram:
    return ReferenceExpressionProgram(
        program_id="optional-probe-program",
        operator_manifest_hash=HASH_A,
        operation=ReferenceOperation.FIELD,
        input_fields=("close",),
    )


def request(runtime: OptionalPyQlibExpressionRuntime) -> FormulaExecutionRequest:
    compiled = program()
    return FormulaExecutionRequest(
        request_id="optional-request",
        program_hash=compiled.artifact_hash,
        operator_manifest_hash=compiled.operator_manifest_hash,
        snapshot_hash=HASH_C,
        universe_hash=HASH_D,
        runtime_hash=runtime.manifest.artifact_hash,
        required_fields=("close",),
        start_time=at(1),
        end_time=at(1),
        decision_time=at(2),
        expected_row_count=1,
        max_output_rows=1,
        timeout_ms=100,
        seed=0,
    )


def test_optional_runtime_disabled_does_not_probe(monkeypatch) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    runtime = OptionalPyQlibExpressionRuntime(operator_policy_hash=HASH_A, code_hash=HASH_B)
    monkeypatch.setattr(
        runtime,
        "_configured_environment_versions",
        lambda: (_ for _ in ()).throw(AssertionError("disabled runtime probed dependency")),
    )

    manifest = runtime.manifest
    result = runtime.execute(request=request(runtime), program=program(), provider=object())

    assert manifest.enabled is False
    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.RUNTIME_DISABLED


def test_missing_dependency_is_typed_and_default_tests_do_not_require_pyqlib(monkeypatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    runtime = OptionalPyQlibExpressionRuntime(operator_policy_hash=HASH_A, code_hash=HASH_B)
    monkeypatch.setattr(runtime, "_configured_environment_versions", lambda: None)

    result = runtime.execute(request=request(runtime), program=program(), provider=object())

    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.RUNTIME_UNAVAILABLE
    assert "configured local Python" in result.message


def test_explicit_local_probe_versions_are_bound_into_runtime_hash(monkeypatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    left = OptionalPyQlibExpressionRuntime(operator_policy_hash=HASH_A, code_hash=HASH_B)
    right = OptionalPyQlibExpressionRuntime(operator_policy_hash=HASH_A, code_hash=HASH_B)
    monkeypatch.setattr(
        left,
        "_configured_environment_versions",
        lambda: ("3.11.15", {"pyqlib": "0.9.7", "numpy": "2.4.6", "pandas": "2.3.3", "scipy": "1.17.1"}),
    )
    monkeypatch.setattr(
        right,
        "_configured_environment_versions",
        lambda: ("3.11.15", {"pyqlib": "0.9.8", "numpy": "2.4.6", "pandas": "2.3.3", "scipy": "1.17.1"}),
    )

    assert left.manifest.enabled is True
    assert left.manifest.dependency_versions["pyqlib"] == "0.9.7"
    assert left.manifest.artifact_hash != right.manifest.artifact_hash


def test_detected_dependency_still_cannot_execute_without_fixed_worker(monkeypatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    runtime = OptionalPyQlibExpressionRuntime(operator_policy_hash=HASH_A, code_hash=HASH_B)
    monkeypatch.setattr(
        runtime,
        "_configured_environment_versions",
        lambda: ("3.11.15", {"pyqlib": "0.9.7"}),
    )

    result = runtime.execute(request=request(runtime), program=program(), provider=object())
    assert isinstance(result, FormulaExecutionFailure)
    assert result.code is RuntimeFailureCode.RUNTIME_UNAVAILABLE
    assert "fixed reviewed" in result.message

