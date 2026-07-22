from __future__ import annotations

from pathlib import Path

from veriquant.integration.formal_research import run_formal_research


def test_formal_research_run_outputs_report_and_workbench(tmp_path: Path) -> None:
    result = run_formal_research(
        question="挖一下最近50天大动量的因子",
        output_root=tmp_path / "runs",
        workbench_root=tmp_path / "workbench",
        seed=7,
        run_id="formal-test-seed7",
    )
    assert result["status"] == "COMPLETED"
    report_path = Path(result["formal_report_markdown"])
    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    assert "正式研究报告" in text
    assert "APPROVED_PROXY_DATA_VENDOR" in text
    assert "RankIC" not in text or "rank_ic" in text
    assert "Desk" not in text
    assert "Router" not in text
    assert (tmp_path / "workbench" / "runs" / "formal-test-seed7" / "overview.json").exists()
