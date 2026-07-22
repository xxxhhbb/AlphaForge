from __future__ import annotations

from veriquant.qlib_migration_coverage import (
    QlibMigrationStatus,
    build_qlib_migration_coverage,
    qlib_migration_coverage_summary,
)


def test_default_scan_classifies_all_461_and_exact_390_gap_cohort() -> None:
    report = build_qlib_migration_coverage()

    assert len(report.records) == 461
    assert sum(report.counts_by_status.values()) == 461
    assert sum(record.legacy_gap_member for record in report.records) == 390
    assert report.legacy_gap_unclassified_count == 0
    assert set(report.counts_by_status) == {
        status.value for status in QlibMigrationStatus
    }
    assert (
        report.execution_eligible_count
        <= report.pit_ready_count
        <= report.field_available_count
    )
    assert report.operator_compatible_count >= report.execution_eligible_count


def test_default_scan_is_stable_and_summary_is_compact() -> None:
    first = build_qlib_migration_coverage()
    second = build_qlib_migration_coverage()
    summary = qlib_migration_coverage_summary()

    assert first.report_hash == second.report_hash == summary["report_hash"]
    assert first.to_dict() == second.to_dict()
    assert summary["total_records"] == 461
    assert summary["classified_records"] == 461
    assert summary["unclassified_records"] == 0
    assert summary["legacy_gap_records"] == 390
    assert summary["legacy_gap_unclassified_records"] == 0
    assert all(
        "impacted_factor_ids" not in item for item in summary["operator_backlog"]
    )


def test_strict_auxiliary_source_is_not_upgraded_by_a_known_factor_name() -> None:
    report = build_qlib_migration_coverage()
    by_id = {record.factor_id: record for record in report.records}

    assert by_id["qlib158_cntd10"].status is QlibMigrationStatus.AUXILIARY_ONLY
    assert by_id["qlib158_cntd10"].execution_eligible is False
    assert by_id["alpha101_001"].status is QlibMigrationStatus.SEMANTICS_REVIEW_REQUIRED
    assert by_id["alpha101_001"].expression_certified is False


def test_operator_backlog_is_impact_sorted_and_maps_pinned_capabilities() -> None:
    report = build_qlib_migration_coverage()
    backlog = report.operator_backlog
    counts = [item.impacted_factor_count for item in backlog]
    by_name = {item.legacy_operator: item for item in backlog}

    assert counts == sorted(counts, reverse=True)
    assert by_name["correlation"].qlib_mapping == ("Corr",)
    assert by_name["signed_power"].qlib_mapping == ("Abs", "Power", "Sign")
    assert by_name["ts_argmax"].qlib_mapping == ("IdxMax",)
    assert by_name["time_series_rank"].qlib_mapping == ("Rank",)
