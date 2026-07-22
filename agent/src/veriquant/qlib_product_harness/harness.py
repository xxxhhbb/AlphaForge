from __future__ import annotations

import json
from pathlib import Path

from ..formula_execution import KNOWN_DIFFERENCE_OPERATOR_NAMES
from ..qlib_expression.api import (
    QlibExpressionError,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    STATIC_QLIB_OPERATOR_MANIFEST,
    certify_qlib_expression,
)
from .models import CapabilityHarnessReport, SemanticTemplateResult


_WINDOWS = (3, 5, 10, 20)


def _operator_expression(name: str, window: int) -> str:
    rolling = {"Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med", "Mad", "Slope", "Rsquare", "Resi", "Rank", "Count", "WMA", "Delta", "IdxMax", "IdxMin"}
    binary = {"Power": "**", "Add": "+", "Sub": "-", "Mul": "*", "Div": "/", "Greater": ">", "Less": "<", "And": "&", "Or": "|", "Gt": ">", "Ge": ">=", "Lt": "<", "Le": "<=", "Eq": "==", "Ne": "!="}
    if name == "ChangeInstrument":
        return 'ChangeInstrument("SH000300",$close)'
    if name == "Mask":
        return 'Mask($close,"SH000300")'
    if name == "Ref":
        return f"Ref($close,{window})"
    if name in rolling:
        return f"{name}($close,{window})"
    if name == "Quantile":
        return f"Quantile($close,{window},0.5)"
    if name == "EMA":
        return f"EMA($close,{window})"
    if name in {"Corr", "Cov"}:
        return f"{name}($close,$volume,{window})"
    if name in {"Abs", "Sign", "Log", "Not"}:
        return f"{name}($close)"
    if name in binary:
        return f"($close{binary[name]}$volume)"
    if name == "If":
        return f"If($close>Mean($close,{window}),$close,$volume)"
    raise KeyError(name)


def build_semantic_templates() -> tuple[tuple[str, str, str], ...]:
    templates: list[tuple[str, str, str]] = []
    trusted = tuple(
        item for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust is not QlibOperatorTrust.EXCLUDED
    )
    for spec in trusted:
        for window in _WINDOWS:
            templates.append((f"{spec.name.lower()}_{window}", spec.name, _operator_expression(spec.name, window)))
    return tuple(templates)


class QlibProductCapabilityHarness:
    def __init__(self, *, catalog_result_path: str | Path | None = None) -> None:
        self.catalog_result_path = Path(catalog_result_path) if catalog_result_path is not None else None

    def run(self) -> CapabilityHarnessReport:
        manifest = STATIC_QLIB_OPERATOR_MANIFEST
        trusted = tuple(item for item in manifest.operators if item.trust is not QlibOperatorTrust.EXCLUDED)
        policy = QlibExpressionPolicy(
            field_roles={"close": QlibFieldRole.FEATURE, "volume": QlibFieldRole.FEATURE},
            allowed_operators=tuple(item.name for item in trusted),
            approved_instruments=("SH000300",),
        )
        results: list[SemanticTemplateResult] = []
        for template_id, operator, expression in build_semantic_templates():
            try:
                certified = certify_qlib_expression(expression, policy=policy, manifest=manifest)
                status = "KNOWN_DIFFERENCE_REVIEW" if operator in KNOWN_DIFFERENCE_OPERATOR_NAMES else "CERTIFIED"
                results.append(SemanticTemplateResult(template_id, operator, expression, status, certified.expression_hash, None))
            except QlibExpressionError as exc:
                results.append(SemanticTemplateResult(template_id, operator, expression, "FAILED", None, exc.code.value))
        catalog_count, catalog_complete = self._catalog_status()
        frozen = tuple(results)
        return CapabilityHarnessReport(
            pinned_operator_count=len(manifest.operators),
            classified_operator_count=len(manifest.operators),
            trusted_operator_count=len(trusted),
            excluded_operator_count=len(manifest.operators) - len(trusted),
            semantic_template_count=len(frozen),
            certified_template_count=sum(item.status == "CERTIFIED" for item in frozen),
            known_difference_template_count=sum(item.status == "KNOWN_DIFFERENCE_REVIEW" for item in frozen),
            failed_template_count=sum(item.status == "FAILED" for item in frozen),
            catalog_record_count=catalog_count,
            catalog_classification_complete=catalog_complete,
            templates=frozen,
        )

    def _catalog_status(self) -> tuple[int | None, bool | None]:
        if self.catalog_result_path is None or not self.catalog_result_path.is_file():
            return None, None
        payload = json.loads(self.catalog_result_path.read_text(encoding="utf-8"))
        status = payload.get("operational_smoke_status")
        hashes = payload.get("artifact_hashes")
        complete = status == "STATIC_461_PROJECTION_REPLAY_PASSED" and isinstance(hashes, dict) and bool(hashes.get("projection_hash"))
        return 461 if complete else None, complete
