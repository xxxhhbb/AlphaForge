from __future__ import annotations

import json

from src.veriquant.demo import main, run_offline_demo


def test_offline_demo_executes_all_gates_and_writes_both_reports(tmp_path) -> None:
    output = tmp_path / "demo"
    report = run_offline_demo(output_dir=output, seed=7)
    replay = run_offline_demo(output_dir=tmp_path / "demo-replay", seed=7)
    other_seed = run_offline_demo(output_dir=tmp_path / "demo-seed-8", seed=8)

    assert report["status"] == "PASSED"
    assert all(report["invariants"].values())
    assert report["research"]["validated_branches"]
    assert report["research"]["reject_or_revise_branches"]
    assert report["research"]["official_evaluation_count"] == 6
    assert report["evolution"]["state"] == "DRAFT"
    assert report["evolution"]["trusted_for_alpha"] is False
    assert abs(report["portfolio"]["net_exposure"]) <= 2e-7
    assert report["risk"]["verdict"] in {"APPROVE", "SCALE_DOWN"}
    assert report["paper"]["reconciliation_status"] == "RECONCILED"
    assert replay["report_hash"] == report["report_hash"]
    assert replay["research"]["selected_evidence_hash"] == report["research"]["selected_evidence_hash"]
    assert other_seed["pit"]["dataset_hash"] != report["pit"]["dataset_hash"]
    assert replay["paper"]["attribution_hash"] == report["paper"]["attribution_hash"]

    machine = json.loads((output / "demo_report.json").read_text(encoding="utf-8"))
    human = (output / "demo_report.md").read_text(encoding="utf-8")
    assert machine == report
    assert "Evidence Court outcomes" in human
    assert (output / "research_runs" / "prompt09-seed-7" / "trial_ledger.sqlite3").is_file()
    assert (output / "evolution" / "trial-ledger.sqlite3").is_file()
    assert (output / "paper" / "journal.sqlite3").is_file()


def test_cli_fails_closed_without_offline_flag(tmp_path, capsys) -> None:
    code = main(["--seed", "7", "--output-dir", str(tmp_path / "forbidden")])
    captured = capsys.readouterr()

    assert code == 1
    assert '"status": "FAILED"' in captured.err
    assert not (tmp_path / "forbidden").exists()
