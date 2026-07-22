from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping
import xml.etree.ElementTree as ET

from .models import (
    FailureClass,
    HarnessPlan,
    NON_REPAIRABLE_FAILURES,
    Phase,
    PhaseSpec,
    SCHEMA_VERSION,
    SECURITY_FAILURES,
    TEST_PHASES,
    CommandSpec,
    canonical_hash,
    canonical_json,
)
from .store import HarnessStore, file_hash, text_hash


FailureClassifier = Callable[[Phase, CommandSpec, int, str, str], FailureClass]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HarnessOutcome:
    status: str
    exit_code: int
    acceptance_report_hash: str
    completed_phases: tuple[str, ...]
    failure_class: str | None = None


class OvernightHarness:
    def __init__(
        self,
        root: Path,
        registry: Mapping[str, CommandSpec],
        *,
        failure_classifier: FailureClassifier | None = None,
        max_repair_rounds: int = 3,
    ) -> None:
        if max_repair_rounds != 3:
            raise ValueError("the constitution fixes max_repair_rounds at three")
        if len(registry) != len(set(registry)):
            raise ValueError("duplicate command registration")
        for key, spec in registry.items():
            if key != spec.command_id:
                raise ValueError("registry key must match CommandSpec.command_id")
        self.store = HarnessStore(root)
        self.registry = dict(registry)
        self.failure_classifier = failure_classifier or self._default_classifier
        self.max_repair_rounds = max_repair_rounds

    @staticmethod
    def _default_classifier(
        phase: Phase, spec: CommandSpec, exit_code: int, stdout: str, stderr: str
    ) -> FailureClass:
        del phase, spec, exit_code, stdout, stderr
        return FailureClass.CODE_DEFECT

    def run(self, plan: HarnessPlan) -> HarnessOutcome:
        manifest = plan.identity_payload(self.registry)
        manifest["plan_hash"] = plan.identity(self.registry)
        existing = self.store.load_json("run_manifest.json")
        if existing is not None and existing != manifest:
            return self._terminal(plan, "FAILED", 3, FailureClass.TRIAL_IDENTITY_MISMATCH, ())
        if existing is None:
            self.store.atomic_json("run_manifest.json", manifest)

        resume = self.store.load_json("resume.json") or {
            "schema_version": SCHEMA_VERSION,
            "run_id": plan.run_id,
            "plan_hash": manifest["plan_hash"],
            "committed_phases": {},
            "next_phase": Phase.DISCOVER.value,
        }
        if resume.get("plan_hash") != manifest["plan_hash"]:
            return self._terminal(plan, "FAILED", 3, FailureClass.TRIAL_IDENTITY_MISMATCH, ())

        committed = dict(resume.get("committed_phases", {}))
        completed: list[str] = []
        for phase_spec in plan.phases:
            phase_identity = phase_spec.identity(self.registry)
            if phase_spec.phase.value in committed:
                if committed[phase_spec.phase.value] != phase_identity or not self._verify_phase_result(phase_spec):
                    return self._terminal(
                        plan, "FAILED", 3, FailureClass.TRIAL_IDENTITY_MISMATCH, tuple(completed)
                    )
                completed.append(phase_spec.phase.value)
                continue

            result, failure = self._run_phase(phase_spec, phase_identity)
            self.store.atomic_json(f"phase_results/{phase_spec.phase.value}.json", result)
            if failure is not None:
                status = "BLOCKED" if failure is FailureClass.DEPENDENCY_UNAVAILABLE else "FAILED"
                code = 3 if failure in SECURITY_FAILURES else 2 if status == "BLOCKED" else 1
                return self._terminal(plan, status, code, failure, tuple(completed))

            committed[phase_spec.phase.value] = phase_identity
            completed.append(phase_spec.phase.value)
            next_index = len(completed)
            self.store.atomic_json(
                "resume.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": plan.run_id,
                    "plan_hash": manifest["plan_hash"],
                    "committed_phases": committed,
                    "next_phase": PHASE_NAME(next_index),
                },
            )
            self.store.append_jsonl(
                "events.jsonl",
                {
                    "event": "PHASE_COMMITTED",
                    "phase": phase_spec.phase.value,
                    "phase_identity": phase_identity,
                    "occurred_at": _now(),
                },
            )
        return self._terminal(plan, "PASSED", 0, None, tuple(completed))

    def _run_phase(self, phase_spec: PhaseSpec, phase_identity: str) -> tuple[dict[str, object], FailureClass | None]:
        command_results: list[dict[str, object]] = []
        total_tests = 0
        if not phase_spec.command_ids:
            failure = FailureClass.CODE_DEFECT
            return self._phase_payload(phase_spec, phase_identity, command_results, total_tests, failure), failure

        for command_id in phase_spec.command_ids:
            spec = self.registry[command_id]
            final_failure: FailureClass | None = None
            for attempt in range(1, self.max_repair_rounds + 1):
                record = self._execute(phase_spec.phase, spec, attempt, role="CHECK")
                command_results.append(record)
                total_tests += int(record["test_count"])
                if int(record["exit_code"]) == 0:
                    final_failure = None
                    break
                final_failure = self.failure_classifier(
                    phase_spec.phase,
                    spec,
                    int(record["exit_code"]),
                    str(record["stdout"]),
                    str(record["stderr"]),
                )
                if final_failure in NON_REPAIRABLE_FAILURES:
                    break
                if attempt == self.max_repair_rounds or not phase_spec.repair_command_ids:
                    break
                for repair_id in phase_spec.repair_command_ids:
                    repair = self._execute(
                        phase_spec.phase, self.registry[repair_id], attempt, role="REPAIR"
                    )
                    command_results.append(repair)
                    if int(repair["exit_code"]) != 0:
                        final_failure = self.failure_classifier(
                            phase_spec.phase,
                            self.registry[repair_id],
                            int(repair["exit_code"]),
                            str(repair["stdout"]),
                            str(repair["stderr"]),
                        )
                        break
                if final_failure is not FailureClass.CODE_DEFECT:
                    break
            if final_failure is not None:
                return (
                    self._phase_payload(phase_spec, phase_identity, command_results, total_tests, final_failure),
                    final_failure,
                )
        failure = None
        if phase_spec.phase in TEST_PHASES and total_tests <= 0:
            failure = FailureClass.CODE_DEFECT
        if not any(str(row["stdout"]) or str(row["stderr"]) for row in command_results):
            failure = FailureClass.CODE_DEFECT
        return self._phase_payload(phase_spec, phase_identity, command_results, total_tests, failure), failure

    @staticmethod
    def _phase_payload(
        phase_spec: PhaseSpec,
        phase_identity: str,
        commands: list[dict[str, object]],
        test_count: int,
        failure: FailureClass | None,
    ) -> dict[str, object]:
        stable_commands = [
            {
                "command_id": item["command_id"],
                "command_identity": item["command_identity"],
                "attempt": item["attempt"],
                "role": item["role"],
                "exit_code": item["exit_code"],
                "stdout_hash": item["stdout_hash"],
                "stderr_hash": item["stderr_hash"],
                "test_count": item["test_count"],
            }
            for item in commands
        ]
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "phase": phase_spec.phase.value,
            "phase_identity": phase_identity,
            "status": "PASSED" if failure is None else "FAILED",
            "test_count": test_count,
            "commands": [_public_record(item) for item in commands],
            "stable_result_hash": canonical_hash(
                {
                    "phase": phase_spec.phase.value,
                    "phase_identity": phase_identity,
                    "status": "PASSED" if failure is None else "FAILED",
                    "test_count": test_count,
                    "commands": stable_commands,
                    "failure_class": failure.value if failure else None,
                }
            ),
        }
        if failure:
            payload["failure_class"] = failure.value
        return payload

    def _execute(self, phase: Phase, spec: CommandSpec, attempt: int, *, role: str) -> dict[str, object]:
        started = _now()
        # Start from an explicit non-secret Windows runtime allowlist.  A PATH-
        # only environment is not sufficient for reliable Python subprocesses
        # on Windows (temporary files and DLL discovery depend on these), while
        # copying os.environ would leak provider and operator credentials.
        safe_runtime_names = (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
        )
        environment = {
            name: os.environ[name]
            for name in safe_runtime_names
            if name in os.environ
        }
        environment.update({"PYTHONNOUSERSITE": "1", **spec.environment})
        if phase in TEST_PHASES and role == "CHECK":
            self._prepare_test_report(spec)
        try:
            result = subprocess.run(
                list(spec.argv),
                cwd=spec.cwd,
                env=environment,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                check=False,
            )
            stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stderr += "\nCOMMAND_TIMEOUT"
            exit_code = 124
        completed = _now()
        test_report_hash: str | None = None
        observed_test_count = 0
        if phase in TEST_PHASES and role == "CHECK" and exit_code == 0:
            observed_test_count, test_report_hash = self._verified_test_report(spec)
            if observed_test_count <= 0:
                exit_code = 1
                stderr += "\nTEST_PROOF_MISSING_OR_EMPTY"
        record: dict[str, object] = {
            "command_id": spec.command_id,
            "command_identity": spec.identity,
            "command": list(spec.argv),
            "cwd": str(spec.cwd.resolve()),
            "interpreter": spec.interpreter,
            "phase": phase.value,
            "attempt": attempt,
            "role": role,
            "started_at": started,
            "completed_at": completed,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_hash": text_hash(stdout),
            "stderr_hash": text_hash(stderr),
            "test_count": observed_test_count if exit_code == 0 else 0,
            "test_report": str(spec.test_report.resolve()) if spec.test_report else None,
            "test_report_hash": test_report_hash,
        }
        self.store.append_jsonl("commands.jsonl", _public_record(record))
        self.store.append_jsonl(
            "events.jsonl",
            {
                "event": "COMMAND_COMPLETED",
                "phase": phase.value,
                "command_id": spec.command_id,
                "attempt": attempt,
                "exit_code": exit_code,
                "occurred_at": completed,
            },
        )
        return record

    def _prepare_test_report(self, spec: CommandSpec) -> None:
        if spec.test_report is None:
            return
        report = spec.test_report.resolve()
        cwd = spec.cwd.resolve()
        if self.store.root != report and self.store.root not in report.parents and cwd != report and cwd not in report.parents:
            raise ValueError("test report escapes harness root")
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.exists():
            report.unlink()

    def _verified_test_report(self, spec: CommandSpec) -> tuple[int, str | None]:
        if spec.test_report is None:
            return 0, None
        report = spec.test_report.resolve()
        junit_arg = f"--junitxml={report}"
        if not (len(spec.argv) >= 3 and spec.argv[1:3] == ("-m", "pytest") and junit_arg in spec.argv):
            return 0, None
        if not report.is_file():
            return 0, None
        try:
            root = ET.parse(report).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            total = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
            failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
            errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
            skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
        except (OSError, ValueError, ET.ParseError):
            return 0, None
        if total <= 0 or failures or errors or skipped:
            return 0, file_hash(report)
        if spec.expected_test_count and total != spec.expected_test_count:
            return 0, file_hash(report)
        return total, file_hash(report)

    def _verify_phase_result(self, phase_spec: PhaseSpec) -> bool:
        path = self.store.phase_result_path(phase_spec.phase.value)
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("phase_identity") != phase_spec.identity(self.registry) or payload.get("status") != "PASSED":
                return False
            commands = payload.get("commands")
            if not isinstance(commands, list) or not commands:
                return False
            observed_total = 0
            cursor = 0

            def valid_record(record: dict[str, object], spec: CommandSpec, role: str) -> bool:
                nonlocal observed_total
                command_id = record.get("command_id")
                if (
                    command_id != spec.command_id
                    or
                    record.get("command_identity") != spec.identity
                    or record.get("command") != list(spec.argv)
                    or record.get("cwd") != str(spec.cwd.resolve())
                    or record.get("interpreter") != spec.interpreter
                    or record.get("role") != role
                ):
                    return False
                observed = int(record.get("test_count", 0))
                if phase_spec.phase in TEST_PHASES and role == "CHECK" and record.get("exit_code") == 0:
                    report_value = record.get("test_report")
                    if not isinstance(report_value, str):
                        return False
                    report = Path(report_value).resolve()
                    if not report.is_file() or file_hash(report) != record.get("test_report_hash"):
                        return False
                    if spec.test_report is None or report != spec.test_report.resolve():
                        return False
                elif observed != 0 or record.get("test_report_hash") is not None:
                    return False
                observed_total += observed
                return True

            for command_id in phase_spec.command_ids:
                attempts = 0
                while True:
                    if cursor >= len(commands):
                        return False
                    record = commands[cursor]
                    spec = self.registry[command_id]
                    if not valid_record(record, spec, "CHECK"):
                        return False
                    cursor += 1
                    attempts += 1
                    if record.get("exit_code") == 0:
                        break
                    if attempts >= self.max_repair_rounds or not phase_spec.repair_command_ids:
                        return False
                    for repair_id in phase_spec.repair_command_ids:
                        if cursor >= len(commands):
                            return False
                        repair_spec = self.registry[repair_id]
                        if not valid_record(commands[cursor], repair_spec, "REPAIR"):
                            return False
                        if commands[cursor].get("exit_code") != 0:
                            return False
                        cursor += 1
            if cursor != len(commands):
                return False
            if payload.get("test_count") != observed_total:
                return False
            stable_commands = [
                {
                    "command_id": item["command_id"],
                    "command_identity": item["command_identity"],
                    "attempt": item["attempt"],
                    "role": item["role"],
                    "exit_code": item["exit_code"],
                    "stdout_hash": item["stdout_hash"],
                    "stderr_hash": item["stderr_hash"],
                    "test_count": item["test_count"],
                }
                for item in commands
            ]
            expected_stable_hash = canonical_hash(
                {
                    "phase": phase_spec.phase.value,
                    "phase_identity": phase_spec.identity(self.registry),
                    "status": "PASSED",
                    "test_count": observed_total,
                    "commands": stable_commands,
                    "failure_class": None,
                }
            )
            if payload.get("stable_result_hash") != expected_stable_hash:
                return False
            if phase_spec.phase in TEST_PHASES and observed_total <= 0:
                return False
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _terminal(
        self,
        plan: HarnessPlan,
        status: str,
        exit_code: int,
        failure: FailureClass | None,
        completed: tuple[str, ...],
    ) -> HarnessOutcome:
        blockers = [] if failure is None else [{"failure_class": failure.value, "status": status}]
        self.store.atomic_json("blockers.json", {"schema_version": SCHEMA_VERSION, "blockers": blockers})
        artifact_hashes = self.store.artifact_hashes()
        self.store.atomic_json("artifact_hashes.json", {"schema_version": SCHEMA_VERSION, "artifacts": artifact_hashes})
        stable_phases = []
        for phase in PHASE_ORDER:
            path = self.store.phase_result_path(phase.value)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                stable_phases.append(
                    {
                        "phase": phase.value,
                        "phase_identity": payload.get("phase_identity"),
                        "status": payload.get("status"),
                        "test_count": payload.get("test_count", 0),
                        "stable_result_hash": payload.get("stable_result_hash"),
                    }
                )
        verdict_body = {
            "schema_version": SCHEMA_VERSION,
            "run_id": plan.run_id,
            "plan_hash": plan.identity(self.registry),
            "status": status,
            "exit_code": exit_code,
            "failure_class": failure.value if failure else None,
            "phases": stable_phases,
        }
        report_hash = canonical_hash(verdict_body)
        verdict = {**verdict_body, "acceptance_report_hash": report_hash}
        self.store.atomic_json("acceptance_verdict.json", verdict)
        report = self._render_report(verdict)
        self.store.atomic_text("acceptance_report.md", report)
        return HarnessOutcome(status, exit_code, report_hash, completed, failure.value if failure else None)

    @staticmethod
    def _render_report(verdict: Mapping[str, object]) -> str:
        rows = [
            "# Overnight FactorIR Acceptance",
            "",
            f"- Status: `{verdict['status']}`",
            f"- Exit code: `{verdict['exit_code']}`",
            f"- Plan hash: `{verdict['plan_hash']}`",
            f"- Acceptance report hash: `{verdict['acceptance_report_hash']}`",
            "",
            "| Phase | Status | Tests |",
            "| --- | --- | ---: |",
        ]
        for phase in verdict["phases"]:  # type: ignore[index]
            rows.append(f"| {phase['phase']} | {phase['status']} | {phase['test_count']} |")
        return "\n".join(rows) + "\n"


def PHASE_NAME(index: int) -> str | None:
    return PHASE_ORDER[index].value if index < len(PHASE_ORDER) else None


PHASE_ORDER = tuple(Phase)


def _public_record(record: Mapping[str, object]) -> dict[str, object]:
    """Persist hashes and provenance only; raw process streams never enter artifacts."""

    return {key: value for key, value in record.items() if key not in {"stdout", "stderr"}}
