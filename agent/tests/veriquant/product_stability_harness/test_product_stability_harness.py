from __future__ import annotations

from pathlib import Path

from veriquant.product_stability_harness import ProductStabilityHarness


REPO_ROOT = Path(__file__).resolve().parents[4]
STRICT_DATA = REPO_ROOT / ".autobuild/veriquant/product_acceptance/repair24/real_capture_v2/data"


def test_product_stability_harness_runs_customer_report_spine(tmp_path):
    report = ProductStabilityHarness(
        output_root=tmp_path / "product",
        data_root=STRICT_DATA,
        dotenv_path=tmp_path / "missing.env",
        questions=(
            "挖掘过去50日动量对未来5日收益的预测能力",
            "研究过去20日反转因子是否仍然有效",
        ),
    ).run()

    assert report.status == "PASSED"
    assert len(report.scenarios) == 2
    assert all(item.report_hash and item.report_path for item in report.scenarios)
    assert all(1 <= len(item.selected_specialists) <= 6 for item in report.scenarios)
    assert report.report_hash == ProductStabilityHarness(
        output_root=tmp_path / "product",
        data_root=STRICT_DATA,
        dotenv_path=tmp_path / "missing.env",
        questions=(
            "挖掘过去50日动量对未来5日收益的预测能力",
            "研究过去20日反转因子是否仍然有效",
        ),
    ).run().report_hash


def test_missing_data_fails_closed(tmp_path):
    report = ProductStabilityHarness(
        output_root=tmp_path / "product",
        data_root=tmp_path / "missing-data",
        dotenv_path=tmp_path / "missing.env",
    ).run()

    assert report.status == "FAILED"
    assert [item.code for item in report.findings] == ["DATA_UNAVAILABLE"]
