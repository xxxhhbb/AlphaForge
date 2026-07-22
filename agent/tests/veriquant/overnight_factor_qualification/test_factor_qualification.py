from __future__ import annotations

from pathlib import Path

import pytest

from veriquant.factor_qualification import (
    FactorCompatibilityStatus,
    FactorQualificationService,
)
from veriquant.factor_library import CatalogBuildError
from veriquant.factors import FactorOperator


def _write_factor(root: Path, zoo: str, name: str, factor_id: str, body: str = "return panel['close']") -> Path:
    directory = root / zoo
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(
        "\n".join(
            [
                "__alpha_meta__ = {",
                f"    'id': '{factor_id}',",
                "    'theme': ['test'],",
                "    'columns_required': ['close'],",
                "    'universe': ['equity_cn'],",
                "    'frequency': ['1d'],",
                "    'min_warmup_bars': 5,",
                "}",
                "def compute(panel):",
                f"    {body}",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_default_scan_counts_461_and_selects_bounded_templates():
    report = FactorQualificationService().qualify()
    assert report.total_records == 461
    assert 12 <= len(report.qualified_templates) <= 24
    assert report.counts_by_status[FactorCompatibilityStatus.DIRECT_DAG.value] == len(report.qualified_templates)


def test_corrupted_file_fails_closed(tmp_path: Path):
    bad = tmp_path / "toy" / "bad.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("__alpha_meta__ = {'id':", encoding="utf-8")
    with pytest.raises(CatalogBuildError):
        FactorQualificationService(tmp_path).qualify()


def test_duplicate_id_is_rejected(tmp_path: Path):
    _write_factor(tmp_path, "toy", "one", "toy_dup")
    _write_factor(tmp_path, "toy", "two", "toy_dup")
    with pytest.raises(CatalogBuildError):
        FactorQualificationService(tmp_path).qualify()


def test_path_traversal_is_rejected(tmp_path: Path):
    outside = _write_factor(tmp_path.parent, "outside", "one", "toy_outside")
    with pytest.raises(CatalogBuildError):
        FactorQualificationService(tmp_path).qualify(paths=[outside])


def test_report_hash_is_stable_and_changes_with_source_hash(tmp_path: Path):
    path = _write_factor(tmp_path, "toy", "one", "toy_one")
    first = FactorQualificationService(tmp_path).qualify()
    second = FactorQualificationService(tmp_path).qualify()
    assert first.report_hash == second.report_hash
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    third = FactorQualificationService(tmp_path).qualify()
    assert third.report_hash != first.report_hash


def test_unknown_operator_is_needs_deterministic_operator(tmp_path: Path):
    _write_factor(tmp_path, "toy", "one", "toy_unknown", "return ts_argmax(panel['close'], 5)")
    report = FactorQualificationService(tmp_path).qualify()
    assert report.records[0].status is FactorCompatibilityStatus.NEEDS_DETERMINISTIC_OPERATOR
    assert "ts_argmax" in report.records[0].missing_operators


def test_python_execution_surface_is_rejected_or_auxiliary(tmp_path: Path):
    _write_factor(tmp_path, "toy", "one", "toy_exec", "exec(\"print(1)\")")
    report = FactorQualificationService(tmp_path).qualify()
    assert report.records[0].status in {
        FactorCompatibilityStatus.REJECTED_SOURCE,
        FactorCompatibilityStatus.AUXILIARY_ONLY,
    }


def test_qualified_templates_use_only_current_factor_dag_operators():
    allowed = {operator.value for operator in FactorOperator}
    report = FactorQualificationService().qualify()
    assert report.qualified_templates
    for template in report.qualified_templates:
        ops = {node["op"] for node in template.dag_payload["nodes"]}
        assert ops <= allowed


def test_no_old_factor_file_execution_path():
    service = FactorQualificationService()
    assert not hasattr(service, "execute")
    assert not hasattr(service, "run_factor_file")
    assert not hasattr(service, "compute")
