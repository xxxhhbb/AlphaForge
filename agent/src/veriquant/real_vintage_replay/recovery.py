"""Recovery StageProvider for one durable real-vintage replay stage."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from veriquant.contracts import FailureCode
from veriquant.data import canonical_content_hash
from veriquant.run_recovery.api import RecoveryError, StageExecutionResult, StageTask
from veriquant.shadow_replay.api import ReplayStatus, ShadowReplayPolicy

from .controller import RealVintageReplayController


class RealVintageReplayStageProvider:
    """Delegate replay execution and expose only hashes to RunRecoveryController."""

    def __init__(self, controller: RealVintageReplayController, policy: ShadowReplayPolicy) -> None:
        self._controller = controller
        self._policy = policy
        self.call_count = 0

    def execute(
        self,
        task: StageTask,
        *,
        committed_inputs: Mapping[str, str],
    ) -> StageExecutionResult:
        self.call_count += 1
        report = self._controller.run(self._policy)
        if report.manifest.status is not ReplayStatus.READY:
            last = report.daily_records[-1]
            code = FailureCode(last.failure_code) if last.failure_code else FailureCode.EXECUTION_HALTED
            raise RecoveryError(code, last.failure_message or "real-vintage replay halted")
        if report.manifest.artifact_hash != task.identity.expected_artifact_hash:
            raise RecoveryError(FailureCode.INVALID_HASH, "replay manifest does not match recovery stage identity")
        inputs = MappingProxyType(dict(committed_inputs))
        return StageExecutionResult(
            stage_identity_hash=task.identity.artifact_hash,
            output_artifact_hash=report.manifest.artifact_hash,
            payload_hash=canonical_content_hash(
                {
                    "manifest_hash": report.manifest.artifact_hash,
                    "daily_record_hashes": report.manifest.daily_record_hashes,
                    "committed_inputs": inputs,
                }
            ),
            metadata={
                "replay_id": self._policy.replay_id,
                "records": str(len(report.daily_records)),
            },
        )

