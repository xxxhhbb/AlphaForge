from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.qlib_expression.api import (
    QlibExpressionPolicy,
    QlibFieldRole,
    certify_qlib_expression,
)
from src.veriquant.qlib_expression.models import CallNode, FieldNode, StringNode
from src.veriquant.qlib_operator_worker import executor
from src.veriquant.qlib_operator_worker.api import (
    QlibFactorScoreArtifact,
    QlibFactorScoreRow,
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    QlibOperatorWorkerError,
    QlibOperatorWorkerFailure,
    QlibOperatorWorkerFailureCode,
    QlibRuntimeIdentity,
    WORKER_OPERATOR_NAMES,
    execute_certified_qlib_operators,
)
from src.veriquant.qlib_operator_worker.models import build_score_artifact
from src.veriquant.qlib_operator_worker.runtime import QlibRuntimeLoadFailure


H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def _program(expression: str = "Mean($close,3)"):
    return certify_qlib_expression(
        expression,
        policy=QlibExpressionPolicy(
            field_roles={"close": QlibFieldRole.FEATURE, "volume": QlibFieldRole.FEATURE},
            max_window=20,
            max_ref_periods=20,
        ),
    )


def _rows(*, extra: bool = False, calendar_shift: bool = False) -> tuple[QlibInMemoryFeatureRow, ...]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    for symbol, base in (("SH600000", 1.0), ("SZ000001", 10.0)):
        for index in range(6):
            event_time = start + timedelta(days=index + (1 if calendar_shift and symbol == "SZ000001" else 0))
            values = {"close": base + index}
            if extra:
                values["future_return_20d"] = 0.5
            result.append(QlibInMemoryFeatureRow(event_time, symbol, values))
    return tuple(result)


def _request(program=None, rows=None) -> QlibOperatorExecutionRequest:
    return QlibOperatorExecutionRequest(
        program=program or _program(),
        snapshot_hash=H1,
        universe_hash=H2,
        output_symbols=("SH600000", "SZ000001"),
        rows=rows or _rows(),
    )


def test_worker_api_rejects_raw_expression_string() -> None:
    with pytest.raises(QlibOperatorWorkerError) as caught:
        execute_certified_qlib_operators("Mean($close,3)")  # type: ignore[arg-type]
    assert caught.value.code is QlibOperatorWorkerFailureCode.INVALID_REQUEST


def test_default_or_forced_missing_runtime_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor,
        "load_pinned_qlib_runtime",
        lambda: QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_UNAVAILABLE,
            "pyqlib unavailable in unit runtime",
        ),
    )
    result = execute_certified_qlib_operators(_request())
    assert isinstance(result, QlibOperatorWorkerFailure)
    assert result.code is QlibOperatorWorkerFailureCode.QLIB_RUNTIME_UNAVAILABLE
    assert result.retryable is False


def test_tampered_certified_metadata_is_rejected_before_runtime() -> None:
    tampered = replace(_program(), operators=("Std",))
    with pytest.raises(QlibOperatorWorkerError) as caught:
        _request(program=tampered)
    assert caught.value.code is QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_structured_excluded_operator_is_rejected_even_with_consistent_identity() -> None:
    original = _program()
    root = CallNode("TResample", (FieldNode("close"), StringNode("1D"), StringNode("mean")))
    canonical = 'TResample($close,"1D","mean")'
    identity = {
        "schema_version": original.schema_version,
        "canonical_expression": canonical,
        "manifest_hash": original.manifest_hash,
        "policy_hash": original.policy_hash,
    }
    structured = replace(
        original,
        root=root,
        canonical_expression=canonical,
        expression_hash=_hash(identity),
        operators=("TResample",),
        required_fields=("close",),
        node_count=4,
    )
    with pytest.raises(QlibOperatorWorkerError) as caught:
        _request(program=structured)
    assert caught.value.code is QlibOperatorWorkerFailureCode.EXCLUDED_OPERATOR


def test_rows_cannot_smuggle_label_or_extra_fields() -> None:
    with pytest.raises(QlibOperatorWorkerError) as caught:
        _request(rows=_rows(extra=True))
    assert caught.value.code is QlibOperatorWorkerFailureCode.DATA_SCHEMA_MISMATCH


def test_instrument_calendars_must_match_exactly() -> None:
    with pytest.raises(QlibOperatorWorkerError) as caught:
        _request(rows=_rows(calendar_shift=True))
    assert caught.value.code is QlibOperatorWorkerFailureCode.CALENDAR_MISMATCH


def test_duplicate_rows_and_nonfinite_inputs_fail_closed() -> None:
    rows = _rows()
    with pytest.raises(QlibOperatorWorkerError) as caught:
        _request(rows=rows + (rows[0],))
    assert caught.value.code is QlibOperatorWorkerFailureCode.DUPLICATE_ROW

    with pytest.raises(QlibOperatorWorkerError) as caught:
        QlibInMemoryFeatureRow(rows[0].event_time, rows[0].symbol, {"close": float("inf")})
    assert caught.value.code is QlibOperatorWorkerFailureCode.NON_FINITE_INPUT


def test_score_artifact_identity_and_sort_are_deterministic() -> None:
    request = _request()
    rows = (
        QlibFactorScoreRow(request.rows[6].event_time, "SZ000001", 10.0),
        QlibFactorScoreRow(request.rows[0].event_time, "SH600000", None),
    )
    first = build_score_artifact(request, QlibRuntimeIdentity.expected(), rows)
    second = build_score_artifact(request, QlibRuntimeIdentity.expected(), tuple(reversed(rows)))
    assert isinstance(first, QlibFactorScoreArtifact)
    assert first.output_hash == second.output_hash
    assert first.rows == second.rows
    assert first.missing_scores == 1
    assert first.artifact_class == "FACTOR_SCORE_ARTIFACT"
    with pytest.raises(QlibOperatorWorkerError) as caught:
        replace(first, output_hash=H1)
    assert caught.value.code is QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT


def test_worker_mapping_covers_every_certified_executable_operator() -> None:
    assert len(WORKER_OPERATOR_NAMES) == 46
    for excluded in ("Rolling", "Feature", "PFeature", "TResample", "P", "PRef"):
        assert excluded not in WORKER_OPERATOR_NAMES
