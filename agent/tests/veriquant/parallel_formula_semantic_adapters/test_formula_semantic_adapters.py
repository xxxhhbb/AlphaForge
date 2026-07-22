from __future__ import annotations

import math

import pytest

from src.veriquant.formula_semantic_adapters import (
    ADAPTER_POLICY_HASH,
    BUILTIN_ADAPTER_POLICIES,
    AdapterFailureCode,
    SemanticAdapterError,
    apply_builtin_adapter,
    safe_idx_max,
    safe_idx_min,
    safe_if,
    safe_wma,
)
from src.veriquant.qlib_semantic_parity.reference import (
    conditional,
    rolling_extreme_index,
    rolling_wma,
)


def test_safe_if_normal_missing_nan_and_selected_missing() -> None:
    result = safe_if((1.0, 0.0, None, math.nan), (10.0, 11.0, 12.0, 13.0), (20.0, None, 22.0, 23.0))
    assert result.values == (10.0, None, None, None)


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        (safe_idx_max, (1.0, 2.0, 2.0, 1.0, 1.0)),
        (safe_idx_min, (1.0, 1.0, 1.0, 1.0, 3.0)),
    ],
)
def test_safe_index_extrema_warmup_missing_and_first_tie(adapter, expected) -> None:
    assert adapter((2.0, 5.0, 5.0, math.nan, 4.0), 3).values == expected


@pytest.mark.parametrize("adapter", [safe_idx_max, safe_idx_min])
def test_safe_index_extrema_all_missing(adapter) -> None:
    assert adapter((None, math.nan, None), 2).values == (None, None, None)


def test_safe_wma_oldest_to_newest_warmup_and_renormalization() -> None:
    result = safe_wma((1.0, math.nan, 3.0, 5.0), 3)
    assert result.values[:3] == (1.0, 1.0, 2.5)
    assert math.isclose(result.values[3], 4.2)


def test_safe_wma_all_missing_window() -> None:
    assert safe_wma((None, math.nan, None), 3).values == (None, None, None)


def test_adapters_match_current_veriquant_reference_policy() -> None:
    values = (2.0, 5.0, 5.0, None, 4.0)
    assert safe_idx_max(values, 3).values == rolling_extreme_index(values, 3, maximum=True)
    assert safe_idx_min(values, 3).values == rolling_extreme_index(values, 3, maximum=False)
    assert safe_wma(values, 3).values == rolling_wma(values, 3)
    assert safe_if((1.0, 0.0, None), values[:3], values[2:5]).values == conditional(
        (1.0, 0.0, None), values[:3], values[2:5]
    )


@pytest.mark.parametrize(
    ("adapter", "series", "window"),
    [
        (safe_idx_max, (3.0, 1.0, 2.0, 9.0), 3),
        (safe_idx_min, (3.0, 1.0, 2.0, -9.0), 3),
        (safe_wma, (3.0, 1.0, 2.0, 90.0), 3),
    ],
)
def test_future_perturbation_does_not_change_prior_outputs(adapter, series, window) -> None:
    baseline = adapter(series, window).values
    perturbed = adapter((*series[:-1], series[-1] * 100.0), window).values
    assert baseline[:-1] == perturbed[:-1]


def test_safe_if_future_perturbation_does_not_change_prior_outputs() -> None:
    baseline = safe_if((1.0, 0.0, 1.0), (2.0, 3.0, 4.0), (5.0, 6.0, 7.0)).values
    changed = safe_if((1.0, 0.0, 0.0), (2.0, 3.0, 400.0), (5.0, 6.0, 700.0)).values
    assert baseline[:2] == changed[:2]


def test_replay_and_result_identity_are_stable() -> None:
    first = safe_wma((1.0, None, 3.0), 3)
    second = safe_wma((1.0, math.nan, 3.0), 3)
    assert first == second
    assert first.result_hash == second.result_hash


def test_canonical_catalog_policy_and_per_adapter_hashes_are_frozen() -> None:
    assert ADAPTER_POLICY_HASH == "sha256:0eae4ba7f8693944ac410e53e2e5c0d1d26f0061dbdecfdb7e69fc3cc786d319"
    assert {name: policy.policy_hash for name, policy in BUILTIN_ADAPTER_POLICIES.items()} == {
        "SafeIf": "sha256:b5b7fe3b9778800ff91432ecb0f6ff1a892042b8ad6913d1440145777f062630",
        "SafeIdxMax": "sha256:31cc61fbf7e16d16917690575096cf2f1f21604b8318c5000895f982068d8f6e",
        "SafeIdxMin": "sha256:7f88c08fa20739805d85a2b72235d9d9c9da5bd56be9a882b40d649d9fb1ff1f",
        "SafeWMA": "sha256:b317f367f74f9cb17605b7e60b41c9977d82a20649e3e5136cbee0fcb7cb8748",
    }
    assert all(policy.adapter_hash.startswith("sha256:") for policy in BUILTIN_ADAPTER_POLICIES.values())


def test_known_pyqlib_differences_are_never_native_equivalence_claims() -> None:
    assert set(BUILTIN_ADAPTER_POLICIES) == {"SafeIf", "SafeIdxMax", "SafeIdxMin", "SafeWMA"}
    for policy in BUILTIN_ADAPTER_POLICIES.values():
        assert policy.semantics["native_pyqlib_0_9_7"] == "KNOWN_DIFFERENCE_DO_NOT_SUBSTITUTE"
        assert "CERTIFIED_EQUIVALENT" not in policy.semantics.values()


@pytest.mark.parametrize("window", [0, -1, 1.5, True, None])
def test_invalid_window_fails_closed(window) -> None:
    with pytest.raises(SemanticAdapterError) as caught:
        safe_wma((1.0,), window)
    assert caught.value.code is AdapterFailureCode.INVALID_WINDOW


@pytest.mark.parametrize("values", [(), (True,), ("bad",), (math.inf,)])
def test_invalid_values_fail_closed(values) -> None:
    with pytest.raises(SemanticAdapterError) as caught:
        safe_idx_max(values, 2)
    assert caught.value.code is AdapterFailureCode.INVALID_INPUT


def test_shape_arity_unknown_adapter_and_window_policy_fail_closed() -> None:
    with pytest.raises(SemanticAdapterError) as caught:
        safe_if((1.0,), (2.0,), (3.0, 4.0))
    assert caught.value.code is AdapterFailureCode.SHAPE_MISMATCH
    with pytest.raises(SemanticAdapterError) as caught:
        apply_builtin_adapter("Unknown", (1.0,))
    assert caught.value.code is AdapterFailureCode.UNKNOWN_ADAPTER
    with pytest.raises(SemanticAdapterError) as caught:
        apply_builtin_adapter("SafeIf", (1.0,), (2.0,), (3.0,), window=2)
    assert caught.value.code is AdapterFailureCode.INVALID_WINDOW


def test_closed_catalog_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        BUILTIN_ADAPTER_POLICIES["Unsafe"] = BUILTIN_ADAPTER_POLICIES["SafeIf"]
