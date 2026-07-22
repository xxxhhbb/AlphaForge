"""Mechanical merger for factor catalog and Qlib qualification artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..factor_library import FactorLibraryCatalog, build_factor_library_catalog
from ..factor_ir_v2 import FactorIRProgram, canonical_hash as factor_ir_hash
from ..formula_semantic_adapters import ADAPTER_CATALOG_VERSION, ADAPTER_POLICY_HASH
from ..static_formula_compiler import try_compile_qlib_safe_expression
from ..postprocess_identity import (
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
)
from ..qlib_expression.api import (
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    STATIC_QLIB_OPERATOR_MANIFEST,
    certify_qlib_expression,
)
from ..qlib_formula_migration import (
    FormulaMigrationRecord,
    FormulaMigrationReport,
    FormulaMigrationStatus,
    build_qlib_formula_migration,
)
from ..qlib_migration_coverage import (
    QlibMigrationCoverageReport,
    QlibMigrationRecord,
    QlibMigrationStatus,
    build_qlib_migration_coverage,
)
from ..qlib_semantic_parity import (
    EXPECTED_DISPOSITIONS,
    OPERATOR_POLICIES,
    SEMANTIC_POLICY_HASH,
    SemanticParityStatus,
)
from .models import (
    OperatorParityProjection,
    ProgramOrigin,
    ProjectionError,
    ProjectionFailureCode,
    QlibExecutionQualification,
    QlibFactorLibraryProjection,
    QlibFactorProjectionRecord,
    stable_hash,
)


EXPECTED_PARITY_REPORT_HASH = "sha256:5cfe73c155793dd0d29a45b178b081a2bfaf5bef823e5258b0b675bb665ae1ad"
KNOWN_DIFFERENCE_OPERATORS = frozenset(("IdxMax", "IdxMin", "If", "WMA"))
SAFE_ADAPTER_FOR_NATIVE = {"IdxMax": "SafeIdxMax", "IdxMin": "SafeIdxMin", "If": "SafeIf", "WMA": "SafeWMA"}
FACTOR_IR_RUNTIME_HASH = factor_ir_hash(
    {
        "runtime": "veriquant.factor-ir-v2-typed-runtime/v1",
        "adapter_catalog_version": ADAPTER_CATALOG_VERSION,
        "adapter_policy_hash": ADAPTER_POLICY_HASH,
        "native_string_resolver": False,
        "generated_python": False,
    }
)
_POSTPROCESS_OPERATOR = {
    "rank_cross_section": (StaticPostprocessOperator.RANK, RANK_POLICY_VERSION),
    "zscore_cross_section": (StaticPostprocessOperator.ZSCORE, ZSCORE_POLICY_VERSION),
    "winsorize": (StaticPostprocessOperator.WINSORIZE, WINSORIZE_POLICY_VERSION),
    "neutralize_by_group": (StaticPostprocessOperator.INDUSTRY_NEUTRALIZE, INDUSTRY_NEUTRALIZATION_VERSION),
    "neutralize_by_industry": (StaticPostprocessOperator.INDUSTRY_NEUTRALIZE, INDUSTRY_NEUTRALIZATION_VERSION),
    "neutralize_by_market_cap": (StaticPostprocessOperator.SIZE_NEUTRALIZE, SIZE_NEUTRALIZATION_VERSION),
}
_MARKET_FIELDS = frozenset(("open", "high", "low", "close", "volume", "vwap", "amount"))
_POSTPROCESS_ALIASES = {"cross_sectional_zscore": "zscore_cross_section"}
_SUPPORTED_POSTPROCESS = frozenset(
    (
        "rank_cross_section",
        "zscore_cross_section",
        "winsorize",
        "neutralize_by_group",
        "neutralize_by_industry",
        "neutralize_by_market_cap",
    )
)


@dataclass(frozen=True, slots=True)
class _ProgramCandidate:
    origin: ProgramOrigin
    program_hash: str
    canonical_expression: str
    expression_hash: str
    policy_hash: str
    manifest_hash: str
    required_fields: tuple[str, ...]
    maximum_lookback: int
    operators: tuple[str, ...]
    postprocess_steps: tuple[str, ...]


def _fail(code: ProjectionFailureCode, message: str, **details: object) -> None:
    raise ProjectionError(code, message, **details)


def _default_parity_snapshot() -> Mapping[str, object]:
    repo_root = Path(__file__).resolve().parents[4]
    path = repo_root / ".autobuild" / "veriquant" / "parallel" / "qlib_semantic_parity" / "real_smoke_result.json"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity artifact is unavailable", error=type(exc).__name__)
    if len(payload) > 256_000:
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity artifact exceeds size cap")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity artifact is malformed", error=type(exc).__name__)
    if not isinstance(value, Mapping):
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity artifact must be an object")
    return value


def _validate_parity(snapshot: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    if (
        snapshot.get("status") != "PASSED"
        or snapshot.get("replay_stable") is not True
        or snapshot.get("pyqlib_version") != "0.9.7"
        or snapshot.get("semantic_policy_hash") != SEMANTIC_POLICY_HASH
        or snapshot.get("report_hash") != EXPECTED_PARITY_REPORT_HASH
    ):
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity identity or status is invalid")
    raw = snapshot.get("operator_evidence")
    if not isinstance(raw, Mapping) or set(raw) != set(OPERATOR_POLICIES):
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "measured parity operator set is incomplete")
    result: dict[str, Mapping[str, object]] = {}
    total_cases = 0
    for operator in sorted(OPERATOR_POLICIES):
        item = raw[operator]
        if not isinstance(item, Mapping):
            _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "operator parity entry is malformed", operator=operator)
        expected = EXPECTED_DISPOSITIONS[operator]
        matched = item.get("matched")
        mismatched = item.get("mismatched")
        if (
            item.get("status") != expected.value
            or item.get("policy_hash") != OPERATOR_POLICIES[operator].policy_hash
            or isinstance(matched, bool)
            or not isinstance(matched, int)
            or isinstance(mismatched, bool)
            or not isinstance(mismatched, int)
            or matched < 0
            or mismatched < 0
            or matched + mismatched <= 0
        ):
            _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "operator parity evidence failed validation", operator=operator)
        if expected is SemanticParityStatus.CERTIFIED_EQUIVALENT and mismatched:
            _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "equivalent operator contains mismatches", operator=operator)
        if expected is SemanticParityStatus.KNOWN_DIFFERENCE and not mismatched:
            _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "known difference lacks a mismatch", operator=operator)
        total_cases += matched + mismatched
        result[operator] = item
    if snapshot.get("case_count") != total_cases:
        _fail(ProjectionFailureCode.PARITY_EVIDENCE_INVALID, "parity case count is inconsistent")
    return result


def _coverage_candidate(record: QlibMigrationRecord, *, source_hash: str) -> _ProgramCandidate | None:
    if not record.execution_eligible:
        return None
    if record.canonical_expression is None:
        _fail(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "eligible coverage record has no expression", factor_id=record.factor_id)
    fields = tuple(field for field in record.required_fields if field in _MARKET_FIELDS)
    allowed = tuple(
        spec.name
        for spec in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if spec.trust is not QlibOperatorTrust.EXCLUDED
    )
    policy = QlibExpressionPolicy(
        field_roles={field: QlibFieldRole.FEATURE for field in fields},
        allowed_operators=allowed,
    )
    certified = certify_qlib_expression(record.canonical_expression, policy=policy)
    if (
        certified.expression_hash != record.expression_hash
        or certified.policy_hash != record.expression_policy_hash
    ):
        _fail(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "coverage expression identity changed", factor_id=record.factor_id)
    steps = tuple(sorted(_POSTPROCESS_ALIASES.get(step, step) for step in record.postprocess_steps))
    return _ProgramCandidate(
        origin=ProgramOrigin.REVIEWED_TEMPLATE,
        program_hash=stable_hash(
            {
                "schema_version": "veriquant.qlib-reviewed-template-program/v1",
                "factor_id": record.factor_id,
                "source_hash": source_hash,
                "expression_hash": certified.expression_hash,
                "policy_hash": certified.policy_hash,
                "operator_manifest_hash": certified.manifest_hash,
                "required_fields": list(record.required_fields),
                "maximum_lookback": certified.maximum_lookback,
                "postprocess_steps": list(steps),
            }
        ),
        canonical_expression=certified.canonical_expression,
        expression_hash=certified.expression_hash,
        policy_hash=certified.policy_hash,
        manifest_hash=certified.manifest_hash,
        required_fields=record.required_fields,
        maximum_lookback=certified.maximum_lookback,
        operators=certified.operators,
        postprocess_steps=steps,
    )


def _formula_candidate(record: FormulaMigrationRecord) -> _ProgramCandidate | None:
    if not record.certified:
        return None
    if None in (
        record.canonical_expression,
        record.expression_hash,
        record.expression_policy_hash,
        record.operator_manifest_hash,
    ):
        _fail(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "certified whole formula has incomplete identity", factor_id=record.factor_id)
    steps = tuple(sorted(_POSTPROCESS_ALIASES.get(step, step) for step in record.postprocess_steps))
    return _ProgramCandidate(
        origin=ProgramOrigin.WHOLE_FORMULA,
        program_hash=record.program_hash,
        canonical_expression=str(record.canonical_expression),
        expression_hash=str(record.expression_hash),
        policy_hash=str(record.expression_policy_hash),
        manifest_hash=str(record.operator_manifest_hash),
        required_fields=record.required_fields,
        maximum_lookback=record.maximum_lookback,
        operators=record.operators,
        postprocess_steps=steps,
    )


def _parity_projection(
    operators: tuple[str, ...],
    evidence: Mapping[str, Mapping[str, object]],
) -> tuple[OperatorParityProjection, ...]:
    result = []
    for operator in sorted(set(operators).intersection(OPERATOR_POLICIES)):
        item = evidence[operator]
        reason = ()
        if item["status"] == SemanticParityStatus.KNOWN_DIFFERENCE.value:
            difference = item.get("difference")
            reason = (str(difference),) if isinstance(difference, str) else (f"QLIB_KNOWN_DIFFERENCE:{operator}",)
        result.append(
            OperatorParityProjection(
                operator=operator,
                status=str(item["status"]),
                policy_hash=str(item["policy_hash"]),
                reason_codes=reason,
            )
        )
    return tuple(result)


def _compile_factor_ir(candidate: _ProgramCandidate) -> FactorIRProgram | None:
    result = try_compile_qlib_safe_expression(candidate.canonical_expression, allowed_fields=candidate.required_fields)
    return result.program


def _candidate_safe(candidate: _ProgramCandidate, evidence: Mapping[str, Mapping[str, object]]) -> FactorIRProgram | None:
    if any(step not in _SUPPORTED_POSTPROCESS for step in candidate.postprocess_steps):
        return None
    program = _compile_factor_ir(candidate)
    if program is None:
        return None
    adapters = {item.adapter for item in program.semantic_adapter_policies}
    for operator in set(candidate.operators).intersection(OPERATOR_POLICIES):
        if evidence[operator]["status"] != SemanticParityStatus.CERTIFIED_EQUIVALENT.value and SAFE_ADAPTER_FOR_NATIVE.get(operator) not in adapters:
            return None
    return program


def _static_program_identity(candidate: _ProgramCandidate, program: FactorIRProgram):
    steps = []
    for name in candidate.postprocess_steps:
        operator, version = _POSTPROCESS_OPERATOR[name]
        values: dict[str, object] = {"operator": operator, "policy_version": version}
        if operator is StaticPostprocessOperator.WINSORIZE:
            values.update(lower=0.01, upper=0.99)
        steps.append(StaticPostprocessStep(**values))
    plan = CrossSectionalPostprocessPlan(plan_id="factor-ir-v2.postprocess.v1", steps=tuple(steps))
    neutralization = NeutralizationPolicy(policy_id="factor-ir-v2.neutralization.v1")
    runtime = PostprocessRuntimePolicy(
        policy_id="factor-ir-v2.runtime.v1",
        runtime_version="factor-ir-v2-static-runtime/v1",
    )
    return derive_program_identity(
        time_series_formula_ir_hash=program.program_hash,
        postprocess_plan=plan,
        neutralization_policy=neutralization,
        runtime_policy=runtime,
        compiler_version="factor-ir-v2/compiler-v2",
    )


class QlibFactorLibraryProjectionBuilder:
    """Merge only static/read-only artifacts into a content-addressed view."""

    def build(self) -> QlibFactorLibraryProjection:
        return self.build_from_artifacts(
            catalog=build_factor_library_catalog(),
            coverage=build_qlib_migration_coverage(),
            formula=build_qlib_formula_migration(),
            parity_snapshot=_default_parity_snapshot(),
        )

    def build_from_artifacts(
        self,
        *,
        catalog: FactorLibraryCatalog,
        coverage: QlibMigrationCoverageReport,
        formula: FormulaMigrationReport,
        parity_snapshot: Mapping[str, object],
    ) -> QlibFactorLibraryProjection:
        if (
            catalog.catalog_hash != coverage.catalog_hash
            or catalog.catalog_hash != formula.catalog_hash
            or coverage.report_hash != formula.baseline_coverage_hash
        ):
            _fail(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "catalog/coverage/formula report hashes do not form one chain")
        catalog_by_id = {record.factor_id: record for record in catalog.records}
        coverage_by_id = {record.factor_id: record for record in coverage.records}
        formula_by_id = {record.factor_id: record for record in formula.records}
        if not (set(catalog_by_id) == set(coverage_by_id) == set(formula_by_id)):
            _fail(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "factor ids differ across input reports")
        if set(formula.baseline_certified_ids) != {
            record.factor_id for record in coverage.records if record.execution_eligible
        }:
            _fail(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "formula baseline ids differ from coverage execution ids")
        coverage_aux = {
            record.factor_id for record in coverage.records if record.status is QlibMigrationStatus.AUXILIARY_ONLY
        }
        formula_aux = {
            record.factor_id for record in formula.records if record.status is FormulaMigrationStatus.AUXILIARY_ONLY
        }
        if coverage_aux != formula_aux:
            _fail(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "auxiliary-only identities differ across reports")
        parity = _validate_parity(parity_snapshot)
        records = tuple(
            self._record(
                catalog_by_id[factor_id],
                coverage_by_id[factor_id],
                formula_by_id[factor_id],
                coverage,
                formula,
                parity,
            )
            for factor_id in sorted(catalog_by_id)
        )
        static_union = set(formula.certified_ids) | {
            record.factor_id for record in coverage.records if record.execution_eligible
        }
        ready = sum(record.execution_qualification is QlibExecutionQualification.EXECUTION_READY for record in records)
        source_counts = {
            "catalog_total": len(catalog.records),
            "operator_compatible": coverage.operator_compatible_count,
            "coverage_execution_eligible": coverage.execution_eligible_count,
            "formula_certified": len(formula.certified_ids),
            "static_certified_union": len(static_union),
            "formula_semantics_review": sum(
                record.status is FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED for record in formula.records
            ),
            "auxiliary_only": len(formula_aux),
            "parity_downgraded_from_static_union": len(static_union) - ready,
        }
        return QlibFactorLibraryProjection(
            records=records,
            catalog_hash=catalog.catalog_hash,
            coverage_report_hash=coverage.report_hash,
            formula_report_hash=formula.report_hash,
            parity_policy_hash=SEMANTIC_POLICY_HASH,
            parity_report_hash=str(parity_snapshot["report_hash"]),
            source_counts=source_counts,
        )

    @staticmethod
    def _record(
        catalog: object,
        coverage: QlibMigrationRecord,
        formula: FormulaMigrationRecord,
        coverage_report: QlibMigrationCoverageReport,
        formula_report: FormulaMigrationReport,
        parity_evidence: Mapping[str, Mapping[str, object]],
    ) -> QlibFactorProjectionRecord:
        if (
            catalog.source_hash != coverage.source_hash
            or catalog.source_hash != formula.source_hash
            or catalog.metadata_hash != coverage.metadata_hash
            or catalog.metadata_hash != formula.metadata_hash
            or catalog.source_relpath != coverage.source_relpath
            or catalog.source_relpath != formula.source_relpath
            or catalog.zoo != coverage.zoo
            or catalog.zoo != formula.zoo
        ):
            _fail(ProjectionFailureCode.SOURCE_HASH_MISMATCH, "per-factor source identity differs across reports", factor_id=catalog.factor_id)

        whole = _formula_candidate(formula)
        template = _coverage_candidate(coverage, source_hash=catalog.source_hash)
        candidates = tuple(item for item in (whole, template) if item is not None)
        compiled = tuple((item, _candidate_safe(item, parity_evidence)) for item in candidates)
        safe = tuple((item, program) for item, program in compiled if program is not None)
        selected = safe[0][0] if safe else (candidates[0] if candidates else None)
        selected_factor_ir = safe[0][1] if safe else None
        static_union_member = bool(candidates)
        data_missing = set(coverage.missing_fields)
        pit_missing = set(coverage.pit_missing_fields)
        if selected is not None:
            available = set(coverage_report.data_policy.available_fields)
            pit_ready = set(coverage_report.data_policy.pit_ready_fields)
            data_missing.update(set(selected.required_fields) - available)
            pit_missing.update((set(selected.required_fields) & available) - pit_ready)

        reasons = set(coverage.reason_codes) | set(formula.reason_codes)
        if coverage.operator_compatible and not static_union_member:
            reasons.add("OPERATOR_COMPATIBLE_NOT_EXECUTION_READY")
        all_detected_operators = tuple(
            sorted(
                {operator for item in candidates for operator in item.operators}
                | set(coverage.qlib_operators)
                | set(formula.operators)
            )
        )
        # A runnable record is judged only by the exact selected program.  A
        # non-runnable record retains static operator hints so the reason for
        # review is not erased merely because whole-formula translation failed.
        parity_basis = selected.operators if safe and selected is not None else all_detected_operators
        parity = _parity_projection(parity_basis, parity_evidence)
        for item in parity:
            if item.status == SemanticParityStatus.KNOWN_DIFFERENCE.value:
                reasons.add(f"PARITY_KNOWN_DIFFERENCE:{item.operator}")
                reasons.update(item.reason_codes)
        if selected is not None:
            unsupported_post = set(selected.postprocess_steps) - _SUPPORTED_POSTPROCESS
            for step in unsupported_post:
                reasons.add(f"POSTPROCESS_NOT_CERTIFIED:{step}")

        auxiliary = formula.status is FormulaMigrationStatus.AUXILIARY_ONLY
        if auxiliary:
            qualification = QlibExecutionQualification.AUXILIARY_ONLY
        elif data_missing:
            qualification = QlibExecutionQualification.DATA_FIELD_MISSING
        elif pit_missing:
            qualification = QlibExecutionQualification.PIT_METADATA_MISSING
        elif safe:
            qualification = QlibExecutionQualification.EXECUTION_READY
            selected, selected_factor_ir = safe[0]
            reasons.add("FACTOR_IR_V2_EXECUTION_READY_AFTER_PARITY_AND_ADAPTER_FENCE")
        else:
            qualification = QlibExecutionQualification.SEMANTICS_REVIEW_REQUIRED
            reasons.add("NO_PARITY_SAFE_EXECUTION_PROGRAM")

        if selected is None:
            origin = ProgramOrigin.NONE
            program_hash = expression_hash = policy_hash = manifest_hash = expression = None
            fields = tuple(sorted(set(coverage.required_fields) | set(formula.required_fields)))
            lookback = None
            operators = all_detected_operators
            postprocess = ()
            factor_program_hash = factor_policy_hash = runtime_hash = adapter_hash = pit_requirement_hash = None
            semantic_adapters: tuple[str, ...] = ()
        else:
            origin = ProgramOrigin.FACTOR_IR_V2 if selected_factor_ir is not None else selected.origin
            program_hash = selected.program_hash
            expression_hash = selected.expression_hash
            policy_hash = selected.policy_hash
            manifest_hash = selected.manifest_hash
            expression = selected.canonical_expression
            fields = selected.required_fields
            lookback = selected.maximum_lookback
            operators = selected.operators
            postprocess = selected.postprocess_steps
            if selected_factor_ir is None:
                factor_program_hash = factor_policy_hash = runtime_hash = adapter_hash = pit_requirement_hash = None
                semantic_adapters = ()
            else:
                factor_program_hash = selected_factor_ir.program_hash
                static_identity = _static_program_identity(selected, selected_factor_ir)
                program_hash = static_identity.program_hash
                factor_policy_hash = factor_ir_hash(
                    {
                        "operator_policy_hash": selected_factor_ir.identity.operator_policy_hash,
                        "semantic_adapter_policy_hash": selected_factor_ir.identity.semantic_adapter_policy_hash,
                        "postprocess_plan_hash": selected_factor_ir.identity.postprocess_plan_hash,
                    }
                )
                runtime_hash = factor_ir_hash(
                    {
                        "factor_ir_runtime_hash": FACTOR_IR_RUNTIME_HASH,
                        "postprocess_runtime_policy_hash": static_identity.runtime_policy_hash,
                    }
                )
                adapter_hash = factor_ir_hash([item.adapter_hash for item in selected_factor_ir.semantic_adapter_policies])
                pit_requirement_hash = factor_ir_hash(
                    {
                        "required_fields": list(selected_factor_ir.required_fields),
                        "maximum_lookback": selected_factor_ir.lookback,
                        "availability_rule": "available_at<=decision_time",
                        "historical_universe_required": True,
                    }
                )
                semantic_adapters = tuple(item.adapter for item in selected_factor_ir.semantic_adapter_policies)

        return QlibFactorProjectionRecord(
            factor_id=catalog.factor_id,
            zoo=catalog.zoo,
            family=catalog.family,
            themes=catalog.themes,
            markets=catalog.markets,
            source_relpath=catalog.source_relpath,
            catalog_hash=coverage_report.catalog_hash,
            source_hash=catalog.source_hash,
            metadata_hash=catalog.metadata_hash,
            coverage_report_hash=coverage_report.report_hash,
            formula_report_hash=formula_report.report_hash,
            coverage_status=coverage.status.value,
            formula_status=formula.status.value,
            execution_qualification=qualification,
            program_origin=origin,
            static_union_member=static_union_member,
            operator_compatible=coverage.operator_compatible,
            required_fields=fields,
            missing_fields=tuple(data_missing),
            pit_missing_fields=tuple(pit_missing),
            maximum_lookback=lookback,
            program_hash=program_hash,
            expression_hash=expression_hash,
            expression_policy_hash=policy_hash,
            operator_manifest_hash=manifest_hash,
            canonical_expression=expression,
            operators=operators,
            postprocess_steps=postprocess,
            factor_ir_hash=factor_program_hash,
            factor_ir_policy_hash=factor_policy_hash,
            runtime_hash=runtime_hash,
            adapter_hash=adapter_hash,
            pit_requirement_hash=pit_requirement_hash,
            semantic_adapters=semantic_adapters,
            parity_policy_hash=SEMANTIC_POLICY_HASH,
            parity_report_hash=EXPECTED_PARITY_REPORT_HASH,
            parity=parity,
            reason_codes=tuple(reasons),
        )


def build_qlib_factor_library_projection() -> QlibFactorLibraryProjection:
    return QlibFactorLibraryProjectionBuilder().build()


__all__ = [
    "EXPECTED_PARITY_REPORT_HASH",
    "KNOWN_DIFFERENCE_OPERATORS",
    "QlibFactorLibraryProjectionBuilder",
    "build_qlib_factor_library_projection",
]
