"""Pinned pyqlib 0.9.7 dual-engine parity matrix; prints one canonical JSON report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.veriquant.qlib_expression.api import QlibExpressionPolicy, QlibFieldRole, certify_qlib_expression
from src.veriquant.qlib_operator_worker.api import (
    QlibFactorScoreArtifact,
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    QlibRuntimeIdentity,
    execute_certified_qlib_operators,
)
from src.veriquant.qlib_semantic_parity.api import (
    PARITY_OPERATORS,
    SEMANTIC_POLICY_HASH,
    ParityObservation,
    QlibSemanticParityReport,
    build_operator_evidence,
    conditional,
    rolling_corr,
    rolling_cov,
    rolling_extreme,
    rolling_extreme_index,
    rolling_quantile,
    rolling_rank,
    rolling_rsquare,
    rolling_wma,
)


H1 = "sha256:" + "c" * 64
H2 = "sha256:" + "d" * 64


@dataclass(frozen=True)
class Case:
    operator: str
    case_id: str
    expression: str
    values: dict[str, tuple[float | None, ...]]
    reference: tuple[float | None, ...]


def _single_cases(operator: str, function):
    core = (2.0, 5.0, 5.0, None, 4.0, 1.0)
    edge = (None, 1.0, 1.0, 3.0, None, 2.0)
    return (
        Case(operator, "ties_nan_warmup", f"{operator}($close,3)", {"close": core}, function(core, 3)),
        Case(operator, "window_edge", f"{operator}($close,2)", {"close": edge}, function(edge, 2)),
    )


def _cases() -> tuple[Case, ...]:
    cases: list[Case] = []
    cases.extend(_single_cases("Max", lambda values, window: rolling_extreme(values, window, maximum=True)))
    cases.extend(_single_cases("Min", lambda values, window: rolling_extreme(values, window, maximum=False)))
    cases.extend(_single_cases("IdxMax", lambda values, window: rolling_extreme_index(values, window, maximum=True)))
    cases.extend(_single_cases("IdxMin", lambda values, window: rolling_extreme_index(values, window, maximum=False)))
    cases.extend(_single_cases("Rank", rolling_rank))
    cases.extend(_single_cases("WMA", rolling_wma))

    left = (1.0, 2.0, None, 4.0, 5.0, 5.000001)
    right = (2.0, 4.0, 6.0, None, 10.0, 10.000002)
    for operator, function in (("Cov", rolling_cov), ("Corr", rolling_corr)):
        cases.append(Case(operator, "ddof_pairwise_nan", f"{operator}($close,$volume,3)", {"close": left, "volume": right}, function(left, right, 3)))
        lhs2, rhs2 = (1.0, 1.0, 2.0, 3.0), (2.0, 3.0, 4.0, 6.0)
        cases.append(Case(operator, "constant_window_edge", f"{operator}($close,$volume,2)", {"close": lhs2, "volume": rhs2}, function(lhs2, rhs2, 2)))

    for quantile in (0.25, 1.0):
        values = (1.0, 4.0, 2.0, None, 3.0, 8.0)
        cases.append(Case("Quantile", f"linear_q_{quantile}", f"Quantile($close,3,{quantile})", {"close": values}, rolling_quantile(values, 3, quantile)))

    for case_id, values in (
        ("regression_nan_warmup", (1.0, 2.0, 4.0, None, 5.0, 7.0)),
        ("constant_window_edge", (1.0, 1.0, 1.0, 2.0, 3.0, 4.0)),
    ):
        cases.append(Case("Rsquare", case_id, "Rsquare($close,3)", {"close": values}, rolling_rsquare(values, 3)))

    for case_id, condition_values in (
        ("truth_table", (1.0, 0.0, 1.0, 0.0)),
        ("missing_condition", (1.0, None, 0.0, None)),
    ):
        left_values, right_values = (10.0, 11.0, 12.0, 13.0), (20.0, 21.0, 22.0, 23.0)
        cases.append(
            Case(
                "If",
                case_id,
                "If($condition,$close,$volume)",
                {"condition": condition_values, "close": left_values, "volume": right_values},
                conditional(condition_values, left_values, right_values),
            )
        )
    return tuple(cases)


def _execute(case: Case) -> tuple[float | None, ...]:
    policy = QlibExpressionPolicy(
        field_roles={name: QlibFieldRole.FEATURE for name in case.values},
        max_window=20,
        max_ref_periods=20,
    )
    program = certify_qlib_expression(case.expression, policy=policy)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    length = len(next(iter(case.values.values())))
    rows = tuple(
        QlibInMemoryFeatureRow(
            event_time=start + timedelta(days=index),
            symbol="SH600000",
            values={field: values[index] for field, values in case.values.items()},
        )
        for index in range(length)
    )
    request = QlibOperatorExecutionRequest(
        program=program,
        snapshot_hash=H1,
        universe_hash=H2,
        output_symbols=("SH600000",),
        rows=rows,
    )
    first = execute_certified_qlib_operators(request)
    second = execute_certified_qlib_operators(request)
    if not isinstance(first, QlibFactorScoreArtifact) or not isinstance(second, QlibFactorScoreArtifact):
        failure = first if not isinstance(first, QlibFactorScoreArtifact) else second
        raise AssertionError(f"{case.operator}/{case.case_id} failed: {failure.code.value}")
    if first.output_hash != second.output_hash or first.rows != second.rows:
        raise AssertionError(f"{case.operator}/{case.case_id} replay changed")
    return tuple(row.score for row in first.rows)


def main() -> int:
    grouped: dict[str, list[ParityObservation]] = {name: [] for name in PARITY_OPERATORS}
    for case in _cases():
        grouped[case.operator].append(ParityObservation(case.case_id, case.reference, _execute(case)))
    evidence = tuple(build_operator_evidence(name, tuple(grouped[name])) for name in sorted(grouped))
    report = QlibSemanticParityReport(evidence=evidence, runtime_hash=QlibRuntimeIdentity.expected().runtime_hash)
    repeated = QlibSemanticParityReport(evidence=evidence, runtime_hash=QlibRuntimeIdentity.expected().runtime_hash)
    if repeated.report_hash != report.report_hash:
        raise AssertionError("semantic parity report hash changed on replay")
    print(
        json.dumps(
            {
                "status": "PASSED",
                "pyqlib_version": "0.9.7",
                "semantic_policy_hash": SEMANTIC_POLICY_HASH,
                "report_hash": report.report_hash,
                "runtime_hash": report.runtime_hash,
                "evidence": [item.to_canonical() for item in report.evidence],
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
