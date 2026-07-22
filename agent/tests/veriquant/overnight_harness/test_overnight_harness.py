from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from src.veriquant.overnight_harness import (
    CommandSpec,
    FailureClass,
    OvernightHarness,
    Phase,
    make_plan,
)


def _registry(tmp_path: Path, *, output: str = "ok", exit_code: int = 0):
    proof = tmp_path / "proof_test.py"
    proof.write_text("def test_machine_proof():\n    assert True\n", encoding="utf-8")
    registry = {}
    mapping = {}
    for phase in Phase:
        command_id = phase.value.lower()
        test_report = None
        if phase in {Phase.NARROW_TEST, Phase.INTEGRATION_TEST, Phase.ACCEPTANCE_GATE} and exit_code == 0:
            test_report = (tmp_path / "harness-artifacts" / "test-reports" / f"{phase.value}.xml").resolve()
            argv = (
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                f"--junitxml={test_report}", str(proof),
            )
        else:
            argv = (sys.executable, "-c", f"import sys; print({output!r}); sys.exit({exit_code})")
        registry[command_id] = CommandSpec(
            command_id=command_id,
            argv=argv,
            cwd=tmp_path.resolve(),
            interpreter=sys.executable,
            expected_test_count=1 if phase in {Phase.NARROW_TEST, Phase.INTEGRATION_TEST, Phase.ACCEPTANCE_GATE} else 0,
            test_report=test_report,
            environment={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        mapping[phase] = (command_id,)
    return registry, make_plan("overnight-test", mapping)


def test_full_run_writes_required_artifacts_and_is_deterministic(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path)
    run_a = OvernightHarness(tmp_path / "harness-artifacts", registry).run(plan)
    run_b = OvernightHarness(tmp_path / "harness-artifacts", registry).run(plan)
    assert run_a.status == "PASSED" and run_a.exit_code == 0
    assert run_a.acceptance_report_hash == run_b.acceptance_report_hash
    required = {
        "run_manifest.json", "resume.json", "events.jsonl", "commands.jsonl",
        "artifact_hashes.json", "acceptance_verdict.json", "acceptance_report.md", "blockers.json",
    }
    assert required <= {path.name for path in (tmp_path / "harness-artifacts").iterdir()}
    assert len(list((tmp_path / "harness-artifacts" / "phase_results").glob("*.json"))) == 9
    records = [json.loads(line) for line in (tmp_path / "harness-artifacts" / "commands.jsonl").read_text().splitlines()]
    assert all({"command", "cwd", "interpreter", "started_at", "completed_at", "exit_code", "stdout_hash", "stderr_hash", "test_count"} <= row.keys() for row in records)
    assert all("stdout" not in row and "stderr" not in row for row in records)


def test_resume_does_not_rerun_committed_phases(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path)
    harness = OvernightHarness(tmp_path / "run", registry)
    first = harness.run(plan)
    before = (tmp_path / "run" / "commands.jsonl").read_text()
    second = harness.run(plan)
    assert first.status == second.status == "PASSED"
    assert (tmp_path / "run" / "commands.jsonl").read_text() == before


def test_manifest_identity_mismatch_fails_security_boundary(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path)
    root = tmp_path / "run"
    assert OvernightHarness(root, registry).run(plan).status == "PASSED"
    changed = dict(registry)
    old = changed["discover"]
    changed["discover"] = CommandSpec(old.command_id, (sys.executable, "-c", "print('changed')"), old.cwd, sys.executable)
    outcome = OvernightHarness(root, changed).run(plan)
    assert outcome.exit_code == 3
    assert outcome.failure_class == FailureClass.TRIAL_IDENTITY_MISMATCH.value


def test_code_defect_without_registered_repair_does_not_blind_retry(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path, output="failed", exit_code=1)
    root = tmp_path / "run"
    outcome = OvernightHarness(root, registry).run(plan)
    assert outcome.exit_code == 1
    records = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["role"] == "CHECK"


def test_registered_repair_is_identity_bound_and_runs_before_retry(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path)
    marker = tmp_path / "repaired.marker"
    discover = CommandSpec(
        "discover",
        (sys.executable, "-c", f"import pathlib,sys; print('check'); sys.exit(0 if pathlib.Path({str(marker)!r}).exists() else 1)"),
        tmp_path.resolve(),
        sys.executable,
    )
    repair = CommandSpec(
        "repair_discover",
        (sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('fixed'); print('repaired')"),
        tmp_path.resolve(),
        sys.executable,
    )
    registry["discover"] = discover
    registry[repair.command_id] = repair
    mapping = {phase: (phase.value.lower(),) for phase in Phase}
    plan = make_plan("repair-test", mapping, {Phase.DISCOVER: (repair.command_id,)})
    root = tmp_path / "repair-run"
    assert OvernightHarness(root, registry).run(plan).status == "PASSED"
    records = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    assert [(row["command_id"], row["role"], row["exit_code"]) for row in records[:3]] == [
        ("discover", "CHECK", 1),
        ("repair_discover", "REPAIR", 0),
        ("discover", "CHECK", 0),
    ]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FailureClass.SECURITY_VIOLATION, 3),
        (FailureClass.SEMANTIC_MISMATCH, 1),
        (FailureClass.SEALED_LEAKAGE, 3),
        (FailureClass.TRIAL_IDENTITY_MISMATCH, 1),
        (FailureClass.PIT_VIOLATION, 3),
        (FailureClass.DATA_PROVENANCE_FAILURE, 1),
        (FailureClass.DEPENDENCY_UNAVAILABLE, 2),
    ],
)
def test_nonrepairable_failures_do_not_retry(tmp_path: Path, failure: FailureClass, expected_code: int) -> None:
    registry, plan = _registry(tmp_path, output="failed", exit_code=1)
    outcome = OvernightHarness(
        tmp_path / failure.value,
        registry,
        failure_classifier=lambda *_: failure,
    ).run(plan)
    assert outcome.exit_code == expected_code
    records = [json.loads(line) for line in (tmp_path / failure.value / "commands.jsonl").read_text().splitlines()]
    assert len(records) == 1


def test_empty_output_and_missing_test_count_fail_closed(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path, output="")
    first = next(iter(registry.values()))
    registry[first.command_id] = CommandSpec(
        first.command_id, (sys.executable, "-c", "pass"), first.cwd, sys.executable
    )
    assert OvernightHarness(tmp_path / "empty", registry).run(plan).status == "FAILED"

    registry, plan = _registry(tmp_path)
    test_spec = registry["narrow_test"]
    registry["narrow_test"] = CommandSpec(
        test_spec.command_id,
        (sys.executable, "-c", "print('no tests collected')"),
        test_spec.cwd,
        test_spec.interpreter,
        expected_test_count=0,
    )
    assert OvernightHarness(tmp_path / "no-tests", registry).run(plan).status == "FAILED"


def test_phase_result_tampering_is_rejected_on_resume(tmp_path: Path) -> None:
    registry, plan = _registry(tmp_path)
    root = tmp_path / "run"
    assert OvernightHarness(root, registry).run(plan).status == "PASSED"
    path = root / "phase_results" / "DISCOVER.json"
    payload = json.loads(path.read_text())
    payload["commands"][0]["stdout_hash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = OvernightHarness(root, registry).run(plan)
    assert outcome.exit_code == 3


@pytest.mark.parametrize("field", ["command_identity", "command", "cwd", "interpreter", "exit_code", "test_count", "test_report_hash", "stable_result_hash"])
def test_phase_metadata_tampering_is_rejected_on_resume(tmp_path: Path, field: str) -> None:
    registry, plan = _registry(tmp_path)
    root = tmp_path / field
    assert OvernightHarness(root, registry).run(plan).status == "PASSED"
    path = root / "phase_results" / "NARROW_TEST.json"
    payload = json.loads(path.read_text())
    target = payload if field == "stable_result_hash" else payload["commands"][0]
    target[field] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert OvernightHarness(root, registry).run(plan).exit_code == 3


def test_rejects_unregistered_command_and_path_escape(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    mapping = {phase: (("not-registered",) if phase is Phase.PLAN else (phase.value.lower(),)) for phase in Phase}
    plan = make_plan("bad", mapping)
    with pytest.raises(ValueError, match="unregistered"):
        OvernightHarness(tmp_path / "run", registry).run(plan)
    from src.veriquant.overnight_harness.store import HarnessStore
    with pytest.raises(ValueError, match="escapes"):
        HarnessStore(tmp_path / "safe").atomic_text("../escape.txt", "x")


def test_subprocess_environment_is_runtime_allowlisted_and_secret_free(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-copied")
    monkeypatch.setenv("TUSHARE_TOKEN", "must-not-be-copied")
    proof = tmp_path / "environment_proof.py"
    proof.write_text(
        "import os\n"
        "def test_environment_boundary():\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    assert 'TUSHARE_TOKEN' not in os.environ\n"
        "    assert os.environ.get('PYTHONNOUSERSITE') == '1'\n",
        encoding="utf-8",
    )
    registry, plan = _registry(tmp_path)
    report = (tmp_path / "harness-artifacts" / "test-reports" / "NARROW_TEST.xml").resolve()
    registry["narrow_test"] = CommandSpec(
        command_id="narrow_test",
        argv=(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            str(proof),
        ),
        cwd=tmp_path.resolve(),
        interpreter=sys.executable,
        expected_test_count=1,
        test_report=report,
        environment={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert OvernightHarness(tmp_path / "harness-artifacts", registry).run(plan).status == "PASSED"


def test_registered_product_plan_binds_junit_receipts_to_selected_output(tmp_path: Path) -> None:
    from src.veriquant.overnight_harness.__main__ import _registered_plan

    repo_root = Path(__file__).resolve().parents[4]
    output = (tmp_path / "custom-product-harness").resolve()
    registry, _plan = _registered_plan(repo_root, output)
    reports = tuple(
        spec.test_report
        for spec in registry.values()
        if spec.test_report is not None
    )
    assert reports
    assert all(output in report.parents for report in reports)
