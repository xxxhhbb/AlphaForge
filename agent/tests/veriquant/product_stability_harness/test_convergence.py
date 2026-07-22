from __future__ import annotations

import json
from pathlib import Path

from veriquant.product_stability_harness.convergence import ARTIFACT_NAMES, BackendProductConvergenceHarness


def test_h0_freezes_baseline_and_all_placeholders_without_false_qualification(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    (workspace / ".autobuild/veriquant").mkdir(parents=True)
    (workspace / ".autobuild/veriquant/state.json").write_text("{}", encoding="utf-8")
    output = workspace / ".autobuild/veriquant/product_stabilization_harness/convergence_v1"
    harness = BackendProductConvergenceHarness(
        workspace=workspace, output_root=output, git_head="a" * 40
    )
    state_path = workspace / ".autobuild/veriquant/state.json"
    first = harness.initialize(state_path=state_path)
    second = harness.initialize(state_path=state_path)
    assert first == second
    assert first["status"] == "IN_PROGRESS" and first["qualified"] is False
    assert all((output / name).is_file() for name in ARTIFACT_NAMES)
    matrix = json.loads((output / "acceptance_matrix.json").read_text(encoding="utf-8"))
    assert matrix["gates"]["H0_BASELINE_AND_TRUSTED_PATH"] == "PASSED"
    assert matrix["gates"]["H1"] == "NOT_RUN"
    assert len((output / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["current_prompt"]["id"] == "backend_product_convergence_harness_v1"
    assert state["current_prompt"]["status"] == "IN_PROGRESS"


def test_h1_h2_requires_real_wiring_and_test_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "agent/src/veriquant/agent_research_product"
    source.mkdir(parents=True)
    (source / "canary.py").write_text(
        "max_sessions: int | None = None\nmax_symbols: int | None = None\n必需数据语义\n",
        encoding="utf-8",
    )
    (source / "terminal.py").write_text(
        'AgentResearchProductCanary(configuration)\n"--smoke"\n"EXPLICIT_SMOKE"\n'
        "task_plan_hash=task_plan.artifact_hash\n",
        encoding="utf-8",
    )
    (workspace / ".autobuild/veriquant").mkdir(parents=True)
    (workspace / ".autobuild/veriquant/state.json").write_text("{}", encoding="utf-8")
    output = workspace / ".autobuild/veriquant/product_stabilization_harness/convergence_v1"
    harness = BackendProductConvergenceHarness(workspace=workspace, output_root=output, git_head="b" * 40)
    harness.initialize()
    result = harness.record_h1_h2(tests_passed=28, test_command="pytest narrow")
    assert result["completed_gates"][-2:] == ["H1", "H2"]
    matrix = json.loads((output / "acceptance_matrix.json").read_text(encoding="utf-8"))
    assert matrix["gates"]["H1"] == matrix["gates"]["H2"] == "PASSED"
