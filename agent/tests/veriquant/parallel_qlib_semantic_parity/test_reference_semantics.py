from __future__ import annotations

import math

import pytest

from src.veriquant.qlib_semantic_parity.api import (
    DIFFERENCE_CODES,
    EXPECTED_DISPOSITIONS,
    OPERATOR_POLICIES,
    PARITY_OPERATORS,
    SEMANTIC_POLICY_HASH,
    ParityObservation,
    QlibSemanticParityReport,
    SemanticParityStatus,
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


def test_reference_semantics_cover_ties_missing_warmup_origin_and_weights() -> None:
    values = (2.0, 5.0, 5.0, None, 4.0)
    assert rolling_extreme(values, 3, maximum=True) == (2.0, 5.0, 5.0, 5.0, 5.0)
    assert rolling_extreme(values, 3, maximum=False) == (2.0, 2.0, 2.0, 5.0, 4.0)
    assert rolling_extreme_index(values, 3, maximum=True) == (1.0, 2.0, 2.0, 1.0, 1.0)
    assert rolling_extreme_index(values, 3, maximum=False) == (1.0, 1.0, 1.0, 1.0, 3.0)
    assert rolling_rank((1.0, 2.0, 2.0, None), 3) == (1.0, 1.0, 5.0 / 6.0, None)
    assert rolling_wma((1.0, 2.0, 3.0), 3) == (1.0, 5.0 / 3.0, 14.0 / 6.0)
    assert rolling_wma((1.0, None, 3.0), 3)[-1] == 2.5


def test_pair_semantics_define_ddof_and_pairwise_missing() -> None:
    covariance = rolling_cov((1.0, 2.0, 3.0), (2.0, 4.0, 6.0), 3)
    assert covariance[0] is None and covariance[1:] == (1.0, 2.0)
    correlation = rolling_corr((1.0, 2.0, None, 4.0), (2.0, 4.0, 6.0, 8.0), 4)
    assert correlation[0] is None
    assert all(math.isclose(value, 1.0) for value in correlation[1:] if value is not None)
    assert rolling_corr((1.0, 1.0 + 1e-7), (2.0, 3.0), 2)[1] is None


def test_quantile_rsquare_and_conditional_edge_semantics() -> None:
    assert rolling_quantile((1.0, 3.0, 2.0, None), 3, 0.5) == (1.0, 2.0, 2.0, 2.5)
    rsquared = rolling_rsquare((1.0, 2.0, 3.0, None, 5.0), 3)
    assert rsquared[0] is None
    assert all(math.isclose(value, 1.0) for value in rsquared[1:] if value is not None)
    assert conditional((1.0, 0.0, None), (10.0, 11.0, 12.0), (20.0, 21.0, 22.0)) == (
        10.0,
        21.0,
        None,
    )


def test_reference_rejects_invalid_windows_quantiles_and_shapes() -> None:
    with pytest.raises(ValueError):
        rolling_rank((1.0,), 0)
    with pytest.raises(ValueError):
        rolling_quantile((1.0,), 2, 1.1)
    with pytest.raises(ValueError):
        rolling_cov((1.0,), (1.0, 2.0), 2)
    with pytest.raises(ValueError):
        conditional((1.0,), (1.0,), (1.0, 2.0))


def test_policy_is_complete_stable_and_conservative() -> None:
    assert tuple(sorted(OPERATOR_POLICIES)) == tuple(sorted(PARITY_OPERATORS))
    assert tuple(sorted(EXPECTED_DISPOSITIONS)) == tuple(sorted(PARITY_OPERATORS))
    assert SEMANTIC_POLICY_HASH.startswith("sha256:") and len(SEMANTIC_POLICY_HASH) == 71
    assert EXPECTED_DISPOSITIONS["WMA"] is SemanticParityStatus.KNOWN_DIFFERENCE
    assert EXPECTED_DISPOSITIONS["If"] is SemanticParityStatus.KNOWN_DIFFERENCE
    assert set(DIFFERENCE_CODES) == {"IdxMax", "IdxMin", "If", "WMA"}


def test_matrix_fail_closed_and_report_hash_is_stable() -> None:
    exact = ParityObservation("core", (1.0, None), (1.0, None))
    evidence = [build_operator_evidence(name, (exact,)) for name in PARITY_OPERATORS if name not in DIFFERENCE_CODES]
    mismatch = ParityObservation("edge", (1.0, None), (1.0, 2.0))
    evidence.extend(build_operator_evidence(name, (mismatch,)) for name in DIFFERENCE_CODES)
    ordered = tuple(sorted(evidence, key=lambda item: item.operator))
    report = QlibSemanticParityReport(evidence=ordered, runtime_hash="sha256:" + "1" * 64)
    repeated = QlibSemanticParityReport(evidence=ordered, runtime_hash="sha256:" + "1" * 64)
    assert report.report_hash == repeated.report_hash
    with pytest.raises(ValueError):
        build_operator_evidence("Max", (mismatch,))
    with pytest.raises(ValueError):
        build_operator_evidence("WMA", (exact,))
