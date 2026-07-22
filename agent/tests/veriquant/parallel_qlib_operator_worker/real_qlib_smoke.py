"""No-pytest smoke run by the isolated vibe-qlib Python 3.11 environment."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from src.veriquant.qlib_expression.api import QlibExpressionPolicy, QlibFieldRole, certify_qlib_expression
from src.veriquant.qlib_operator_worker.api import (
    QlibFactorScoreArtifact,
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    execute_certified_qlib_operators,
)


H1 = "sha256:" + "a" * 64
H2 = "sha256:" + "b" * 64
EXPRESSIONS = {
    "Ref": "Ref($close,2)",
    "Mean": "Mean($close,3)",
    "Std": "Std($close,3)",
    "Corr": "Corr($close,$volume,3)",
    "Rank": "Rank($close,3)",
    "If": "If($close>Mean($close,3),$close,0)",
}


def rows(required_fields: tuple[str, ...]) -> tuple[QlibInMemoryFeatureRow, ...]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    for symbol, offset in (("SH600000", 0.0), ("SZ000001", 10.0)):
        for index in range(8):
            close = offset + index + 1.0
            result.append(
                QlibInMemoryFeatureRow(
                    event_time=start + timedelta(days=index),
                    symbol=symbol,
                    values={
                        field: close if field == "close" else close * 100.0
                        for field in required_fields
                    },
                )
            )
    return tuple(result)


def main() -> int:
    policy = QlibExpressionPolicy(
        field_roles={"close": QlibFieldRole.FEATURE, "volume": QlibFieldRole.FEATURE},
        max_window=20,
        max_ref_periods=20,
    )
    outputs = {}
    for name, expression in EXPRESSIONS.items():
        program = certify_qlib_expression(expression, policy=policy)
        request = QlibOperatorExecutionRequest(
            program=program,
            snapshot_hash=H1,
            universe_hash=H2,
            output_symbols=("SH600000", "SZ000001"),
            rows=rows(program.required_fields),
        )
        result = execute_certified_qlib_operators(request)
        if not isinstance(result, QlibFactorScoreArtifact):
            raise AssertionError(f"{name} failed: {result.code.value}")
        repeated = execute_certified_qlib_operators(request)
        if not isinstance(repeated, QlibFactorScoreArtifact):
            raise AssertionError(f"{name} repeat failed: {repeated.code.value}")
        assert repeated.output_hash == result.output_hash and repeated.rows == result.rows
        outputs[name] = {
            "output_hash": result.output_hash,
            "rows": len(result.rows),
            "missing": result.missing_scores,
            "first_symbol_scores": [row.score for row in result.rows if row.symbol == "SH600000"],
        }

    ref = outputs["Ref"]["first_symbol_scores"]
    assert ref[:2] == [None, None] and ref[2:5] == [1.0, 2.0, 3.0]
    mean = outputs["Mean"]["first_symbol_scores"]
    assert mean[:4] == [1.0, 1.5, 2.0, 3.0]
    std = outputs["Std"]["first_symbol_scores"]
    assert std[0] is None and math.isclose(std[1], math.sqrt(0.5), rel_tol=1e-12)
    corr = outputs["Corr"]["first_symbol_scores"]
    assert corr[0] is None and all(math.isclose(value, 1.0, rel_tol=1e-12) for value in corr[1:] if value is not None)
    rank = outputs["Rank"]["first_symbol_scores"]
    assert rank == [1.0] * 8
    conditional = outputs["If"]["first_symbol_scores"]
    assert conditional[0] == 0.0 and conditional[1:] == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

    print(
        json.dumps(
            {
                "status": "PASSED",
                "runtime": "pyqlib-0.9.7-builtins",
                "operators": list(EXPRESSIONS),
                "artifacts": outputs,
                "native_string_resolver_used": False,
                "custom_operator_registered": False,
                "network_used": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
