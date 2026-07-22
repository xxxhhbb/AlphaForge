"""Deterministic whole-formula migration over the frozen 461-factor zoo."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Iterable, Mapping

from ..factor_library import FactorLibraryCatalogBuilder, FactorLibraryRecord
from ..qlib_expression.api import (
    QlibExpressionError,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    STATIC_QLIB_OPERATOR_MANIFEST,
    certify_qlib_expression,
)
from ..qlib_expression.models import stable_hash
from ..qlib_migration_coverage import (
    QlibMigrationCoverageReport,
    QlibMigrationCoverageScanner,
)
from .models import (
    FormulaMigrationError,
    FormulaMigrationFailureCode,
    FormulaMigrationRecord,
    FormulaMigrationReport,
    FormulaMigrationStatus,
)
from .translator import StaticComputeTranslator


MAX_SOURCE_BYTES = 250_000


def _bytes_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _policy(fields: tuple[str, ...]) -> QlibExpressionPolicy:
    allowed = tuple(
        spec.name
        for spec in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if spec.trust is not QlibOperatorTrust.EXCLUDED
    )
    return QlibExpressionPolicy(
        field_roles={field: QlibFieldRole.FEATURE for field in fields},
        allowed_operators=allowed,
        max_expression_length=16_384,
        max_nodes=256,
        max_depth=48,
    )


def _literal_formula(tree: ast.Module, source_hash: str) -> tuple[str | None, str]:
    nodes = [
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__alpha_meta__"
            for target in statement.targets
        )
    ]
    formula: str | None = None
    if len(nodes) == 1:
        try:
            metadata = ast.literal_eval(nodes[0])
        except (TypeError, ValueError, SyntaxError):
            metadata = None
        if isinstance(metadata, Mapping):
            value = metadata.get("formula_latex")
            if isinstance(value, str) and value.strip():
                formula = value
    return formula, stable_hash(
        {
            "source_hash": source_hash,
            "source_formula": formula if formula is not None else "UNAVAILABLE",
        }
    )


class QlibFormulaMigrationScanner:
    """Scan source bytes only; factor modules and callables are never imported."""

    def __init__(
        self,
        zoo_root: Path | str | None = None,
        *,
        expected_total: int = 461,
        baseline_coverage: QlibMigrationCoverageReport | None = None,
    ) -> None:
        default_root = Path(__file__).resolve().parents[2] / "factors" / "zoo"
        self.zoo_root = Path(zoo_root) if zoo_root is not None else default_root
        self.expected_total = expected_total
        self.baseline_coverage = baseline_coverage

    def scan(
        self,
        *,
        paths: Iterable[Path | str] | None = None,
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> FormulaMigrationReport:
        try:
            root = self.zoo_root.resolve(strict=True)
        except OSError as exc:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.ROOT_UNAVAILABLE,
                "factor zoo root is unavailable",
            ) from exc
        if not root.is_dir():
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.ROOT_UNAVAILABLE,
                "factor zoo root is not a directory",
            )
        try:
            catalog = FactorLibraryCatalogBuilder(root).build(paths=paths)
        except Exception as exc:
            raw_code = str(getattr(getattr(exc, "code", None), "value", getattr(exc, "code", "MALFORMED_SOURCE")))
            mapped = {
                "PATH_TRAVERSAL": FormulaMigrationFailureCode.PATH_TRAVERSAL,
                "INVALID_PATH": FormulaMigrationFailureCode.INVALID_PATH,
                "MALFORMED_SOURCE": FormulaMigrationFailureCode.MALFORMED_SOURCE,
                "SOURCE_TOO_LARGE": FormulaMigrationFailureCode.SOURCE_TOO_LARGE,
                "DUPLICATE_FACTOR_ID": FormulaMigrationFailureCode.DUPLICATE_FACTOR_ID,
            }.get(raw_code, FormulaMigrationFailureCode.MALFORMED_SOURCE)
            raise FormulaMigrationError(mapped, "static catalog build failed") from exc
        if catalog.total_records != self.expected_total:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CATALOG_MISMATCH,
                "catalog total differs from the expected frozen inventory",
                expected=self.expected_total,
                actual=catalog.total_records,
            )
        expected_hashes = dict(expected_source_hashes or {})
        unknown_expected = set(expected_hashes) - {record.factor_id for record in catalog.records}
        if unknown_expected:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CATALOG_MISMATCH,
                "expected source hashes reference unknown factors",
            )
        baseline = self._baseline(root, paths)
        baseline_by_id = {record.factor_id: record for record in baseline.records}
        if set(baseline_by_id) != {record.factor_id for record in catalog.records}:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CATALOG_MISMATCH,
                "coverage baseline and catalog identities differ",
            )
        records = tuple(
            self._record(
                root,
                source,
                baseline_by_id[source.factor_id].status.value == "AUXILIARY_ONLY",
                expected_hashes.get(source.factor_id),
            )
            for source in catalog.records
        )
        baseline_certified = tuple(
            record.factor_id for record in baseline.records if record.execution_eligible
        )
        return FormulaMigrationReport(
            catalog_hash=catalog.catalog_hash,
            baseline_coverage_hash=baseline.report_hash,
            records=records,
            baseline_certified_ids=baseline_certified,
            expected_total=self.expected_total,
        )

    def _baseline(
        self, root: Path, paths: Iterable[Path | str] | None
    ) -> QlibMigrationCoverageReport:
        if self.baseline_coverage is not None:
            return self.baseline_coverage
        if self.expected_total != 461 or paths is not None:
            # Custom fixtures still use the same closed coverage classifier, with
            # the observed gap count supplied mechanically after qualification.
            from ..factor_library_qualification import (
                FactorDagCompatibility,
                FactorLibraryCatalogBuilder as QualificationBuilder,
            )

            qualified = QualificationBuilder(root).build(paths=paths)
            gap = sum(
                entry.compatibility is FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR
                for entry in qualified.entries
            )
            return QlibMigrationCoverageScanner(
                root,
                expected_total=self.expected_total,
                expected_legacy_gap_count=gap,
            ).scan(paths=paths)
        return QlibMigrationCoverageScanner(root).scan()

    def _record(
        self,
        root: Path,
        source: FactorLibraryRecord,
        auxiliary_only: bool,
        expected_source_hash: str | None,
    ) -> FormulaMigrationRecord:
        path = (root / source.source_relpath).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.PATH_TRAVERSAL,
                "factor source escapes the zoo root",
            ) from exc
        payload = path.read_bytes()
        if len(payload) > MAX_SOURCE_BYTES:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.SOURCE_TOO_LARGE,
                "factor source exceeds the static size bound",
            )
        source_hash = _bytes_hash(payload)
        if source_hash != source.source_hash or (
            expected_source_hash is not None and expected_source_hash != source_hash
        ):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.SOURCE_HASH_MISMATCH,
                "factor source hash does not match the frozen identity",
                factor_id=source.factor_id,
            )
        try:
            text = payload.decode("utf-8")
            tree = ast.parse(text, filename=source.source_relpath)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.MALFORMED_SOURCE,
                "factor source is not valid static UTF-8 Python",
            ) from exc
        formula, formula_hash = _literal_formula(tree, source_hash)
        required_fields = tuple(sorted(source.fields))
        if auxiliary_only:
            status = FormulaMigrationStatus.AUXILIARY_ONLY
            reason_codes = (FormulaMigrationFailureCode.AUXILIARY_BASELINE.value,)
            return self._result(
                source,
                formula,
                formula_hash,
                status,
                required_fields,
                None,
                reason_codes,
            )
        try:
            translated = StaticComputeTranslator().translate(tree)
        except FormulaMigrationError as exc:
            return self._result(
                source,
                formula,
                formula_hash,
                FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED,
                required_fields,
                None,
                (exc.code.value,),
            )
        expression_fields = tuple(
            sorted(field for field in required_fields if field.replace("_", "a").isalnum())
        )
        if len(expression_fields) != len(required_fields):
            return self._result(
                source,
                formula,
                formula_hash,
                FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED,
                required_fields,
                None,
                (FormulaMigrationFailureCode.FIELD_NAME_UNSUPPORTED.value,),
            )
        try:
            certified = certify_qlib_expression(
                translated.expression,
                policy=_policy(expression_fields),
            )
        except QlibExpressionError as exc:
            code = (
                FormulaMigrationFailureCode.FUTURE_REFERENCE.value
                if str(exc.code.value) == "FUTURE_REFERENCE"
                else f"{FormulaMigrationFailureCode.CERTIFICATION_FAILED.value}:{exc.code.value}"
            )
            return self._result(
                source,
                formula,
                formula_hash,
                FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED,
                required_fields,
                None,
                (code,),
            )
        if set(certified.required_fields) != set(required_fields):
            return self._result(
                source,
                formula,
                formula_hash,
                FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED,
                required_fields,
                certified,
                ("DECLARED_FIELD_USAGE_MISMATCH",),
                translated.postprocess_steps,
            )
        if translated.parity_operators:
            reasons = tuple(
                f"{FormulaMigrationFailureCode.SEMANTICS_PARITY_REQUIRED.value}:{name}"
                for name in translated.parity_operators
            )
            return self._result(
                source,
                formula,
                formula_hash,
                FormulaMigrationStatus.SEMANTICS_REVIEW_REQUIRED,
                required_fields,
                certified,
                reasons,
                translated.postprocess_steps,
            )
        status = (
            FormulaMigrationStatus.CERTIFIED_WITH_POSTPROCESS
            if translated.postprocess_steps
            else FormulaMigrationStatus.CERTIFIED_QLIB_EXPRESSION
        )
        return self._result(
            source,
            formula,
            formula_hash,
            status,
            required_fields,
            certified,
            ("WHOLE_FORMULA_STATICALLY_TRANSLATED",),
            translated.postprocess_steps,
        )

    @staticmethod
    def _result(
        source: FactorLibraryRecord,
        formula: str | None,
        formula_hash: str,
        status: FormulaMigrationStatus,
        required_fields: tuple[str, ...],
        certified: object | None,
        reason_codes: tuple[str, ...],
        postprocess_steps: tuple[str, ...] = (),
    ) -> FormulaMigrationRecord:
        expression = getattr(certified, "canonical_expression", None)
        expression_hash = getattr(certified, "expression_hash", None)
        policy_hash = getattr(certified, "policy_hash", None)
        manifest_hash = getattr(certified, "manifest_hash", None)
        operators = tuple(getattr(certified, "operators", ()))
        lookback = int(getattr(certified, "maximum_lookback", 0))
        program_hash = stable_hash(
            {
                "schema_version": "veriquant.qlib-formula-program/v1",
                "factor_id": source.factor_id,
                "source_hash": source.source_hash,
                "formula_hash": formula_hash,
                "status": status.value,
                "required_fields": list(required_fields),
                "maximum_lookback": lookback,
                "canonical_expression": expression,
                "expression_hash": expression_hash,
                "policy_hash": policy_hash,
                "operator_manifest_hash": manifest_hash,
                "operators": list(sorted(operators)),
                "postprocess_steps": list(postprocess_steps),
                "reason_codes": list(sorted(reason_codes)),
            }
        )
        return FormulaMigrationRecord(
            factor_id=source.factor_id,
            zoo=source.zoo,
            source_relpath=source.source_relpath,
            source_hash=source.source_hash,
            metadata_hash=source.metadata_hash,
            source_formula=formula,
            formula_hash=formula_hash,
            program_hash=program_hash,
            status=status,
            required_fields=required_fields,
            maximum_lookback=lookback,
            canonical_expression=expression,
            expression_hash=expression_hash,
            expression_policy_hash=policy_hash,
            operator_manifest_hash=manifest_hash,
            operators=operators,
            postprocess_steps=postprocess_steps,
            reason_codes=reason_codes,
        )


def build_qlib_formula_migration(
    zoo_root: Path | str | None = None,
) -> FormulaMigrationReport:
    return QlibFormulaMigrationScanner(zoo_root).scan()


__all__ = ["QlibFormulaMigrationScanner", "build_qlib_formula_migration"]
