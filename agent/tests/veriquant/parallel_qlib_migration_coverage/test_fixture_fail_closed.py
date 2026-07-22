from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from veriquant.qlib_migration_coverage import (
    MigrationCoverageError,
    MigrationDataPolicy,
    QlibMigrationCoverageReport,
    QlibMigrationCoverageScanner,
    QlibMigrationStatus,
)


SOURCE = """
__alpha_meta__ = {
    "id": "qlib158_corr5",
    "theme": ["volume"],
    "formula_latex": "ts_corr(close, log(volume + 1), 5)",
    "columns_required": ["close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 5,
}

def compute(panel):
    return ts_corr(panel["close"], panel["volume"], 5)
"""


def fixture_zoo(tmp_path: Path, source: str = SOURCE) -> tuple[Path, Path]:
    root = tmp_path / "zoo"
    directory = root / "qlib158"
    directory.mkdir(parents=True)
    path = directory / "corr5.py"
    path.write_text(source, encoding="utf-8")
    return root, path


def scanner(
    root: Path, *, policy: MigrationDataPolicy | None = None
) -> QlibMigrationCoverageScanner:
    return QlibMigrationCoverageScanner(
        root,
        data_policy=policy,
        expected_total=1,
        expected_legacy_gap_count=1,
    )


def test_fixture_certifies_expression_without_importing_factor_module(
    tmp_path: Path, monkeypatch
) -> None:
    root, path = fixture_zoo(tmp_path)
    original = __import__

    def guarded_import(name, *args, **kwargs):
        if "factors.zoo" in name:
            raise AssertionError("legacy factor module imported")
        return original(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    report = scanner(root).scan(paths=(path,))
    record = report.records[0]

    assert record.factor_id == "qlib158_corr5"
    assert record.status is QlibMigrationStatus.QLIB_DIRECT
    assert record.expression_certified is True
    assert record.execution_eligible is True
    assert record.legacy_gap_member is True


def test_expected_source_hash_tamper_fails_closed(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path)

    with pytest.raises(MigrationCoverageError) as caught:
        scanner(root).scan(
            paths=(path,),
            expected_source_hashes={"qlib158_corr5": "sha256:" + "0" * 64},
        )

    assert "SOURCE_HASH_MISMATCH" in caught.value.code


def test_malformed_source_fails_closed(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path, SOURCE + "\nif (:\n")

    with pytest.raises(MigrationCoverageError):
        scanner(root).scan(paths=(path,))


def test_duplicate_factor_path_fails_closed(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path)

    with pytest.raises(MigrationCoverageError):
        QlibMigrationCoverageScanner(
            root,
            expected_total=2,
            expected_legacy_gap_count=2,
        ).scan(paths=(path, path))


def test_available_but_non_pit_field_is_reported_separately(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path)
    policy = MigrationDataPolicy(
        available_fields=("close", "volume"),
        pit_ready_fields=("close",),
    )

    record = scanner(root, policy=policy).scan(paths=(path,)).records[0]
    assert record.status is QlibMigrationStatus.PIT_METADATA_MISSING
    assert record.missing_fields == ()
    assert record.pit_missing_fields == ("volume",)
    assert record.execution_eligible is False


def test_report_rejects_duplicate_records(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path)
    report = scanner(root).scan(paths=(path,))

    with pytest.raises(MigrationCoverageError) as caught:
        QlibMigrationCoverageReport(
            catalog_hash=report.catalog_hash,
            baseline_qualification_hash=report.baseline_qualification_hash,
            operator_manifest_hash=report.operator_manifest_hash,
            data_policy=report.data_policy,
            records=(report.records[0], replace(report.records[0])),
            operator_backlog=report.operator_backlog,
            expected_total=2,
            expected_legacy_gap_count=2,
        )
    assert caught.value.code == "DUPLICATE_FACTOR_ID"


def test_matching_expected_source_hash_replays_stably(tmp_path: Path) -> None:
    root, path = fixture_zoo(tmp_path)
    expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    service = scanner(root)

    first = service.scan(
        paths=(path,), expected_source_hashes={"qlib158_corr5": expected}
    )
    second = service.scan(
        paths=(path,), expected_source_hashes={"qlib158_corr5": expected}
    )
    assert first.report_hash == second.report_hash
