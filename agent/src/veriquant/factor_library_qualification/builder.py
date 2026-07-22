"""Fail-closed static qualification over the legacy factor zoo.

The module reads bytes and literal metadata.  It never imports a factor module,
calls ``compute``, executes Python, compiles a FactorDAG, or creates Evidence.
``DIRECT_FACTOR_DAG`` means only that this package can emit a bounded DRAFT
template for later validation by the existing trusted services.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..factor_library import CatalogBuildError
from ..factor_library import FactorLibraryCatalogBuilder as LegacyCatalogBuilder
from ..factor_library import FactorLibraryRecord as LegacyFactorRecord

from .models import (
    EvidenceAuthorityClass,
    FactorDagCompatibility,
    FactorExecutionTrust,
    FactorLibraryCatalog,
    FactorQualificationError,
    FactorQualificationRecord,
    FactorQualificationReport,
    FactorSeed,
    FactorSeedManifest,
    FactorSourceKind,
    QualificationStatus,
    canonical_hash,
    require_hash,
)


FACTOR_DAG_SCHEMA_VERSION = "veriquant.factor-dag/v1"
MAX_SOURCE_BYTES = 250_000

_FORBIDDEN_METADATA_KEYS = {
    "sealed",
    "sealed_data",
    "sealed_metrics",
    "sealed_path",
    "test_metrics",
    "official_metrics",
    "promotion",
    "promotion_decision",
    "risk_approval",
    "target_weights",
    "orders",
    "api_key",
    "token",
    "authorization",
}
_FORBIDDEN_PATH_PARTS = {"sealed", "official_metrics", "promotion", "orders", "broker"}
_UNSAFE_IMPORT_ROOTS = {"subprocess", "importlib", "runpy", "marshal", "pickle"}
_UNSAFE_CALL_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "system",
    "popen",
    "popen2",
    "popen3",
    "popen4",
}
_CONDITIONAL_CALL_NAMES = {"where", "select", "case_when", "conditional"}
_MISSING_OPERATOR_HINTS = {
    "ts_argmax": "ts_argmax",
    "ts_argmin": "ts_argmin",
    "signed_power": "signed_power",
    "decay_linear": "decay_linear",
    "ts_corr": "correlation",
    "correlation": "correlation",
    "ts_cov": "covariance",
    "covariance": "covariance",
    "ts_rank": "time_series_rank",
    "ts_max": "rolling_max",
    "ts_min": "rolling_min",
}
_WINDOW_RE = re.compile(r"(5|10|20|60)$")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _field_node(node_id: str, field_name: str) -> dict[str, Any]:
    return {"id": node_id, "op": "field", "field": field_name}


def _dag(factor_id: str, nodes: Sequence[Mapping[str, Any]], output: str) -> dict[str, Any]:
    return {
        "schema_version": FACTOR_DAG_SCHEMA_VERSION,
        "dag_id": f"library.{factor_id}",
        "nodes": [dict(node) for node in nodes],
        "output_node": output,
    }


def _zscore_field(record: FactorQualificationRecord) -> dict[str, Any]:
    field_name = record.fields[0]
    return _dag(
        record.factor_id,
        (
            _field_node("base", field_name),
            {"id": "score", "op": "zscore_cross_section", "inputs": ["base"]},
        ),
        "score",
    )


def _window(record: FactorQualificationRecord) -> int:
    match = _WINDOW_RE.search(record.factor_id)
    if match is None:
        raise FactorQualificationError("UNSUPPORTED_TEMPLATE", "factor id has no supported bounded window")
    return int(match.group(1))


def _pct_change(record: FactorQualificationRecord) -> dict[str, Any]:
    return _dag(
        record.factor_id,
        (
            _field_node("base", record.fields[0]),
            {"id": "score", "op": "pct_change", "inputs": ["base"], "periods": _window(record)},
        ),
        "score",
    )


def _rolling_ratio(record: FactorQualificationRecord, operator: str) -> dict[str, Any]:
    return _dag(
        record.factor_id,
        (
            _field_node("base", record.fields[0]),
            {"id": "rolling", "op": operator, "inputs": ["base"], "window": _window(record)},
            {"id": "score", "op": "divide_safe", "inputs": ["rolling", "base"], "epsilon": 1e-12},
        ),
        "score",
    )


def _short_term_reversal(record: FactorQualificationRecord) -> dict[str, Any]:
    return _dag(
        record.factor_id,
        (
            _field_node("close", "close"),
            {"id": "return_21", "op": "pct_change", "inputs": ["close"], "periods": 21},
            {"id": "zero", "op": "subtract", "inputs": ["return_21", "return_21"]},
            {"id": "inverse_return", "op": "subtract", "inputs": ["zero", "return_21"]},
            {"id": "score", "op": "zscore_cross_section", "inputs": ["inverse_return"]},
        ),
        "score",
    )


_DIRECT_IDS = {
    "academic_strev",
    "fund_asset_growth",
    "fund_earnings_yield",
    "fund_gross_profitability",
    "fund_roe",
    *(f"qlib158_roc{window}" for window in (5, 10, 20, 60)),
    *(f"qlib158_std{window}" for window in (5, 10, 20, 60)),
    *(f"qlib158_vstd{window}" for window in (5, 10, 20, 60)),
    *(f"qlib158_vma{window}" for window in (5, 10, 20, 60)),
}
_AUDITED_CONDITIONAL_SOURCE_HASHES = {
    "academic_strev": "sha256:480ada115d111ad5c58a6b3fb91cae772ae0859000df77ce0def41c10f3f8cd6",
}


def _fundamental_template(record: FactorQualificationRecord) -> dict[str, Any]:
    if record.factor_id in {"fund_roe", "fund_gross_profitability"}:
        return _zscore_field(record)
    if record.factor_id == "fund_asset_growth":
        return _dag(
            record.factor_id,
            (
                _field_node("base", "fund:asset_growth"),
                {"id": "zero", "op": "subtract", "inputs": ["base", "base"]},
                {"id": "inverse", "op": "subtract", "inputs": ["zero", "base"]},
                {"id": "score", "op": "zscore_cross_section", "inputs": ["inverse"]},
            ),
            "score",
        )
    if record.factor_id == "fund_earnings_yield":
        return _dag(
            record.factor_id,
            (
                _field_node("close", "close"),
                _field_node("net_income", "fund:net_income"),
                _field_node("shares", "fund:shares_diluted"),
                {"id": "market_cap", "op": "multiply", "inputs": ["close", "shares"]},
                {
                    "id": "earnings_yield",
                    "op": "divide_safe",
                    "inputs": ["net_income", "market_cap"],
                    "epsilon": 1e-12,
                },
                {"id": "score", "op": "zscore_cross_section", "inputs": ["earnings_yield"]},
            ),
            "score",
        )
    raise FactorQualificationError("UNSUPPORTED_TEMPLATE", "unknown fundamental template")


def _direct_template(record: FactorQualificationRecord) -> dict[str, Any] | None:
    if record.factor_id not in _DIRECT_IDS:
        return None
    if record.factor_id.startswith("fund_"):
        return _fundamental_template(record)
    if record.factor_id.startswith("qlib158_roc"):
        return _pct_change(record)
    if record.factor_id.startswith("qlib158_std") or record.factor_id.startswith("qlib158_vstd"):
        return _rolling_ratio(record, "rolling_std")
    if record.factor_id.startswith("qlib158_vma"):
        return _rolling_ratio(record, "rolling_mean")
    if record.factor_id == "academic_strev":
        return _short_term_reversal(record)
    return None


def _safe_resolve(root: Path, path: Path) -> tuple[Path, str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FactorQualificationError("INVALID_PATH", "factor source path is unavailable", path=str(path)) from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise FactorQualificationError("PATH_TRAVERSAL", "factor source escapes the configured zoo root") from exc
    if resolved.suffix.lower() != ".py" or len(relative.parts) != 2:
        raise FactorQualificationError("INVALID_PATH", "factor source must be one Python file below a zoo directory")
    if any(part.lower() in _FORBIDDEN_PATH_PARTS for part in relative.parts):
        raise FactorQualificationError("FORBIDDEN_CATALOG_FIELD", "sensitive namespace is forbidden in factor catalog")
    return resolved, relative.as_posix()


def _literal_metadata(tree: ast.AST) -> Mapping[str, Any]:
    assignments: list[ast.AST] = []
    for statement in getattr(tree, "body", ()):  # ast.Module in normal operation
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__alpha_meta__" for target in statement.targets
        ):
            assignments.append(statement.value)
    if len(assignments) != 1:
        raise FactorQualificationError("MALFORMED_DEFINITION", "exactly one literal __alpha_meta__ is required")
    try:
        metadata = ast.literal_eval(assignments[0])
    except (TypeError, ValueError, SyntaxError) as exc:
        raise FactorQualificationError("MALFORMED_DEFINITION", "__alpha_meta__ must be a literal mapping") from exc
    if not isinstance(metadata, Mapping):
        raise FactorQualificationError("MALFORMED_DEFINITION", "__alpha_meta__ must be a mapping")
    lowered_keys = {str(key).lower() for key in metadata}
    forbidden = tuple(sorted(lowered_keys & _FORBIDDEN_METADATA_KEYS))
    if forbidden:
        raise FactorQualificationError(
            "FORBIDDEN_CATALOG_FIELD",
            "catalog metadata contains a sealed, authority, trading, or secret field",
            fields=forbidden,
        )
    return metadata


def _call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return "<dynamic>"


def _static_lint(tree: ast.AST, source_text: str, source_path: str) -> tuple[bool, bool, tuple[str, ...]]:
    unsafe = source_path.lower().endswith("/factor.py") or source_path.lower() == "factor.py"
    conditional = False
    missing: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else [str(node.module or "").split(".")[0]]
            unsafe = unsafe or any(name in _UNSAFE_IMPORT_ROOTS for name in names)
        elif isinstance(node, ast.Lambda):
            unsafe = True
        elif isinstance(node, (ast.If, ast.IfExp)):
            conditional = True
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            unsafe = unsafe or name in _UNSAFE_CALL_NAMES or name == "<dynamic>"
            conditional = conditional or name in _CONDITIONAL_CALL_NAMES
    lowered = source_text.lower()
    if "callback" in lowered or "python_callable" in lowered:
        unsafe = True
    for marker, operator in _MISSING_OPERATOR_HINTS.items():
        if marker in lowered:
            missing.add(operator)
    if conditional:
        missing.add("conditional")
    return unsafe, conditional, tuple(sorted(missing))


def _formula_is_consistent(record: LegacyFactorRecord, metadata: Mapping[str, Any]) -> bool:
    formula = str(metadata.get("formula_latex", "")).lower()
    factor_id = record.factor_id
    if factor_id.startswith("fund_"):
        required_terms = {
            "fund_asset_growth": ("zscore", "assets"),
            "fund_earnings_yield": ("zscore", "net", "income", "close", "shares"),
            "fund_gross_profitability": ("zscore", "gross", "profit", "assets"),
            "fund_roe": ("zscore", "roe"),
        }[factor_id]
        return all(term in formula for term in required_terms)
    if factor_id.startswith("qlib158_roc"):
        return "close" in formula and str(record.warmup) in formula and "- 1" in formula
    if factor_id.startswith("qlib158_std"):
        return "std" in formula and "close" in formula and str(record.warmup) in formula
    if factor_id.startswith("qlib158_vstd"):
        return "std" in formula and "volume" in formula and str(record.warmup) in formula
    if factor_id.startswith("qlib158_vma"):
        return "mean" in formula and "volume" in formula and str(record.warmup) in formula
    if factor_id == "academic_strev":
        return "zscore" in formula and "close" in formula and "21" in formula
    return False


class FactorLibraryCatalogBuilder:
    """Build a stable static read model from legacy factor source artifacts."""

    def __init__(self, zoo_root: Path | str | None = None) -> None:
        if zoo_root is None:
            zoo_root = Path(__file__).resolve().parents[2] / "factors" / "zoo"
        self.zoo_root = Path(zoo_root)

    def build(
        self,
        *,
        paths: Iterable[Path | str] | None = None,
        generated_at: str = "UNSPECIFIED",
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> FactorLibraryCatalog:
        try:
            root = self.zoo_root.resolve(strict=True)
            legacy = LegacyCatalogBuilder(root).build(paths=paths)
        except (CatalogBuildError, OSError) as exc:
            code = getattr(getattr(exc, "code", None), "value", None) or "CATALOG_BUILD_FAILED"
            raise FactorQualificationError(str(code), "legacy catalog source failed static validation") from exc

        expected = dict(expected_source_hashes or {})
        entries: list[FactorQualificationRecord] = []
        observed_keys: set[str] = set()
        for legacy_record in legacy.records:
            resolved, rel_path = _safe_resolve(root, root / legacy_record.source_relpath)
            payload = resolved.read_bytes()
            if len(payload) > MAX_SOURCE_BYTES:
                raise FactorQualificationError("SOURCE_TOO_LARGE", "factor source exceeds the static size cap")
            actual_hash = _sha256_bytes(payload)
            if actual_hash != legacy_record.source_hash:
                raise FactorQualificationError("SOURCE_HASH_MISMATCH", "source changed during catalog build")
            for key in (legacy_record.factor_id, rel_path):
                if key in expected:
                    require_hash(expected[key], f"expected_source_hashes.{key}")
                    observed_keys.add(key)
                    if expected[key] != actual_hash:
                        raise FactorQualificationError(
                            "SOURCE_HASH_MISMATCH",
                            "source hash differs from the frozen expectation",
                            factor_id=legacy_record.factor_id,
                        )
            try:
                source_text = payload.decode("utf-8")
                tree = ast.parse(source_text, filename=rel_path)
            except (UnicodeDecodeError, SyntaxError) as exc:
                raise FactorQualificationError("MALFORMED_DEFINITION", "factor source is not valid UTF-8 Python") from exc
            metadata = _literal_metadata(tree)
            unsafe, conditional, missing = _static_lint(tree, source_text, rel_path)
            audited_conditional = _AUDITED_CONDITIONAL_SOURCE_HASHES.get(legacy_record.factor_id) == actual_hash
            direct = (
                legacy_record.factor_id in _DIRECT_IDS
                and not unsafe
                and (not conditional or audited_conditional)
                and (not missing or (audited_conditional and missing == ("conditional",)))
                and _formula_is_consistent(legacy_record, metadata)
            )
            if direct:
                compatibility = FactorDagCompatibility.DIRECT_FACTOR_DAG
                reasons = ("STATIC_TEMPLATE_AVAILABLE", "REQUIRES_TRUSTED_FACTOR_DAG_COMPILE")
                status = QualificationStatus.METADATA_VALIDATED
            elif unsafe:
                compatibility = FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY
                reasons = ("PYTHON_EXECUTION_SURFACE",)
                status = QualificationStatus.AUXILIARY_ONLY
            elif missing:
                compatibility = FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR
                reasons = tuple(f"MISSING_OPERATOR:{name}" for name in missing)
                status = QualificationStatus.METADATA_VALIDATED
            else:
                compatibility = FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY
                reasons = ("NO_REVIEWED_STATIC_TEMPLATE",)
                status = QualificationStatus.AUXILIARY_ONLY
            entries.append(
                FactorQualificationRecord(
                    factor_id=legacy_record.factor_id,
                    zoo=legacy_record.zoo,
                    theme=legacy_record.themes,
                    market=legacy_record.markets,
                    fields=legacy_record.fields,
                    frequency=legacy_record.frequency,
                    warmup=legacy_record.warmup,
                    source_hash=legacy_record.source_hash,
                    metadata_hash=legacy_record.metadata_hash,
                    family=legacy_record.family,
                    qualification_status=status,
                    compatibility=compatibility,
                    compatibility_reasons=reasons,
                    source_kind=FactorSourceKind.PYTHON_FILE,
                    source_path=rel_path,
                    execution_trust=FactorExecutionTrust.NEVER_TRUSTED_EXECUTION,
                    evidence_authority=EvidenceAuthorityClass.AUXILIARY_ONLY,
                )
            )
        unknown_expectations = set(expected) - observed_keys
        if unknown_expectations:
            raise FactorQualificationError(
                "UNKNOWN_EXPECTED_SOURCE",
                "expected source hash references a factor not included in the build",
                keys=tuple(sorted(unknown_expectations)),
            )
        return FactorLibraryCatalog(entries=tuple(entries), generated_at=generated_at)

    def verify_catalog(self, catalog: FactorLibraryCatalog) -> None:
        if not isinstance(catalog, FactorLibraryCatalog):
            raise FactorQualificationError("INVALID_CATALOG", "catalog type is invalid")
        root = self.zoo_root.resolve(strict=True)
        for record in catalog.entries:
            if record.source_kind is not FactorSourceKind.PYTHON_FILE:
                continue
            resolved, rel_path = _safe_resolve(root, root / record.source_path)
            if rel_path != record.source_path or _sha256_bytes(resolved.read_bytes()) != record.source_hash:
                raise FactorQualificationError(
                    "SOURCE_HASH_MISMATCH", "catalog source no longer matches its content hash", factor_id=record.factor_id
                )

    @staticmethod
    def auxiliary_result_record(
        *,
        factor_id: str,
        source_kind: FactorSourceKind,
        source_hash: str,
        metadata_hash: str,
        zoo: str = "legacy_results",
    ) -> FactorQualificationRecord:
        if source_kind not in {FactorSourceKind.ALPHA_BENCH_RESULT, FactorSourceKind.QLIB_RESULT, FactorSourceKind.CALLABLE}:
            raise FactorQualificationError("INVALID_LEGACY_SOURCE_KIND", "auxiliary result must be bench, Qlib, or callable")
        return FactorQualificationRecord(
            factor_id=factor_id,
            zoo=zoo,
            theme=("legacy",),
            market=("unknown",),
            fields=("close",),
            frequency=("unknown",),
            warmup=0,
            source_hash=source_hash,
            metadata_hash=metadata_hash,
            family="legacy_auxiliary",
            qualification_status=QualificationStatus.AUXILIARY_ONLY,
            compatibility=FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY,
            compatibility_reasons=("LEGACY_RESULT_NEVER_OFFICIAL",),
            source_kind=source_kind,
            execution_trust=FactorExecutionTrust.NEVER_TRUSTED_EXECUTION,
            evidence_authority=EvidenceAuthorityClass.AUXILIARY_ONLY,
        )

    @staticmethod
    def report(catalog: FactorLibraryCatalog) -> FactorQualificationReport:
        buckets = {
            compatibility.value: tuple(
                record.factor_id for record in catalog.entries if record.compatibility is compatibility
            )
            for compatibility in FactorDagCompatibility
        }
        recommended = tuple(
            record.factor_id
            for record in catalog.entries
            if record.compatibility is FactorDagCompatibility.DIRECT_FACTOR_DAG
        )[:24]
        rejected = tuple(
            record.factor_id for record in catalog.entries if record.qualification_status is QualificationStatus.REJECTED
        )
        return FactorQualificationReport(
            catalog=catalog,
            compatibility_buckets=buckets,
            recommended_baseline_factor_ids=recommended,
            rejected_factor_ids=rejected,
        )

    @staticmethod
    def seed_manifest(
        catalog: FactorLibraryCatalog, *, generated_at: str = "UNSPECIFIED", limit: int = 24
    ) -> FactorSeedManifest:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
            raise FactorQualificationError("INVALID_SEED_LIMIT", "seed limit must be an integer from 1 to 24")
        seeds: list[FactorSeed] = []
        for record in catalog.entries:
            if record.compatibility is not FactorDagCompatibility.DIRECT_FACTOR_DAG:
                continue
            template = _direct_template(record)
            if template is None:
                raise FactorQualificationError("MISSING_DAG_TEMPLATE", "direct record has no static DAG template")
            seeds.append(
                FactorSeed(
                    factor_id=record.factor_id,
                    family=record.family,
                    source_hash=record.source_hash,
                    metadata_hash=record.metadata_hash,
                    dag_template_hash=canonical_hash(template),
                    dag_template=template,
                )
            )
            if len(seeds) >= limit:
                break
        if not seeds:
            raise FactorQualificationError("NO_DIRECT_SEEDS", "catalog has no direct FactorDAG candidates")
        return FactorSeedManifest(catalog_hash=catalog.catalog_hash, seeds=tuple(seeds), generated_at=generated_at)


def query_factor_library(
    catalog: FactorLibraryCatalog,
    *,
    factor_ids: Iterable[str] | None = None,
    themes: Iterable[str] | None = None,
    zoos: Iterable[str] | None = None,
    compatibility: FactorDagCompatibility | None = None,
    limit: int = 100,
) -> tuple[FactorQualificationRecord, ...]:
    """Return a stable, read-only metadata projection with no sealed fields."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise FactorQualificationError("INVALID_QUERY_LIMIT", "query limit must be 1-500")
    id_filter = set(factor_ids or ())
    theme_filter = set(themes or ())
    zoo_filter = set(zoos or ())
    if compatibility is not None and not isinstance(compatibility, FactorDagCompatibility):
        try:
            compatibility = FactorDagCompatibility(compatibility)
        except (TypeError, ValueError) as exc:
            raise FactorQualificationError("INVALID_COMPATIBILITY", "unknown compatibility filter") from exc
    selected = []
    for record in catalog.entries:
        if id_filter and record.factor_id not in id_filter:
            continue
        if theme_filter and not theme_filter.intersection(record.theme):
            continue
        if zoo_filter and record.zoo not in zoo_filter:
            continue
        if compatibility is not None and record.compatibility is not compatibility:
            continue
        selected.append(record)
        if len(selected) >= limit:
            break
    return tuple(selected)


__all__ = ["FactorLibraryCatalogBuilder", "query_factor_library"]
