from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from veriquant.formula_validation_factory import (
    FACTORY_HASH,
    FormulaValidationFactory,
    ValidationEligibility,
    ValidationExecutionReceipt,
    ValidationFactoryError,
    ValidationFactoryFailureCode,
    ValidationLayer,
    ValidationScenario,
    ValidationStatus,
    apply_execution_receipts,
    canonical_hash,
    subject_from_projection,
)


H = lambda value: canonical_hash({"value": value})


@dataclass(frozen=True)
class _Identity:
    source_hash: str = H("source")
    program_hash: str = H("program")
    compiler_hash: str = H("compiler")
    operator_policy_hash: str = H("operators")
    semantic_adapter_policy_hash: str = H("adapters")
    postprocess_plan_hash: str = H("postprocess")


@dataclass(frozen=True)
class _Operator:
    operator: str


@dataclass(frozen=True)
class _Adapter:
    adapter: str


@dataclass(frozen=True)
class _Program:
    identity: _Identity = _Identity()
    required_fields: tuple[str, ...] = ("volume", "close")
    lookback: int = 19
    operator_policies: tuple[_Operator, ...] = (_Operator("Mean"), _Operator("Div"))
    semantic_adapter_policies: tuple[_Adapter, ...] = (_Adapter("SafeWMA"),)


def _projection_record(qualification: str = "EXECUTION_READY", *, missing=(), pit_missing=()):
    return SimpleNamespace(
        factor_id="alpha101_001",
        source_hash=H("projection-source"),
        factor_ir_hash=H("factor-ir"),
        program_hash=H("legacy-program"),
        runtime_hash=H("runtime"),
        factor_ir_policy_hash=H("factor-ir-policy"),
        expression_policy_hash=H("expression-policy"),
        adapter_hash=H("adapter"),
        postprocess_steps=("zscore_cross_section",),
        execution_qualification=SimpleNamespace(value=qualification),
        required_fields=("volume", "close"),
        maximum_lookback=19,
        operators=("Mean", "Div"),
        semantic_adapters=("SafeWMA",),
        missing_fields=tuple(missing),
        pit_missing_fields=tuple(pit_missing),
        canonical_expression="Mean($close,20)/Mean($volume,20)",
        record_hash=H("record"),
    )


def test_catalog_is_exactly_thirteen_scenarios_and_eight_layers() -> None:
    assert len(ValidationScenario) == 13
    assert len(ValidationLayer) == 8
    assert set(item.value for item in ValidationScenario) == {
        "NORMAL_VALUES", "PARTIAL_WARMUP", "NAN_VALUES", "CONSECUTIVE_MISSING", "ALL_MISSING",
        "TIES", "EXTREME_VALUES", "DIVISION_BY_ZERO", "UNIVERSE_ENTRY_EXIT", "DATA_REVISION",
        "FUTURE_PERTURBATION", "DIFFERENT_UNIVERSE", "DETERMINISTIC_REPLAY",
    }


def test_factor_ir_generates_complete_matrix_without_claiming_execution() -> None:
    matrix = FormulaValidationFactory().from_factor_ir(_Program(), factor_id="factor_ir_demo")
    assert len(matrix.cells) == 104
    assert matrix.status_counts == {"FAILED": 0, "NOT_RUN": 104, "PASSED": 0, "UNAVAILABLE": 0}
    assert matrix.eligibility_counts == {"BLOCKED": 65, "ELIGIBLE": 39}
    assert matrix.factory_hash == FACTORY_HASH
    assert matrix.subject.required_fields == ("close", "volume")


def test_execution_ready_projection_is_eligible_but_all_cells_remain_not_run() -> None:
    matrix = FormulaValidationFactory().from_projection(_projection_record(), projection_hash=H("projection"))
    assert matrix.eligibility_counts == {"BLOCKED": 0, "ELIGIBLE": 104}
    assert matrix.status_counts[ValidationStatus.NOT_RUN.value] == 104
    assert matrix.status_counts[ValidationStatus.PASSED.value] == 0
    assert matrix.subject.program_hash == H("factor-ir")


def test_static_qualification_never_inherits_projection_parity_as_passed() -> None:
    matrix = FormulaValidationFactory().from_projection(_projection_record(), projection_hash=H("projection"))
    pyqlib_cells = [cell for cell in matrix.cells if cell.layer is ValidationLayer.PINNED_PYQLIB_097_COMPARISON]
    assert len(pyqlib_cells) == 13
    assert all(cell.eligibility is ValidationEligibility.ELIGIBLE for cell in pyqlib_cells)
    assert all(cell.status is ValidationStatus.NOT_RUN for cell in pyqlib_cells)
    assert all(cell.evidence_hash is None for cell in pyqlib_cells)


@pytest.mark.parametrize("qualification", ["SEMANTICS_REVIEW_REQUIRED", "DATA_FIELD_MISSING", "PIT_METADATA_MISSING", "AUXILIARY_ONLY"])
def test_non_ready_projection_blocks_runtime_authority_layers(qualification: str) -> None:
    missing = ("amount",) if qualification == "DATA_FIELD_MISSING" else ()
    pit_missing = ("fundamental",) if qualification == "PIT_METADATA_MISSING" else ()
    matrix = FormulaValidationFactory().from_projection(
        _projection_record(qualification, missing=missing, pit_missing=pit_missing),
        projection_hash=H("projection"),
    )
    for cell in matrix.cells:
        if cell.layer in {
            ValidationLayer.FROZEN_PIT_EXECUTION,
            ValidationLayer.FACTOR_SCORE_ARTIFACT,
            ValidationLayer.TRIAL_LEDGER,
            ValidationLayer.OFFICIAL_EVIDENCE_REPLAY,
        }:
            assert cell.eligibility is ValidationEligibility.BLOCKED
            assert "PROJECTION_EXECUTION_QUALIFICATION_REQUIRED" in cell.blocker_codes
            assert cell.status is ValidationStatus.NOT_RUN


def test_matrix_identity_is_stable_for_reordered_subject_metadata() -> None:
    factory = FormulaValidationFactory()
    left = factory.from_factor_ir(_Program(), factor_id="stable_factor")
    right = factory.from_factor_ir(
        _Program(required_fields=("close", "volume"), operator_policies=(_Operator("Div"), _Operator("Mean"))),
        factor_id="stable_factor",
    )
    assert left.subject.subject_hash == right.subject.subject_hash
    assert left.matrix_hash == right.matrix_hash


def test_real_receipt_updates_only_one_cell_and_preserves_audit_identity() -> None:
    matrix = FormulaValidationFactory().from_projection(_projection_record(), projection_hash=H("projection"))
    target = matrix.cells[0]
    updated = apply_execution_receipts(matrix, (
        ValidationExecutionReceipt(
            cell_id=target.cell_id,
            subject_hash=matrix.subject.subject_hash,
            status=ValidationStatus.PASSED,
            evidence_hash=H("measured-evidence"),
            command_hash=H("registered-command"),
            runtime_hash=H("runtime-0.9.7"),
            test_count=1,
        ),
    ))
    changed = updated.cells[0]
    assert changed.status is ValidationStatus.PASSED
    assert changed.test_count == 1
    assert updated.status_counts[ValidationStatus.PASSED.value] == 1
    assert updated.status_counts[ValidationStatus.NOT_RUN.value] == 103
    assert matrix.status_counts[ValidationStatus.PASSED.value] == 0


def test_unexecuted_or_evidence_free_pass_is_rejected() -> None:
    with pytest.raises(ValidationFactoryError) as not_run:
        ValidationExecutionReceipt(
            cell_id="x", subject_hash=H("subject"), status=ValidationStatus.PASSED,
            evidence_hash=H("evidence"), command_hash=H("command"), test_count=1, executed=False,
        )
    assert not_run.value.code is ValidationFactoryFailureCode.UNEXECUTED_OUTCOME
    with pytest.raises(ValidationFactoryError) as no_tests:
        ValidationExecutionReceipt(
            cell_id="x", subject_hash=H("subject"), status=ValidationStatus.PASSED,
            evidence_hash=H("evidence"), command_hash=H("command"), test_count=0,
        )
    assert no_tests.value.code is ValidationFactoryFailureCode.INCOMPLETE_EVIDENCE


def test_blocked_cell_cannot_receive_an_outcome() -> None:
    matrix = FormulaValidationFactory().from_factor_ir(_Program(), factor_id="static_only")
    target = next(cell for cell in matrix.cells if cell.eligibility is ValidationEligibility.BLOCKED)
    receipt = ValidationExecutionReceipt(
        cell_id=target.cell_id, subject_hash=matrix.subject.subject_hash, status=ValidationStatus.FAILED,
        evidence_hash=H("failure"), command_hash=H("command"), test_count=1,
    )
    with pytest.raises(ValidationFactoryError) as caught:
        apply_execution_receipts(matrix, (receipt,))
    assert caught.value.code is ValidationFactoryFailureCode.BLOCKED_CELL


def test_receipt_identity_and_duplicates_fail_closed() -> None:
    matrix = FormulaValidationFactory().from_projection(_projection_record(), projection_hash=H("projection"))
    target = matrix.cells[0]
    wrong = ValidationExecutionReceipt(
        cell_id=target.cell_id, subject_hash=H("wrong"), status=ValidationStatus.FAILED,
        evidence_hash=H("failure"), command_hash=H("command"), test_count=1,
    )
    with pytest.raises(ValidationFactoryError) as identity:
        apply_execution_receipts(matrix, (wrong,))
    assert identity.value.code is ValidationFactoryFailureCode.IDENTITY_MISMATCH
    valid = ValidationExecutionReceipt(
        cell_id=target.cell_id, subject_hash=matrix.subject.subject_hash, status=ValidationStatus.FAILED,
        evidence_hash=H("failure"), command_hash=H("command"), test_count=1,
    )
    with pytest.raises(ValidationFactoryError) as duplicate:
        apply_execution_receipts(matrix, (valid, valid))
    assert duplicate.value.code is ValidationFactoryFailureCode.DUPLICATE_OUTCOME


def test_future_revision_universe_and_replay_cells_have_explicit_assertions() -> None:
    matrix = FormulaValidationFactory().from_projection(_projection_record(), projection_hash=H("projection"))
    joined = {cell.scenario: " ".join(cell.assertions) for cell in matrix.cells if cell.layer is ValidationLayer.FROZEN_PIT_EXECUTION}
    assert "future rows" in joined[ValidationScenario.FUTURE_PERTURBATION]
    assert "decision time" in joined[ValidationScenario.DATA_REVISION]
    assert "universe identity" in joined[ValidationScenario.DIFFERENT_UNIVERSE]
    assert "exact artifact hash" in joined[ValidationScenario.DETERMINISTIC_REPLAY]


def test_projection_subject_binds_record_and_projection_hashes() -> None:
    subject = subject_from_projection(_projection_record(), projection_hash=H("projection"))
    assert subject.projection_hash == H("projection")
    assert subject.projection_record_hash == H("record")
    assert subject.execution_qualification == "EXECUTION_READY"

