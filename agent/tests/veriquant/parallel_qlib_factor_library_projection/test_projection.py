from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from veriquant.factor_library import build_factor_library_catalog
from veriquant.qlib_factor_library_projection import (
    ProjectionError,
    ProjectionFailureCode,
    QlibExecutionQualification,
    QlibFactorLibraryProjectionBuilder,
    QlibFactorLibraryProjectionRepository,
)
from veriquant.qlib_formula_migration import build_qlib_formula_migration
from veriquant.qlib_migration_coverage import build_qlib_migration_coverage


@pytest.fixture(scope="session")
def artifacts():
    repo = Path(__file__).resolve().parents[4]
    parity = json.loads(
        (
            repo
            / ".autobuild"
            / "veriquant"
            / "parallel"
            / "qlib_semantic_parity"
            / "real_smoke_result.json"
        ).read_text(encoding="utf-8")
    )
    return (
        build_factor_library_catalog(),
        build_qlib_migration_coverage(),
        build_qlib_formula_migration(),
        parity,
    )


@pytest.fixture(scope="session")
def projection(artifacts):
    catalog, coverage, formula, parity = artifacts
    return QlibFactorLibraryProjectionBuilder().build_from_artifacts(
        catalog=catalog,
        coverage=coverage,
        formula=formula,
        parity_snapshot=parity,
    )


def test_real_461_projection_has_replayable_source_and_exclusive_counts(projection):
    summary = projection.summary()
    assert summary["total_records"] == 461
    assert summary["source_counts"] == {
        "auxiliary_only": 50,
        "catalog_total": 461,
        "coverage_execution_eligible": 81,
        "formula_certified": 63,
        "formula_semantics_review": 348,
        "operator_compatible": 407,
        "parity_downgraded_from_static_union": 0,
        "static_certified_union": 112,
    }
    assert summary["counts_by_qualification"] == {
        "AUXILIARY_ONLY": 50,
        "DATA_FIELD_MISSING": 23,
        "EXECUTION_READY": 112,
        "PIT_METADATA_MISSING": 0,
        "SEMANTICS_REVIEW_REQUIRED": 276,
    }
    assert summary["review_or_gap_count"] == 299


def test_operator_compatible_is_not_execution_ready(projection):
    compatible = [record for record in projection.records if record.operator_compatible]
    ready = [
        record
        for record in projection.records
        if record.execution_qualification is QlibExecutionQualification.EXECUTION_READY
    ]
    assert len(compatible) == 407
    assert len(ready) == 112
    record = next(item for item in projection.records if item.factor_id == "academic_bab")
    assert record.operator_compatible is True
    assert record.execution_qualification is QlibExecutionQualification.SEMANTICS_REVIEW_REQUIRED
    assert "OPERATOR_COMPATIBLE_NOT_EXECUTION_READY" in record.reason_codes


@pytest.mark.parametrize("operator", ["IdxMax", "IdxMin", "If", "WMA"])
def test_known_difference_operator_requires_explicit_safe_adapter_for_execution(projection, operator):
    affected = [record for record in projection.records if operator in record.operators]
    assert affected
    safe_name = {"IdxMax": "SafeIdxMax", "IdxMin": "SafeIdxMin", "If": "SafeIf", "WMA": "SafeWMA"}[operator]
    for record in affected:
        if record.execution_qualification is QlibExecutionQualification.EXECUTION_READY:
            assert safe_name in record.semantic_adapters
            assert record.program_origin.value == "FACTOR_IR_V2"
            assert all(
                getattr(record, name).startswith("sha256:")
                for name in ("factor_ir_hash", "factor_ir_policy_hash", "runtime_hash", "adapter_hash", "pit_requirement_hash")
            )
    assert all(f"PARITY_KNOWN_DIFFERENCE:{operator}" in record.reason_codes for record in affected)
    assert all(
        any(item.operator == operator and item.status == "KNOWN_DIFFERENCE" for item in record.parity)
        for record in affected
    )


def test_formula_certified_if_record_is_rewritten_to_bound_safe_adapter(projection):
    record = next(item for item in projection.records if item.factor_id == "alpha101_053")
    assert record.formula_status == "CERTIFIED_QLIB_EXPRESSION"
    assert record.static_union_member is True
    assert record.execution_qualification is QlibExecutionQualification.EXECUTION_READY
    assert "If" in record.operators
    assert "PARITY_KNOWN_DIFFERENCE:If" in record.reason_codes
    assert "SafeIf" in record.semantic_adapters
    assert record.factor_ir_hash is not None


def test_equivalent_operator_and_controlled_postprocess_can_remain_ready(projection):
    corr = next(item for item in projection.records if item.factor_id == "qlib158_corr10")
    assert corr.execution_qualification is QlibExecutionQualification.EXECUTION_READY
    assert any(item.operator == "Corr" and item.status == "CERTIFIED_EQUIVALENT" for item in corr.parity)
    assert corr.program_hash and corr.expression_hash and corr.maximum_lookback is not None

    strev = next(item for item in projection.records if item.factor_id == "academic_strev")
    assert strev.execution_qualification is QlibExecutionQualification.EXECUTION_READY
    assert strev.postprocess_steps == ("zscore_cross_section",)


def test_stable_replay_and_per_record_provenance(artifacts, projection):
    catalog, coverage, formula, parity = artifacts
    replay = QlibFactorLibraryProjectionBuilder().build_from_artifacts(
        catalog=catalog,
        coverage=coverage,
        formula=formula,
        parity_snapshot=parity,
    )
    assert replay.projection_hash == projection.projection_hash
    assert [item.record_hash for item in replay.records] == [item.record_hash for item in projection.records]
    first = projection.records[0]
    assert first.catalog_hash == projection.catalog_hash
    assert first.coverage_report_hash == projection.coverage_report_hash
    assert first.formula_report_hash == projection.formula_report_hash


def test_report_hash_chain_mismatch_fails_closed(artifacts):
    catalog, coverage, formula, parity = artifacts
    tampered = replace(coverage, catalog_hash="sha256:" + "0" * 64)
    with pytest.raises(ProjectionError) as caught:
        QlibFactorLibraryProjectionBuilder().build_from_artifacts(
            catalog=catalog,
            coverage=tampered,
            formula=formula,
            parity_snapshot=parity,
        )
    assert caught.value.code is ProjectionFailureCode.INPUT_IDENTITY_MISMATCH


def test_measured_parity_tampering_fails_closed(artifacts):
    catalog, coverage, formula, parity = artifacts
    tampered = dict(parity)
    tampered["report_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ProjectionError) as caught:
        QlibFactorLibraryProjectionBuilder().build_from_artifacts(
            catalog=catalog,
            coverage=coverage,
            formula=formula,
            parity_snapshot=tampered,
        )
    assert caught.value.code is ProjectionFailureCode.PARITY_EVIDENCE_INVALID


def test_read_only_id_query_and_cursor_pagination(projection):
    repository = QlibFactorLibraryProjectionRepository(projection)
    first = repository.page(page_size=7)
    second = repository.page(page_size=7, cursor=first.next_cursor)
    assert first.total_matching == 461
    assert first.next_cursor is not None
    assert {item.factor_id for item in first.records}.isdisjoint(
        item.factor_id for item in second.records
    )
    assert repository.get_by_id(first.records[0].factor_id) is first.records[0]

    ready = repository.page(
        page_size=100,
        qualification=QlibExecutionQualification.EXECUTION_READY,
    )
    assert ready.total_matching == 112
    assert len(ready.records) == 100
    assert ready.next_cursor is not None
    tail = repository.page(
        page_size=100,
        cursor=ready.next_cursor,
        qualification=QlibExecutionQualification.EXECUTION_READY,
    )
    assert len(tail.records) == 12
    assert tail.next_cursor is None


def test_unknown_id_stale_cursor_and_invalid_page_fail_closed(projection):
    repository = QlibFactorLibraryProjectionRepository(projection)
    with pytest.raises(ProjectionError) as missing:
        repository.get_by_id("factor_does_not_exist")
    assert missing.value.code is ProjectionFailureCode.FACTOR_NOT_FOUND
    with pytest.raises(ProjectionError) as stale:
        repository.page(cursor="0" * 64 + ":academic_bab")
    assert stale.value.code is ProjectionFailureCode.STALE_CURSOR
    with pytest.raises(ProjectionError) as invalid:
        repository.page(page_size=0)
    assert invalid.value.code is ProjectionFailureCode.INVALID_QUERY


def test_projection_package_has_no_legacy_execution_surface():
    root = Path(__file__).resolve().parents[3] / "src" / "veriquant" / "qlib_factor_library_projection"
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))
    assert "importlib" not in source
    assert "__import__(" not in source
    assert "eval(" not in source
    assert "exec(" not in source
    assert "factor.py" not in source
