from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
from pathlib import Path

import pytest

from veriquant.qlib_formula_migration import (
    FormulaMigrationError,
    FormulaMigrationFailureCode,
    FormulaMigrationStatus,
    QlibFormulaMigrationScanner,
    StaticComputeTranslator,
    build_qlib_formula_migration,
)


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _source(factor_id: str, body: str) -> str:
    return f'''from __future__ import annotations
import pandas as pd

__alpha_meta__ = {{
    "id": "{factor_id}",
    "theme": ["momentum"],
    "formula_latex": "static test formula",
    "columns_required": ["close"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 5,
}}

def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
{body}
'''


def _fixture(tmp_path: Path, factor_id: str, body: str) -> tuple[Path, Path]:
    root = tmp_path / "zoo"
    zoo = root / "testzoo"
    zoo.mkdir(parents=True)
    path = zoo / f"{factor_id.split('_', 1)[1]}.py"
    path.write_text(_source(factor_id, body), encoding="utf-8")
    return root, path


def test_real_inventory_is_closed_and_reports_actual_new_certifications() -> None:
    report = build_qlib_formula_migration()

    assert len(report.records) == 461
    assert report.summary()["unclassified_records"] == 0
    assert report.summary()["baseline_certified_count"] == 81
    assert report.summary()["certified_count"] == 63
    assert report.summary()["newly_certified_count"] == 31
    assert report.summary()["combined_certified_count"] == 112
    assert "alpha101_012" in report.newly_certified_ids
    assert "gtja191_189" in report.newly_certified_ids
    assert sum(report.counts_by_status.values()) == 461


def test_real_whole_formula_identity_binds_source_formula_and_program() -> None:
    report = build_qlib_formula_migration()
    record = next(item for item in report.records if item.factor_id == "alpha101_012")

    assert record.status is FormulaMigrationStatus.CERTIFIED_QLIB_EXPRESSION
    assert record.canonical_expression
    assert record.expression_hash and record.operator_manifest_hash
    assert record.required_fields == ("close", "volume")
    assert record.maximum_lookback > 0
    assert record.source_hash.startswith("sha256:")
    assert record.formula_hash.startswith("sha256:")
    assert record.program_hash.startswith("sha256:")


def test_identical_inventory_has_identical_report_and_program_hashes() -> None:
    first = build_qlib_formula_migration()
    second = build_qlib_formula_migration()

    assert first.report_hash == second.report_hash
    assert [record.program_hash for record in first.records] == [
        record.program_hash for record in second.records
    ]


def test_unknown_call_is_a_typed_review_not_an_executable_program() -> None:
    tree = ast.parse(
        _source("test_unknown", '    c = panel["close"]\n    return mystery(c)')
    )

    with pytest.raises(FormulaMigrationError) as captured:
        StaticComputeTranslator().translate(tree)

    assert captured.value.code is FormulaMigrationFailureCode.UNKNOWN_CALL


def test_negative_shift_is_a_typed_future_reference() -> None:
    tree = ast.parse(
        _source("test_future", '    c = panel["close"]\n    return c.shift(-1)')
    )

    with pytest.raises(FormulaMigrationError) as captured:
        StaticComputeTranslator().translate(tree)

    assert captured.value.code is FormulaMigrationFailureCode.FUTURE_REFERENCE


def test_malformed_source_fails_closed(tmp_path: Path) -> None:
    root, path = _fixture(
        tmp_path,
        "test_broken",
        '    c = panel["close"]\n    return c',
    )
    path.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(FormulaMigrationError) as captured:
        QlibFormulaMigrationScanner(root, expected_total=1).scan()

    assert captured.value.code is FormulaMigrationFailureCode.MALFORMED_SOURCE


def test_path_traversal_is_rejected_before_source_analysis(tmp_path: Path) -> None:
    root, _ = _fixture(
        tmp_path,
        "test_inside",
        '    c = panel["close"]\n    return c',
    )
    outside = tmp_path / "outside.py"
    outside.write_text(
        _source("test_outside", '    c = panel["close"]\n    return c'),
        encoding="utf-8",
    )

    with pytest.raises(FormulaMigrationError) as captured:
        QlibFormulaMigrationScanner(root, expected_total=1).scan(paths=[outside])

    assert captured.value.code is FormulaMigrationFailureCode.PATH_TRAVERSAL


def test_expected_source_hash_tampering_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(
        tmp_path,
        "test_hash",
        '    c = panel["close"]\n    return c.pct_change(2)',
    )

    with pytest.raises(FormulaMigrationError) as captured:
        QlibFormulaMigrationScanner(root, expected_total=1).scan(
            expected_source_hashes={"test_hash": "sha256:" + "0" * 64}
        )

    assert captured.value.code is FormulaMigrationFailureCode.SOURCE_HASH_MISMATCH


def test_program_hash_tampering_is_rejected() -> None:
    report = build_qlib_formula_migration()
    record = next(item for item in report.records if item.certified)

    with pytest.raises(FormulaMigrationError) as captured:
        dataclasses.replace(record, program_hash="sha256:" + "0" * 64)

    assert captured.value.code is FormulaMigrationFailureCode.SOURCE_HASH_MISMATCH


def test_translator_source_has_no_dynamic_execution_or_factor_import() -> None:
    import veriquant.qlib_formula_migration.scanner as scanner
    import veriquant.qlib_formula_migration.translator as translator

    source = inspect.getsource(scanner) + inspect.getsource(translator)
    assert "importlib" not in source
    assert "__import__" not in source
    assert "eval(" not in source.replace("literal_eval(", "")
    assert "exec(" not in source
