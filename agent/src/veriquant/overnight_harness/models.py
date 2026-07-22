from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence


SCHEMA_VERSION = "alphaforge-product-stability-harness/v1"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Phase(str, Enum):
    DISCOVER = "DISCOVER"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    NARROW_TEST = "NARROW_TEST"
    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    INTEGRATION_TEST = "INTEGRATION_TEST"
    ACCEPTANCE_GATE = "ACCEPTANCE_GATE"
    COMMIT_CHECKPOINT = "COMMIT_CHECKPOINT"
    NEXT_PHASE = "NEXT_PHASE"


PHASE_ORDER: tuple[Phase, ...] = tuple(Phase)
TEST_PHASES = frozenset({Phase.NARROW_TEST, Phase.INTEGRATION_TEST, Phase.ACCEPTANCE_GATE})


class FailureClass(str, Enum):
    CODE_DEFECT = "CODE_DEFECT"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    SEMANTIC_MISMATCH = "SEMANTIC_MISMATCH"
    SEALED_LEAKAGE = "SEALED_LEAKAGE"
    TRIAL_IDENTITY_MISMATCH = "TRIAL_IDENTITY_MISMATCH"
    PIT_VIOLATION = "PIT_VIOLATION"
    DATA_PROVENANCE_FAILURE = "DATA_PROVENANCE_FAILURE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


SECURITY_FAILURES = frozenset(
    {
        FailureClass.SECURITY_VIOLATION,
        FailureClass.SEALED_LEAKAGE,
        FailureClass.PIT_VIOLATION,
    }
)
NON_REPAIRABLE_FAILURES = frozenset(set(FailureClass) - {FailureClass.CODE_DEFECT})


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    argv: tuple[str, ...]
    cwd: Path
    interpreter: str
    expected_test_count: int = 0
    test_report: Path | None = None
    timeout_seconds: int = 300
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command_id or not self.argv or not self.interpreter:
            raise ValueError("command_id, argv and interpreter are required")
        if self.argv[0] != self.interpreter:
            raise ValueError("argv[0] must equal the registered interpreter")
        if not self.cwd.is_absolute():
            raise ValueError("CommandSpec.cwd must be absolute")
        if self.timeout_seconds <= 0 or self.expected_test_count < 0:
            raise ValueError("invalid command limits")
        if self.test_report is not None and not self.test_report.is_absolute():
            raise ValueError("CommandSpec.test_report must be absolute")

    def identity_payload(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "cwd": str(self.cwd.resolve()),
            "interpreter": self.interpreter,
            "expected_test_count": self.expected_test_count,
            "test_report": str(self.test_report.resolve()) if self.test_report else None,
            "timeout_seconds": self.timeout_seconds,
            "environment": dict(sorted(self.environment.items())),
        }

    @property
    def identity(self) -> str:
        return canonical_hash(self.identity_payload())


@dataclass(frozen=True)
class PhaseSpec:
    phase: Phase
    command_ids: tuple[str, ...]
    repair_command_ids: tuple[str, ...] = ()
    input_hashes: Mapping[str, str] = field(default_factory=dict)

    def identity(self, registry: Mapping[str, CommandSpec]) -> str:
        commands = []
        for command_id in self.command_ids:
            if command_id not in registry:
                raise ValueError(f"unregistered CommandSpec: {command_id}")
            commands.append(registry[command_id].identity)
        repairs = []
        for command_id in self.repair_command_ids:
            if command_id not in registry:
                raise ValueError(f"unregistered repair CommandSpec: {command_id}")
            repairs.append(registry[command_id].identity)
        return canonical_hash(
            {
                "phase": self.phase.value,
                "commands": commands,
                "repairs": repairs,
                "input_hashes": dict(sorted(self.input_hashes.items())),
            }
        )


@dataclass(frozen=True)
class HarnessPlan:
    run_id: str
    phases: tuple[PhaseSpec, ...]

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        actual = tuple(item.phase for item in self.phases)
        if actual != PHASE_ORDER:
            raise ValueError("plan must contain the exact ordered overnight state machine")

    def identity_payload(self, registry: Mapping[str, CommandSpec]) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "phases": [
                {"phase": item.phase.value, "phase_identity": item.identity(registry)}
                for item in self.phases
            ],
        }

    def identity(self, registry: Mapping[str, CommandSpec]) -> str:
        return canonical_hash(self.identity_payload(registry))


def make_plan(
    run_id: str,
    commands_by_phase: Mapping[Phase, Sequence[str]],
    repairs_by_phase: Mapping[Phase, Sequence[str]] | None = None,
) -> HarnessPlan:
    repairs = repairs_by_phase or {}
    return HarnessPlan(
        run_id=run_id,
        phases=tuple(
            PhaseSpec(
                phase=phase,
                command_ids=tuple(commands_by_phase.get(phase, ())),
                repair_command_ids=tuple(repairs.get(phase, ())),
            )
            for phase in PHASE_ORDER
        ),
    )
