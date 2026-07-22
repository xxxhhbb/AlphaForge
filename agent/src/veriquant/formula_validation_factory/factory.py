"""Generate and update validation matrices without running a formula."""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .models import (
    FACTORY_VERSION,
    FormulaValidationCell,
    FormulaValidationMatrix,
    FormulaValidationSubject,
    ValidationEligibility,
    ValidationExecutionReceipt,
    ValidationFactoryError,
    ValidationFactoryFailureCode,
    ValidationLayer,
    ValidationScenario,
    ValidationStatus,
    canonical_hash,
)


_SCENARIO_ASSERTIONS = {
    ValidationScenario.NORMAL_VALUES: ("finite expected values match the layer oracle",),
    ValidationScenario.PARTIAL_WARMUP: ("warmup boundary and first valid observation match declared lookback",),
    ValidationScenario.NAN_VALUES: ("isolated missing values follow bound operator and adapter policies",),
    ValidationScenario.CONSECUTIVE_MISSING: ("consecutive missing spans do not fabricate observations",),
    ValidationScenario.ALL_MISSING: ("all-missing input produces only policy-declared missing output",),
    ValidationScenario.TIES: ("tie behavior is deterministic and follows bound policy",),
    ValidationScenario.EXTREME_VALUES: ("extreme finite inputs do not create unrecorded overflow or clipping",),
    ValidationScenario.DIVISION_BY_ZERO: ("division-by-zero behavior is explicit and deterministic",),
    ValidationScenario.UNIVERSE_ENTRY_EXIT: ("historical membership controls entry and exit without survivorship fallback",),
    ValidationScenario.DATA_REVISION: ("only revisions available by decision time are visible",),
    ValidationScenario.FUTURE_PERTURBATION: ("perturbing future rows leaves the historical output prefix unchanged",),
    ValidationScenario.DIFFERENT_UNIVERSE: ("universe identity is bound and cross-sectional output is recomputed",),
    ValidationScenario.DETERMINISTIC_REPLAY: ("identical frozen inputs reproduce the exact artifact hash",),
}

_LAYER_ASSERTIONS = {
    ValidationLayer.AST_IR_CANONICAL_IDENTITY: ("source, compiler, policies, postprocess, and canonical program identities are bound",),
    ValidationLayer.OPERATOR_POLICY: ("every used operator and semantic adapter has an exact versioned policy identity",),
    ValidationLayer.WHOLE_FORMULA_REFERENCE_SEMANTICS: ("the composed formula matches an independent whole-formula reference",),
    ValidationLayer.PINNED_PYQLIB_097_COMPARISON: ("comparison binds pyqlib 0.9.7 and records equivalent or known-difference disposition",),
    ValidationLayer.FROZEN_PIT_EXECUTION: ("execution reads only frozen rows with availability at or before decision time",),
    ValidationLayer.FACTOR_SCORE_ARTIFACT: ("score artifact binds program, snapshot, universe, runtime, and actual scores",),
    ValidationLayer.TRIAL_LEDGER: ("a pre-execution family reservation and terminal attempt are append-only and replayable",),
    ValidationLayer.OFFICIAL_EVIDENCE_REPLAY: ("official visible Evidence is produced only by Evidence Service and replays identically",),
}

FACTORY_HASH = canonical_hash({
    "factory_version": FACTORY_VERSION,
    "scenarios": {item.value: list(_SCENARIO_ASSERTIONS[item]) for item in ValidationScenario},
    "layers": {item.value: list(_LAYER_ASSERTIONS[item]) for item in ValidationLayer},
})


def subject_from_factor_ir(program: object, *, factor_id: str) -> FormulaValidationSubject:
    identity = getattr(program, "identity", None)
    if identity is None:
        raise ValidationFactoryError(ValidationFactoryFailureCode.INVALID_SUBJECT, "FactorIR identity is required")
    adapter_policies = tuple(getattr(program, "semantic_adapter_policies", ()))
    return FormulaValidationSubject(
        factor_id=factor_id,
        source_kind="FACTOR_IR_V2",
        source_hash=getattr(identity, "source_hash", None),
        program_hash=getattr(identity, "program_hash", None),
        compiler_hash=getattr(identity, "compiler_hash", None),
        operator_policy_hash=getattr(identity, "operator_policy_hash", None),
        adapter_policy_hash=getattr(identity, "semantic_adapter_policy_hash", None),
        postprocess_plan_hash=getattr(identity, "postprocess_plan_hash", None),
        projection_hash=None,
        projection_record_hash=None,
        execution_qualification="STATIC_FACTOR_IR_ONLY",
        required_fields=tuple(getattr(program, "required_fields", ())),
        maximum_lookback=getattr(program, "lookback", None),
        operators=tuple(item.operator for item in getattr(program, "operator_policies", ())),
        semantic_adapters=tuple(item.adapter for item in adapter_policies),
        canonical_expression_available=False,
    )


def subject_from_projection(record: object, *, projection_hash: str) -> FormulaValidationSubject:
    factor_ir_hash = getattr(record, "factor_ir_hash", None)
    program_hash = factor_ir_hash or getattr(record, "program_hash", None)
    qualification = getattr(getattr(record, "execution_qualification", None), "value", getattr(record, "execution_qualification", "UNKNOWN"))
    return FormulaValidationSubject(
        factor_id=getattr(record, "factor_id", ""),
        source_kind="QLIB_FACTOR_LIBRARY_PROJECTION",
        source_hash=getattr(record, "source_hash", None),
        program_hash=program_hash,
        compiler_hash=getattr(record, "runtime_hash", None),
        operator_policy_hash=getattr(record, "factor_ir_policy_hash", None) or getattr(record, "expression_policy_hash", None),
        adapter_policy_hash=getattr(record, "adapter_hash", None),
        postprocess_plan_hash=canonical_hash(list(getattr(record, "postprocess_steps", ()))),
        projection_hash=projection_hash,
        projection_record_hash=getattr(record, "record_hash", None),
        execution_qualification=str(qualification),
        required_fields=tuple(getattr(record, "required_fields", ())),
        maximum_lookback=getattr(record, "maximum_lookback", None),
        operators=tuple(getattr(record, "operators", ())),
        semantic_adapters=tuple(getattr(record, "semantic_adapters", ())),
        missing_fields=tuple(getattr(record, "missing_fields", ())),
        pit_missing_fields=tuple(getattr(record, "pit_missing_fields", ())),
        canonical_expression_available=getattr(record, "canonical_expression", None) is not None,
    )


def _eligibility(subject: FormulaValidationSubject, layer: ValidationLayer) -> tuple[ValidationEligibility, tuple[str, ...]]:
    blockers: list[str] = []
    static_identity = all((subject.program_hash, subject.compiler_hash, subject.operator_policy_hash, subject.postprocess_plan_hash))
    if not static_identity:
        blockers.append("STATIC_PROGRAM_IDENTITY_INCOMPLETE")
    if layer is ValidationLayer.OPERATOR_POLICY and subject.semantic_adapters and subject.adapter_policy_hash is None:
        blockers.append("ADAPTER_POLICY_IDENTITY_INCOMPLETE")
    if layer is ValidationLayer.PINNED_PYQLIB_097_COMPARISON and not subject.canonical_expression_available:
        blockers.append("CANONICAL_QLIB_EXPRESSION_UNAVAILABLE")
    if layer in {
        ValidationLayer.FROZEN_PIT_EXECUTION,
        ValidationLayer.FACTOR_SCORE_ARTIFACT,
        ValidationLayer.TRIAL_LEDGER,
        ValidationLayer.OFFICIAL_EVIDENCE_REPLAY,
    }:
        if subject.execution_qualification != "EXECUTION_READY":
            blockers.append("PROJECTION_EXECUTION_QUALIFICATION_REQUIRED")
        if subject.missing_fields:
            blockers.append("DATA_FIELD_MISSING")
        if subject.pit_missing_fields:
            blockers.append("PIT_METADATA_MISSING")
        if subject.projection_hash is None or subject.projection_record_hash is None:
            blockers.append("PROJECTION_IDENTITY_REQUIRED")
    return (ValidationEligibility.BLOCKED, tuple(sorted(set(blockers)))) if blockers else (ValidationEligibility.ELIGIBLE, ())


def build_formula_validation_matrix(subject: FormulaValidationSubject) -> FormulaValidationMatrix:
    cells: list[FormulaValidationCell] = []
    for scenario in ValidationScenario:
        for layer in ValidationLayer:
            eligibility, blockers = _eligibility(subject, layer)
            cell_id = f"{scenario.value.lower()}::{layer.value.lower()}"
            cells.append(FormulaValidationCell(
                cell_id=cell_id,
                scenario=scenario,
                layer=layer,
                eligibility=eligibility,
                status=ValidationStatus.NOT_RUN,
                assertions=_SCENARIO_ASSERTIONS[scenario] + _LAYER_ASSERTIONS[layer],
                blocker_codes=blockers,
                subject_hash=subject.subject_hash,
            ))
    return FormulaValidationMatrix(subject=subject, cells=tuple(cells), factory_hash=FACTORY_HASH)


def apply_execution_receipts(matrix: FormulaValidationMatrix, receipts: Iterable[ValidationExecutionReceipt]) -> FormulaValidationMatrix:
    by_id = {cell.cell_id: cell for cell in matrix.cells}
    replacements: dict[str, FormulaValidationCell] = {}
    for receipt in receipts:
        if receipt.cell_id in replacements:
            raise ValidationFactoryError(ValidationFactoryFailureCode.DUPLICATE_OUTCOME, "duplicate cell receipt", cell_id=receipt.cell_id)
        cell = by_id.get(receipt.cell_id)
        if cell is None:
            raise ValidationFactoryError(ValidationFactoryFailureCode.UNKNOWN_CELL, "receipt references unknown cell", cell_id=receipt.cell_id)
        if receipt.subject_hash != matrix.subject.subject_hash:
            raise ValidationFactoryError(ValidationFactoryFailureCode.IDENTITY_MISMATCH, "receipt subject hash mismatch")
        if cell.eligibility is ValidationEligibility.BLOCKED:
            raise ValidationFactoryError(ValidationFactoryFailureCode.BLOCKED_CELL, "cannot attach an outcome to blocked cell", cell_id=cell.cell_id)
        replacements[cell.cell_id] = replace(
            cell,
            status=receipt.status,
            evidence_hash=receipt.evidence_hash,
            command_hash=receipt.command_hash,
            runtime_hash=receipt.runtime_hash,
            test_count=receipt.test_count,
        )
    return FormulaValidationMatrix(
        subject=matrix.subject,
        cells=tuple(replacements.get(cell.cell_id, cell) for cell in matrix.cells),
        factory_hash=matrix.factory_hash,
    )


class FormulaValidationFactory:
    factory_hash = FACTORY_HASH

    def from_factor_ir(self, program: object, *, factor_id: str) -> FormulaValidationMatrix:
        return build_formula_validation_matrix(subject_from_factor_ir(program, factor_id=factor_id))

    def from_projection(self, record: object, *, projection_hash: str) -> FormulaValidationMatrix:
        return build_formula_validation_matrix(subject_from_projection(record, projection_hash=projection_hash))

    def apply_receipts(self, matrix: FormulaValidationMatrix, receipts: Iterable[ValidationExecutionReceipt]) -> FormulaValidationMatrix:
        return apply_execution_receipts(matrix, receipts)
