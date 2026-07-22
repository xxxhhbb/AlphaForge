from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.formula_execution import FactorScoreArtifact, FactorScorePoint
from veriquant.qlib_score_postprocess import (
    POSTPROCESS_RUNTIME_HASH,
    ScorePostprocessOperator,
    ScorePostprocessPolicy,
    ScorePostprocessProgram,
    ScorePostprocessService,
    ScorePostprocessStep,
)


UTC = timezone.utc
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _snapshot(
    *,
    days: int = 2,
    symbols: tuple[str, ...] = ("000001.SZ", "000002.SZ", "600000.SH"),
    include_future_entrant: bool = False,
    constant_cap: bool = False,
) -> MarketSnapshot:
    start = datetime(2025, 1, 2, 15, tzinfo=UTC)
    fields = ("industry", "market_cap")
    rows: list[SnapshotRow] = []
    universe: dict[str, tuple[str, ...]] = {}
    for day in range(days):
        event_time = start + timedelta(days=day)
        members = symbols
        if include_future_entrant and day == 0:
            members = symbols[:-1]
        universe[event_time.isoformat()] = members
        for index, symbol in enumerate(members):
            rows.append(
                SnapshotRow(
                    event_time=event_time,
                    symbol=symbol,
                    values={
                        "industry": "bank" if index < 2 else "industry",
                        "market_cap": 100.0 if constant_cap else 100.0 * (index + 1 + day),
                    },
                )
            )
    return MarketSnapshot(
        dataset_id="postprocess.fixture",
        decision_time=start + timedelta(days=days - 1),
        start_time=start,
        fields=fields,
        rows=tuple(rows),
        historical_universe=universe,
        coverage_by_field={"industry": 1.0, "market_cap": 1.0},
    )


def _scores(snapshot: MarketSnapshot, values: tuple[float | None, ...] | None = None) -> FactorScoreArtifact:
    score_values = values or tuple(float(index + 1) for index in range(len(snapshot.rows)))
    assert len(score_values) == len(snapshot.rows)
    return FactorScoreArtifact(
        program_hash=HASH_A,
        snapshot_hash=snapshot.artifact_hash,
        universe_hash=canonical_content_hash(snapshot.historical_universe),
        runtime_hash=HASH_B,
        policy_hash=HASH_C,
        seed=7,
        values=tuple(
            FactorScorePoint(event_time=row.event_time, symbol=row.symbol, score=value)
            for row, value in zip(snapshot.rows, score_values)
        ),
    )


def _policy(*fields: str, minimum: int = 3) -> ScorePostprocessPolicy:
    return ScorePostprocessPolicy(
        policy_id="postprocess.policy.v1",
        allowed_fields=tuple(sorted(fields)),
        min_market_cap_observations=minimum,
    )


def _program(*steps: ScorePostprocessStep) -> ScorePostprocessProgram:
    return ScorePostprocessProgram(program_id="postprocess.program.v1", steps=tuple(steps))


def test_stable_replay_and_derived_identity_bind_all_inputs() -> None:
    snapshot = _snapshot(days=1)
    upstream = _scores(snapshot)
    program = _program(
        ScorePostprocessStep(operator=ScorePostprocessOperator.WINSORIZE, lower=0.1, upper=0.9),
        ScorePostprocessStep(operator=ScorePostprocessOperator.RANK_CROSS_SECTION),
    )
    policy = _policy()
    service = ScorePostprocessService()

    first = service.apply(upstream=upstream, program=program, policy=policy, snapshot=snapshot)
    second = service.apply(upstream=upstream, program=program, policy=policy, snapshot=snapshot)
    identity = service.derive_identity(upstream=upstream, program=program, policy=policy)

    assert first.artifact_hash == second.artifact_hash
    assert first.program_hash == identity.derived_program_hash
    assert first.runtime_hash == POSTPROCESS_RUNTIME_HASH
    assert first.policy_hash == policy.artifact_hash
    assert identity.upstream_score_hash == upstream.artifact_hash
    assert identity.upstream_program_hash == upstream.program_hash
    changed_upstream = _scores(snapshot, (1.0, 2.0, 4.0))
    assert service.derive_identity(upstream=changed_upstream, program=program, policy=policy).derived_program_hash != first.program_hash
    changed_program = _program(ScorePostprocessStep(operator=ScorePostprocessOperator.ZSCORE_CROSS_SECTION))
    assert service.derive_identity(upstream=upstream, program=changed_program, policy=policy).derived_program_hash != first.program_hash


def test_future_perturbation_does_not_change_past_cross_sections() -> None:
    early = _snapshot(days=1, include_future_entrant=True)
    late = _snapshot(days=2, include_future_entrant=True)
    early_scores = _scores(early, (10.0, 20.0))
    late_scores = _scores(late, (10.0, 20.0, 999.0, 1.0, 2.0))
    program = _program(ScorePostprocessStep(operator=ScorePostprocessOperator.RANK_CROSS_SECTION))
    service = ScorePostprocessService()

    early_result = service.apply(upstream=early_scores, program=program, policy=_policy(), snapshot=early)
    late_result = service.apply(upstream=late_scores, program=program, policy=_policy(), snapshot=late)
    late_past = tuple(point.score for point in late_result.values if point.event_time == early.decision_time)

    assert tuple(point.score for point in early_result.values) == late_past == (0.0, 1.0)
    assert all(point.symbol != "600000.SH" for point in late_result.values if point.event_time == early.decision_time)


def test_historical_universe_mismatch_fails_closed() -> None:
    snapshot = _snapshot(days=1, include_future_entrant=True)
    extra = SnapshotRow(
        event_time=snapshot.decision_time,
        symbol="600000.SH",
        values={"industry": "industry", "market_cap": 300.0},
    )
    leaked = replace(snapshot, rows=snapshot.rows + (extra,))
    upstream = FactorScoreArtifact(
        program_hash=HASH_A,
        snapshot_hash=leaked.artifact_hash,
        universe_hash=canonical_content_hash(leaked.historical_universe),
        runtime_hash=HASH_B,
        policy_hash=HASH_C,
        seed=7,
        values=tuple(
            FactorScorePoint(event_time=row.event_time, symbol=row.symbol, score=float(index))
            for index, row in enumerate(leaked.rows)
        ),
    )
    with pytest.raises(ContractValidationError) as exc:
        ScorePostprocessService().apply(
            upstream=upstream,
            program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.RANK_CROSS_SECTION)),
            policy=_policy(),
            snapshot=leaked,
        )
    assert exc.value.code is FailureCode.INVALID_HASH


def test_group_and_industry_neutralization_use_only_pit_field() -> None:
    snapshot = _snapshot(days=1)
    upstream = _scores(snapshot, (1.0, 3.0, 10.0))
    service = ScorePostprocessService()
    for operator in (
        ScorePostprocessOperator.NEUTRALIZE_BY_GROUP,
        ScorePostprocessOperator.NEUTRALIZE_BY_INDUSTRY,
    ):
        step = (
            ScorePostprocessStep(operator=operator, field="industry")
            if operator is ScorePostprocessOperator.NEUTRALIZE_BY_GROUP
            else ScorePostprocessStep(operator=operator)
        )
        result = service.apply(
            upstream=upstream,
            program=_program(step),
            policy=_policy("industry"),
            snapshot=snapshot,
        )
        assert tuple(point.score for point in result.values) == (-1.0, 1.0, 0.0)


def test_market_cap_neutralization_is_deterministic_and_constant_exposure_is_missing() -> None:
    snapshot = _snapshot(days=1)
    upstream = _scores(snapshot, (1.0, 2.0, 4.0))
    program = _program(ScorePostprocessStep(operator=ScorePostprocessOperator.NEUTRALIZE_BY_MARKET_CAP))
    service = ScorePostprocessService()
    result = service.apply(
        upstream=upstream,
        program=program,
        policy=_policy("market_cap"),
        snapshot=snapshot,
    )
    assert all(point.score is not None for point in result.values)
    assert abs(sum(point.score for point in result.values if point.score is not None)) < 1e-12

    constant = _snapshot(days=1, constant_cap=True)
    constant_result = service.apply(
        upstream=_scores(constant),
        program=program,
        policy=_policy("market_cap"),
        snapshot=constant,
    )
    assert all(point.score is None for point in constant_result.values)


def test_constant_score_semantics_are_explicit() -> None:
    snapshot = _snapshot(days=1)
    upstream = _scores(snapshot, (5.0, 5.0, 5.0))
    service = ScorePostprocessService()
    ranked = service.apply(
        upstream=upstream,
        program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.RANK_CROSS_SECTION)),
        policy=_policy(),
        snapshot=snapshot,
    )
    zscored = service.apply(
        upstream=upstream,
        program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.ZSCORE_CROSS_SECTION)),
        policy=_policy(),
        snapshot=snapshot,
    )
    winsorized = service.apply(
        upstream=upstream,
        program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.WINSORIZE)),
        policy=_policy(),
        snapshot=snapshot,
    )
    assert tuple(point.score for point in ranked.values) == (0.5, 0.5, 0.5)
    assert all(point.score is None for point in zscored.values)
    assert tuple(point.score for point in winsorized.values) == (5.0, 5.0, 5.0)


@pytest.mark.parametrize("field", ("future_return_5d", "sealed_metric", "official_label"))
def test_label_and_sealed_fields_are_rejected(field: str) -> None:
    with pytest.raises(ContractValidationError) as exc:
        ScorePostprocessPolicy(policy_id="bad.policy", allowed_fields=(field,))
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_missing_and_unauthorized_fields_fail_closed() -> None:
    snapshot = _snapshot(days=1)
    upstream = _scores(snapshot)
    program = _program(
        ScorePostprocessStep(operator=ScorePostprocessOperator.NEUTRALIZE_BY_GROUP, field="sector")
    )
    with pytest.raises(ContractValidationError) as unauthorized:
        ScorePostprocessService().apply(
            upstream=upstream,
            program=program,
            policy=_policy(),
            snapshot=snapshot,
        )
    assert unauthorized.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(ContractValidationError) as missing:
        ScorePostprocessService().apply(
            upstream=upstream,
            program=program,
            policy=_policy("sector"),
            snapshot=snapshot,
        )
    assert missing.value.code is FailureCode.UNKNOWN_FIELD


def test_nan_market_cap_and_duplicate_keys_are_rejected() -> None:
    snapshot = _snapshot(days=1)
    bad_rows = list(snapshot.rows)
    bad_rows[0] = SnapshotRow(
        event_time=bad_rows[0].event_time,
        symbol=bad_rows[0].symbol,
        values={"industry": "bank", "market_cap": float("nan")},
    )
    bad_snapshot = replace(snapshot, rows=tuple(bad_rows))
    with pytest.raises(ContractValidationError) as nonfinite:
        ScorePostprocessService().apply(
            upstream=_scores(bad_snapshot),
            program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.NEUTRALIZE_BY_MARKET_CAP)),
            policy=_policy("market_cap"),
            snapshot=bad_snapshot,
        )
    assert nonfinite.value.code is FailureCode.NON_FINITE_NUMBER

    point = FactorScorePoint(event_time=snapshot.rows[0].event_time, symbol=snapshot.rows[0].symbol, score=1.0)
    with pytest.raises(ContractValidationError) as duplicate:
        FactorScoreArtifact(
            program_hash=HASH_A,
            snapshot_hash=snapshot.artifact_hash,
            universe_hash=canonical_content_hash(snapshot.historical_universe),
            runtime_hash=HASH_B,
            policy_hash=HASH_C,
            seed=7,
            values=(point, point),
        )
    assert duplicate.value.code is FailureCode.INVALID_VALUE


def test_invalid_program_parameters_and_snapshot_binding_fail_closed() -> None:
    with pytest.raises(ContractValidationError):
        ScorePostprocessStep(
            operator=ScorePostprocessOperator.WINSORIZE,
            lower=0.9,
            upper=0.1,
        )
    snapshot = _snapshot(days=1)
    other = replace(snapshot, dataset_id="postprocess.other")
    with pytest.raises(ContractValidationError) as mismatch:
        ScorePostprocessService().apply(
            upstream=_scores(snapshot),
            program=_program(ScorePostprocessStep(operator=ScorePostprocessOperator.RANK_CROSS_SECTION)),
            policy=_policy(),
            snapshot=other,
        )
    assert mismatch.value.code is FailureCode.INVALID_HASH
