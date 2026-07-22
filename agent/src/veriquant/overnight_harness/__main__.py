from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .harness import OvernightHarness
from .models import CommandSpec, FailureClass, Phase, TEST_PHASES, make_plan


def _registered_plan(
    repo_root: Path,
    output: Path,
) -> tuple[dict[str, CommandSpec], object]:
    """Build the only supported product-stability plan.

    Test counts are intentionally not hard-coded.  The Harness still requires
    a fresh, non-empty, zero-failure JUnit receipt, so adding legitimate tests
    does not make the operational profile stale.
    """

    agent_root = (repo_root / "agent").resolve()
    output = output.resolve()
    registry: dict[str, CommandSpec] = {}
    commands: dict[Phase, tuple[str, ...]] = {}
    test_paths = {
        Phase.NARROW_TEST: (
            "tests/veriquant/bitemporal_features",
            "tests/veriquant/dynamic_acquisition",
            "tests/veriquant/dynamic_data_integration",
            "tests/veriquant/integration_research_os_v2/test_server_entrypoint.py",
            "tests/veriquant/research_os_v3",
        ),
        Phase.INTEGRATION_TEST: (
            "tests/veriquant/product_acceptance",
            "tests/veriquant/parallel_formal_report_v2",
            "tests/veriquant/parallel_formal_report_publication",
        ),
        Phase.ACCEPTANCE_GATE: ("tests/veriquant",),
    }
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(agent_root / "src"),
    }
    for phase in Phase:
        command_id = f"product_stability_{phase.value.lower()}"
        report = None
        if phase in TEST_PHASES:
            report = (output / "test_reports" / f"{phase.value}.xml").resolve()
            argv = (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--import-mode=importlib",
                f"--junitxml={report}",
                *test_paths[phase],
            )
            timeout = 900 if phase is Phase.ACCEPTANCE_GATE else 300
        else:
            argv = (
                sys.executable,
                "-m",
                "veriquant.overnight_harness.stability_probe",
                "--phase",
                phase.value,
                "--repo-root",
                str(repo_root),
            )
            timeout = 120
        registry[command_id] = CommandSpec(
            command_id=command_id,
            argv=argv,
            cwd=agent_root,
            interpreter=sys.executable,
            expected_test_count=0,
            test_report=report,
            timeout_seconds=timeout,
            environment=environment,
        )
        commands[phase] = (command_id,)
    return registry, make_plan("alphaforge-product-stability-v1", commands)


def _classify_failure(phase: Phase, *_: object) -> FailureClass:
    if phase is Phase.INDEPENDENT_REVIEW:
        return FailureClass.SECURITY_VIOLATION
    if phase is Phase.NEXT_PHASE:
        return FailureClass.SEMANTIC_MISMATCH
    return FailureClass.CODE_DEFECT


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AlphaForge product stability Harness")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    output = (
        args.output
        or repo_root / ".autobuild" / "veriquant" / "product_stability_harness"
    ).resolve()
    registry, plan = _registered_plan(repo_root, output)
    outcome = OvernightHarness(
        output,
        registry,
        failure_classifier=_classify_failure,
    ).run(plan)  # type: ignore[arg-type]
    print(f"status={outcome.status}")
    print(f"acceptance_report_hash={outcome.acceptance_report_hash}")
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
