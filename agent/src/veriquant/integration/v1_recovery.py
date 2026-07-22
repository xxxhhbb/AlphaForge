"""Recovery adapters for content-addressed A-to-F integration stages."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from veriquant.contracts import FailureCode
from veriquant.contracts.base import require_hash, require_identifier
from veriquant.data import canonical_content_hash
from veriquant.run_recovery.api import (
    RecoveryInterrupted,
    StageExecutionResult,
    StageIdentity,
    StageTask,
)


@dataclass(frozen=True, kw_only=True)
class IntegrationStageSpec:
    run_id: str
    stage_id: str
    ordinal: int
    input_hash: str
    config_hash: str
    code_hash: str
    policy_hash: str
    expected_artifact_hash: str
    depends_on_stage_ids: tuple[str, ...] = ()
    branch_id: str = "v1"
    provider_id: str = "v1-stage-provider"

    def __post_init__(self) -> None:
        for field_name in ("run_id", "stage_id", "branch_id", "provider_id"):
            require_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "input_hash",
            "config_hash",
            "code_hash",
            "policy_hash",
            "expected_artifact_hash",
        ):
            require_hash(getattr(self, field_name), field_name)


@dataclass(frozen=True, kw_only=True)
class StageArtifact:
    artifact_hash: str
    payload_hash: str
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        require_hash(self.artifact_hash, "artifact_hash")
        require_hash(self.payload_hash, "payload_hash")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class V1RecoveryPlanBuilder:
    """Build StageTasks without executing or owning financial truth."""

    @staticmethod
    def build(specs: Sequence[IntegrationStageSpec]) -> tuple[StageTask, ...]:
        ordered = tuple(sorted(specs, key=lambda item: (item.ordinal, item.stage_id)))
        if not ordered:
            raise ValueError("stage specs are required")
        identities = {
            spec.stage_id: StageIdentity(
                run_id=spec.run_id,
                branch_id=spec.branch_id,
                stage_id=spec.stage_id,
                ordinal=spec.ordinal,
                input_hash=spec.input_hash,
                config_hash=spec.config_hash,
                code_hash=spec.code_hash,
                policy_hash=spec.policy_hash,
                expected_artifact_hash=spec.expected_artifact_hash,
            )
            for spec in ordered
        }
        if len(identities) != len(ordered):
            raise ValueError("stage ids must be unique")
        tasks: list[StageTask] = []
        for spec in ordered:
            unknown = set(spec.depends_on_stage_ids) - set(identities)
            if unknown:
                raise ValueError(f"unknown stage dependencies: {sorted(unknown)}")
            tasks.append(
                StageTask(
                    identity=identities[spec.stage_id],
                    provider_id=spec.provider_id,
                    depends_on=tuple(identities[item].artifact_hash for item in spec.depends_on_stage_ids),
                )
            )
        return tuple(tasks)


StageCallback = Callable[[StageTask, Mapping[str, str]], StageArtifact]


class CallableStageProvider:
    """Delegate stage work to injected deterministic services.

    The provider only checks hashes and recovery fencing contracts.  PIT,
    Evidence, promotion, portfolio, ledger, and Workbench logic remain in their
    existing authorities.
    """

    def __init__(
        self,
        callbacks: Mapping[str, StageCallback],
        *,
        interrupt_once_before: str | None = None,
    ) -> None:
        self._callbacks = dict(callbacks)
        self._interrupt_once_before = interrupt_once_before
        self._interrupted = False
        self.call_counts: dict[str, int] = {}

    def execute(self, task: StageTask, *, committed_inputs: Mapping[str, str]) -> StageExecutionResult:
        stage_id = task.identity.stage_id
        callback = self._callbacks.get(stage_id)
        if callback is None:
            from veriquant.run_recovery.api import ProviderUnavailable

            raise ProviderUnavailable(FailureCode.ADAPTER_UNAVAILABLE, f"stage callback unavailable: {stage_id}")
        if stage_id == self._interrupt_once_before and not self._interrupted:
            self._interrupted = True
            raise RecoveryInterrupted(FailureCode.EVOLUTION_CANCELLED, "forced integration interruption")
        self.call_counts[stage_id] = self.call_counts.get(stage_id, 0) + 1
        artifact = callback(task, MappingProxyType(dict(committed_inputs)))
        if artifact.artifact_hash != task.identity.expected_artifact_hash:
            from veriquant.run_recovery.api import RecoveryError

            raise RecoveryError(FailureCode.INVALID_HASH, "stage artifact does not match declared identity")
        return StageExecutionResult(
            stage_identity_hash=task.identity.artifact_hash,
            output_artifact_hash=artifact.artifact_hash,
            payload_hash=artifact.payload_hash,
            metadata=artifact.metadata,
        )


def stage_hash(label: str, payload: object) -> str:
    """Convenience identity helper for deterministic integration fixtures."""

    return canonical_content_hash({"stage": label, "payload": payload})

