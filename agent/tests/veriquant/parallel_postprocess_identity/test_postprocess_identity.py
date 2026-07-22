from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.evidence import ExperimentIdentity, TrialLedger
from veriquant.postprocess_identity import (
    ActualScore,
    CrossSectionalPostprocessPlan,
    INDUSTRY_NEUTRALIZATION_VERSION,
    NeutralizationPolicy,
    PostprocessRuntimePolicy,
    RANK_POLICY_VERSION,
    SIZE_NEUTRALIZATION_VERSION,
    StaticPostprocessOperator,
    StaticPostprocessStep,
    WINSORIZE_POLICY_VERSION,
    ZSCORE_POLICY_VERSION,
    derive_program_identity,
    derive_score_artifact_identity,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
UTC = timezone.utc


def _plan() -> CrossSectionalPostprocessPlan:
    return CrossSectionalPostprocessPlan(
        plan_id="factor.postprocess.v1",
        steps=(
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.WINSORIZE,
                policy_version=WINSORIZE_POLICY_VERSION,
                lower=0.01,
                upper=0.99,
            ),
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.RANK,
                policy_version=RANK_POLICY_VERSION,
            ),
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.ZSCORE,
                policy_version=ZSCORE_POLICY_VERSION,
            ),
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.INDUSTRY_NEUTRALIZE,
                policy_version=INDUSTRY_NEUTRALIZATION_VERSION,
            ),
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.SIZE_NEUTRALIZE,
                policy_version=SIZE_NEUTRALIZATION_VERSION,
            ),
        ),
    )


def _program():
    return derive_program_identity(
        time_series_formula_ir_hash=HASH_A,
        postprocess_plan=_plan(),
        neutralization_policy=NeutralizationPolicy(policy_id="neutralization.v1"),
        runtime_policy=PostprocessRuntimePolicy(
            policy_id="postprocess.runtime.v1",
            runtime_version="veriquant-static-postprocess/v1",
        ),
        compiler_version="factor-ir-v2/compiler-v1",
    )


def _scores(last: float = 2.0) -> tuple[ActualScore, ...]:
    now = datetime(2025, 1, 2, 15, tzinfo=UTC)
    return (
        ActualScore(event_time=now, symbol="000001.SZ", score=1.0),
        ActualScore(event_time=now, symbol="000002.SZ", score=last),
    )


def test_program_identity_is_static_and_score_free() -> None:
    first = _program()
    second = _program()
    assert first == second
    assert first.program_hash == second.program_hash
    assert first.experiment_factor_hash == first.program_hash
    parameters = inspect.signature(derive_program_identity).parameters
    assert "actual_scores" not in parameters
    assert "score_artifact" not in parameters
    assert "snapshot_hash" not in parameters
    assert "universe_hash" not in parameters


def test_all_postprocess_and_neutralization_semantics_are_explicitly_versioned() -> None:
    program = _program()
    assert program.cross_sectional_postprocess_plan_hash == _plan().artifact_hash
    assert {step.policy_version for step in _plan().steps} == {
        WINSORIZE_POLICY_VERSION,
        RANK_POLICY_VERSION,
        ZSCORE_POLICY_VERSION,
        INDUSTRY_NEUTRALIZATION_VERSION,
        SIZE_NEUTRALIZATION_VERSION,
    }
    with pytest.raises(ContractValidationError) as exc:
        StaticPostprocessStep(
            operator=StaticPostprocessOperator.RANK,
            policy_version="rank/latest",
        )
    assert exc.value.code is FailureCode.INVALID_VALUE


def test_program_identity_can_register_and_reserve_before_score_execution(tmp_path) -> None:
    program = _program()
    identity = ExperimentIdentity(
        experiment_id="experiment.static-postprocess.v1",
        dataset_hash=HASH_B,
        universe_hash=HASH_C,
        factor_hash=program.experiment_factor_hash,
        split_hash=HASH_D,
        label_hash=HASH_A,
        cost_hash=HASH_B,
        code_hash=HASH_C,
        availability_hash=HASH_D,
        seed=7,
    )
    ledger = TrialLedger(tmp_path / "trial-ledger.sqlite3")
    ledger.create_family("family.static-postprocess", 1)
    identity_hash = ledger.register_identity(identity)
    reservation = ledger.reserve_attempt(
        family_id="family.static-postprocess",
        trial_id="trial.static-postprocess.001",
        identity_hash=identity_hash,
        proposer_id="researcher.static",
        units=1,
    )
    assert reservation.identity_hash == identity.identity_hash
    assert ledger.replay().families["family.static-postprocess"].consumed == 1


def test_runtime_scores_change_only_output_identity_not_program_or_experiment_identity() -> None:
    program = _program()
    experiment = ExperimentIdentity(
        experiment_id="experiment.output-isolation.v1",
        dataset_hash=HASH_B,
        universe_hash=HASH_C,
        factor_hash=program.program_hash,
        split_hash=HASH_D,
        label_hash=HASH_A,
        cost_hash=HASH_B,
        code_hash=HASH_C,
        availability_hash=HASH_D,
        seed=7,
    )
    before = experiment.identity_hash
    first = derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_B,
        universe_hash=HASH_C,
        actual_scores=_scores(2.0),
    )
    second = derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_B,
        universe_hash=HASH_C,
        actual_scores=_scores(9.0),
    )
    assert first.score_artifact_hash != second.score_artifact_hash
    assert first.program_hash == second.program_hash == program.program_hash
    assert experiment.identity_hash == before
    assert experiment.factor_hash == program.program_hash


def test_score_identity_binds_snapshot_universe_and_actual_scores() -> None:
    program = _program()
    identity = derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_B,
        universe_hash=HASH_C,
        actual_scores=_scores(),
    )
    assert identity == derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_B,
        universe_hash=HASH_C,
        actual_scores=_scores(),
    )
    changed_snapshot = derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_D,
        universe_hash=HASH_C,
        actual_scores=_scores(),
    )
    assert changed_snapshot.score_artifact_hash != identity.score_artifact_hash


def test_tampered_derived_hashes_fail_closed() -> None:
    program = _program()
    with pytest.raises(ContractValidationError) as program_error:
        replace(program, program_hash=HASH_D)
    assert program_error.value.code is FailureCode.INVALID_HASH
    score = derive_score_artifact_identity(
        program_identity=program,
        frozen_snapshot_hash=HASH_B,
        universe_hash=HASH_C,
        actual_scores=_scores(),
    )
    with pytest.raises(ContractValidationError) as score_error:
        replace(score, score_artifact_hash=HASH_D)
    assert score_error.value.code is FailureCode.INVALID_HASH


@pytest.mark.parametrize("field", ("sealed_metric", "official_label", "forward_return_5d"))
def test_label_and_sealed_fields_cannot_enter_static_identity(field: str) -> None:
    with pytest.raises(ContractValidationError) as exc:
        NeutralizationPolicy(policy_id="bad.policy", industry_field=field)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_plan_policy_field_disagreement_and_invalid_scores_fail_closed() -> None:
    plan = CrossSectionalPostprocessPlan(
        plan_id="factor.postprocess.bad-field",
        steps=(
            StaticPostprocessStep(
                operator=StaticPostprocessOperator.INDUSTRY_NEUTRALIZE,
                policy_version=INDUSTRY_NEUTRALIZATION_VERSION,
                field="sector",
            ),
        ),
    )
    with pytest.raises(ContractValidationError) as mismatch:
        derive_program_identity(
            time_series_formula_ir_hash=HASH_A,
            postprocess_plan=plan,
            neutralization_policy=NeutralizationPolicy(policy_id="neutralization.v1"),
            runtime_policy=PostprocessRuntimePolicy(
                policy_id="postprocess.runtime.v1",
                runtime_version="veriquant-static-postprocess/v1",
            ),
            compiler_version="factor-ir-v2/compiler-v1",
        )
    assert mismatch.value.code is FailureCode.INVALID_VALUE
    with pytest.raises(ContractValidationError) as nonfinite:
        ActualScore(
            event_time=datetime(2025, 1, 2, 15, tzinfo=UTC),
            symbol="000001.SZ",
            score=float("nan"),
        )
    assert nonfinite.value.code is FailureCode.NON_FINITE_NUMBER


def test_actual_scores_require_stable_unique_order() -> None:
    scores = _scores()
    with pytest.raises(ContractValidationError) as unordered:
        derive_score_artifact_identity(
            program_identity=_program(),
            frozen_snapshot_hash=HASH_B,
            universe_hash=HASH_C,
            actual_scores=tuple(reversed(scores)),
        )
    assert unordered.value.code is FailureCode.INVALID_VALUE
    with pytest.raises(ContractValidationError) as duplicate:
        derive_score_artifact_identity(
            program_identity=_program(),
            frozen_snapshot_hash=HASH_B,
            universe_hash=HASH_C,
            actual_scores=(scores[0], scores[0]),
        )
    assert duplicate.value.code is FailureCode.INVALID_VALUE
