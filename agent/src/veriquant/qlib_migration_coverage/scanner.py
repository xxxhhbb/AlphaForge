"""Pure-static migration scan over the frozen factor-library catalog."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Iterable, Mapping

from ..factor_library import FactorLibraryCatalogBuilder as LegacyCatalogBuilder
from ..factor_library import FactorLibraryRecord
from ..factor_library_qualification import (
    FactorDagCompatibility,
    FactorLibraryCatalogBuilder as QualificationCatalogBuilder,
)
from ..qlib_expression.api import (
    QlibExpressionError,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    STATIC_QLIB_OPERATOR_MANIFEST,
    certify_qlib_expression,
)
from ..qlib_expression.models import stable_hash
from .expressions import SEMANTICS_PARITY_OPERATORS, qlib158_template
from .models import (
    BacklogDisposition,
    MigrationCoverageError,
    MigrationDataPolicy,
    OperatorBacklogItem,
    QlibMigrationCoverageReport,
    QlibMigrationRecord,
    QlibMigrationStatus,
)


_OPERATOR_MAPPING: Mapping[str, tuple[tuple[str, ...], BacklogDisposition, str]] = {
    "conditional": (
        ("If",),
        BacklogDisposition.AVAILABLE,
        "Pinned If is available after exact branch semantics are extracted.",
    ),
    "correlation": (
        ("Corr",),
        BacklogDisposition.AVAILABLE,
        "Pinned rolling Corr covers the operator; formula translation remains separate.",
    ),
    "covariance": (
        ("Cov",),
        BacklogDisposition.AVAILABLE,
        "Pinned rolling Cov covers the operator; ddof parity remains tested downstream.",
    ),
    "decay_linear": (
        ("WMA",),
        BacklogDisposition.SEMANTICS_PARITY_REQUIRED,
        "Weight direction and warmup semantics require parity evidence.",
    ),
    "rolling_max": (
        ("Max",),
        BacklogDisposition.AVAILABLE,
        "Pinned rolling Max is available.",
    ),
    "rolling_min": (
        ("Min",),
        BacklogDisposition.AVAILABLE,
        "Pinned rolling Min is available.",
    ),
    "signed_power": (
        ("Abs", "Power", "Sign"),
        BacklogDisposition.SAFE_COMPOSITE,
        "Use Sign(x) * Power(Abs(x), p).",
    ),
    "time_series_rank": (
        ("Rank",),
        BacklogDisposition.SEMANTICS_PARITY_REQUIRED,
        "Tie, percentile, and NaN semantics require parity evidence.",
    ),
    "ts_argmax": (
        ("IdxMax",),
        BacklogDisposition.SEMANTICS_PARITY_REQUIRED,
        "Index origin and tie direction require parity evidence.",
    ),
    "ts_argmin": (
        ("IdxMin",),
        BacklogDisposition.SEMANTICS_PARITY_REQUIRED,
        "Index origin and tie direction require parity evidence.",
    ),
}


def _bytes_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _formula_metadata(
    root: Path, record: FactorLibraryRecord
) -> tuple[str | None, str]:
    path = (root / record.source_relpath).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MigrationCoverageError(
            "PATH_TRAVERSAL", "factor source escapes zoo root"
        ) from exc
    payload = path.read_bytes()
    if _bytes_hash(payload) != record.source_hash:
        raise MigrationCoverageError(
            "SOURCE_HASH_MISMATCH",
            "factor source hash changed during migration scan",
            factor_id=record.factor_id,
        )
    try:
        tree = ast.parse(payload.decode("utf-8"), filename=record.source_relpath)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise MigrationCoverageError(
            "MALFORMED_SOURCE", "factor source is not valid static Python"
        ) from exc
    assignments = [
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__alpha_meta__"
            for target in statement.targets
        )
    ]
    if len(assignments) != 1:
        return None, stable_hash(
            {"source_hash": record.source_hash, "formula": "UNAVAILABLE"}
        )
    try:
        metadata = ast.literal_eval(assignments[0])
    except (TypeError, ValueError, SyntaxError):
        return None, stable_hash(
            {"source_hash": record.source_hash, "formula": "NOT_LITERAL"}
        )
    formula = metadata.get("formula_latex") if isinstance(metadata, Mapping) else None
    if not isinstance(formula, str) or not formula.strip():
        return None, stable_hash(
            {"source_hash": record.source_hash, "formula": "MISSING"}
        )
    return formula, stable_hash(
        {"source_hash": record.source_hash, "formula_latex": formula}
    )


def _legacy_operators(compatibility_reasons: tuple[str, ...]) -> tuple[str, ...]:
    prefix = "MISSING_OPERATOR:"
    return tuple(
        sorted(
            reason[len(prefix) :]
            for reason in compatibility_reasons
            if reason.startswith(prefix)
        )
    )


def _expression_policy(fields: tuple[str, ...]) -> QlibExpressionPolicy:
    allowed = tuple(
        item.name
        for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust is not QlibOperatorTrust.EXCLUDED
    )
    return QlibExpressionPolicy(
        field_roles={field: QlibFieldRole.FEATURE for field in fields},
        allowed_operators=allowed,
    )


def _operator_compatibility(
    legacy_operators: tuple[str, ...]
) -> tuple[bool, tuple[str, ...]]:
    if not legacy_operators:
        return False, ()
    mapped: set[str] = set()
    for operator in legacy_operators:
        assessment = _OPERATOR_MAPPING.get(operator)
        if assessment is None or assessment[1] is BacklogDisposition.UNSUPPORTED:
            return False, tuple(sorted(mapped))
        mapped.update(assessment[0])
    return True, tuple(sorted(mapped))


class QlibMigrationCoverageScanner:
    """Build exact, mutually exclusive coverage without importing factor modules."""

    def __init__(
        self,
        zoo_root: Path | str | None = None,
        *,
        data_policy: MigrationDataPolicy | None = None,
        expected_total: int = 461,
        expected_legacy_gap_count: int = 390,
    ) -> None:
        default_root = Path(__file__).resolve().parents[2] / "factors" / "zoo"
        self.zoo_root = Path(zoo_root) if zoo_root is not None else default_root
        self.data_policy = data_policy or MigrationDataPolicy.default()
        self.expected_total = expected_total
        self.expected_legacy_gap_count = expected_legacy_gap_count

    def scan(
        self,
        *,
        paths: Iterable[Path | str] | None = None,
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> QlibMigrationCoverageReport:
        try:
            root = self.zoo_root.resolve(strict=True)
            legacy_catalog = LegacyCatalogBuilder(root).build(paths=paths)
            baseline = QualificationCatalogBuilder(root).build(
                paths=paths,
                expected_source_hashes=expected_source_hashes,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "CATALOG_BUILD_FAILED"))
            raise MigrationCoverageError(
                code, "static factor catalog failed validation"
            ) from exc
        legacy_by_id = {record.factor_id: record for record in legacy_catalog.records}
        baseline_by_id = {entry.factor_id: entry for entry in baseline.entries}
        if set(legacy_by_id) != set(baseline_by_id):
            raise MigrationCoverageError(
                "CATALOG_IDENTITY_MISMATCH", "catalog and qualification ids differ"
            )
        records = tuple(
            self._record(root, legacy_by_id[factor_id], baseline_by_id[factor_id])
            for factor_id in sorted(legacy_by_id)
        )
        backlog = self._backlog(records)
        return QlibMigrationCoverageReport(
            catalog_hash=legacy_catalog.catalog_hash,
            baseline_qualification_hash=baseline.catalog_hash,
            operator_manifest_hash=STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash,
            data_policy=self.data_policy,
            records=records,
            operator_backlog=backlog,
            expected_total=self.expected_total,
            expected_legacy_gap_count=self.expected_legacy_gap_count,
        )

    def _record(
        self, root: Path, source: FactorLibraryRecord, baseline: object
    ) -> QlibMigrationRecord:
        formula, formula_hash = _formula_metadata(root, source)
        compatibility = baseline.compatibility
        baseline_value = compatibility.value
        legacy_gap = (
            compatibility is FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR
        )
        required = set(source.fields)
        if source.requires_sector:
            required.add("sector")
        required_fields = tuple(sorted(required))
        missing_fields = tuple(
            sorted(required - set(self.data_policy.available_fields))
        )
        pit_missing = tuple(
            sorted(
                (required & set(self.data_policy.available_fields))
                - set(self.data_policy.pit_ready_fields)
            )
        )
        legacy_operators = _legacy_operators(baseline.compatibility_reasons)
        operator_compatible, mapped_operators = _operator_compatibility(
            legacy_operators
        )
        composite: tuple[str, ...] = ()
        postprocess: tuple[str, ...] = ()
        canonical_expression = None
        expression_hash = None
        expression_policy_hash = None
        certified_operators: tuple[str, ...] = ()
        reason_codes: list[str] = []
        semantics_review = formula is None
        if formula is None:
            reason_codes.append("FORMULA_METADATA_UNAVAILABLE")

        if baseline_value == FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY.value:
            status = QlibMigrationStatus.AUXILIARY_ONLY
            reason_codes.append("BASELINE_UNSAFE_AUXILIARY_ONLY")
        else:
            template = qlib158_template(source.factor_id)
            if source.factor_id == "academic_strev":
                template_expression = "0-($close/Ref($close,21)-1)"
                postprocess = ("cross_sectional_zscore",)
            else:
                template_expression = (
                    template.expression if template is not None else None
                )
                composite = template.composite_operators if template is not None else ()
            if template_expression is not None:
                expression_fields = tuple(
                    field
                    for field in required_fields
                    if field
                    in {"open", "high", "low", "close", "volume", "vwap", "amount"}
                )
                try:
                    policy = _expression_policy(expression_fields)
                    certified = certify_qlib_expression(
                        template_expression, policy=policy
                    )
                except QlibExpressionError as exc:
                    semantics_review = True
                    reason_codes.append("QLIB_CERTIFICATION_FAILED:" + exc.code.value)
                else:
                    canonical_expression = certified.canonical_expression
                    expression_hash = certified.expression_hash
                    expression_policy_hash = certified.policy_hash
                    certified_operators = certified.operators
                    operator_compatible = True
                    if SEMANTICS_PARITY_OPERATORS.intersection(certified.operators):
                        semantics_review = True
                        reason_codes.append("OPERATOR_PARITY_REVIEW_REQUIRED")

            if missing_fields:
                status = QlibMigrationStatus.DATA_FIELD_MISSING
                reason_codes.append("REQUIRED_FIELD_UNAVAILABLE")
            elif pit_missing:
                status = QlibMigrationStatus.PIT_METADATA_MISSING
                reason_codes.append("FIELD_NOT_PIT_READY")
            elif canonical_expression is None or semantics_review:
                status = QlibMigrationStatus.SEMANTICS_REVIEW_REQUIRED
                reason_codes.append("EXACT_QLIB_SEMANTICS_NOT_CERTIFIED")
            elif postprocess:
                status = QlibMigrationStatus.QLIB_WITH_POSTPROCESS
                reason_codes.append("CROSS_SECTIONAL_POSTPROCESS_REQUIRED")
            elif composite:
                status = QlibMigrationStatus.QLIB_SAFE_COMPOSITE
                reason_codes.append("SAFE_COMPOSITE_REQUIRED")
            else:
                status = QlibMigrationStatus.QLIB_DIRECT
                reason_codes.append("CERTIFIED_QLIB_EXPRESSION_AVAILABLE")

        expression_certified = canonical_expression is not None
        data_available = not missing_fields
        pit_available = data_available and not pit_missing
        executable = (
            status
            in {
                QlibMigrationStatus.QLIB_DIRECT,
                QlibMigrationStatus.QLIB_WITH_POSTPROCESS,
                QlibMigrationStatus.QLIB_SAFE_COMPOSITE,
            }
            and expression_certified
            and data_available
            and pit_available
        )
        return QlibMigrationRecord(
            factor_id=source.factor_id,
            zoo=source.zoo,
            source_relpath=source.source_relpath,
            source_hash=source.source_hash,
            metadata_hash=source.metadata_hash,
            source_formula_hash=formula_hash,
            baseline_compatibility=baseline_value,
            legacy_gap_member=legacy_gap,
            status=status,
            required_fields=required_fields,
            missing_fields=missing_fields,
            pit_missing_fields=pit_missing,
            detected_legacy_operators=legacy_operators,
            qlib_operators=tuple(
                sorted(set(mapped_operators) | set(certified_operators))
            ),
            composite_operators=composite,
            postprocess_steps=postprocess,
            canonical_expression=canonical_expression,
            expression_hash=expression_hash,
            expression_policy_hash=expression_policy_hash,
            operator_compatible=operator_compatible,
            expression_certified=expression_certified,
            data_fields_available=data_available,
            pit_metadata_available=pit_available,
            execution_eligible=executable,
            reason_codes=tuple(sorted(set(reason_codes))),
        )

    @staticmethod
    def _backlog(
        records: tuple[QlibMigrationRecord, ...]
    ) -> tuple[OperatorBacklogItem, ...]:
        impacted: dict[str, list[str]] = {}
        for record in records:
            for operator in record.detected_legacy_operators:
                impacted.setdefault(operator, []).append(record.factor_id)
        result: list[OperatorBacklogItem] = []
        for operator, factor_ids in impacted.items():
            mapping = _OPERATOR_MAPPING.get(operator)
            if mapping is None:
                qlib_names, disposition, note = (
                    (),
                    BacklogDisposition.UNSUPPORTED,
                    "No reviewed pinned mapping is registered.",
                )
            else:
                qlib_names, disposition, note = mapping
            result.append(
                OperatorBacklogItem(
                    legacy_operator=operator,
                    qlib_mapping=qlib_names,
                    disposition=disposition,
                    impacted_factor_ids=tuple(factor_ids),
                    note=note,
                )
            )
        return tuple(result)


def build_qlib_migration_coverage(
    zoo_root: Path | str | None = None,
    *,
    data_policy: MigrationDataPolicy | None = None,
) -> QlibMigrationCoverageReport:
    """Build the default exact 461-factor migration report."""

    return QlibMigrationCoverageScanner(zoo_root, data_policy=data_policy).scan()
