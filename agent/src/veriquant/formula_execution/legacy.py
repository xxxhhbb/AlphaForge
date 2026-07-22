"""Compatibility backend for existing restricted FactorDAG artifacts."""

from __future__ import annotations

from ..contracts import FailureCode
from ..contracts.base import fail
from ..data import MarketSnapshot, canonical_content_hash
from ..factors import CompiledFactorDAG, execute_factor_dag
from .models import (
    FactorScoreArtifact,
    FactorScorePoint,
    FormulaExecutionRequest,
    FormulaProgramKind,
)


LEGACY_RUNTIME_HASH = canonical_content_hash(
    {"backend": "LegacyFactorDAGBackend", "schema": "veriquant.factor-dag/v1"}
)
LEGACY_POLICY_HASH = canonical_content_hash(
    {"authority": "read-only-compatibility-and-rollback", "generated_python": False}
)


class LegacyFactorDAGBackend:
    kind = FormulaProgramKind.LEGACY_FACTOR_DAG

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: CompiledFactorDAG,
        snapshot: MarketSnapshot,
    ) -> FactorScoreArtifact:
        if not isinstance(program, CompiledFactorDAG):
            fail(FailureCode.INVALID_FACTOR_DAG, "legacy backend requires CompiledFactorDAG")
        if program.artifact_hash != request.program.program_hash:
            fail(FailureCode.INVALID_HASH, "legacy FactorDAG hash does not match program identity")
        if request.program.runtime_hash != LEGACY_RUNTIME_HASH:
            fail(FailureCode.INVALID_HASH, "legacy runtime hash mismatch")
        if request.program.policy_hash != LEGACY_POLICY_HASH:
            fail(FailureCode.INVALID_HASH, "legacy policy hash mismatch")
        result = execute_factor_dag(
            program,
            snapshot,
            seed=request.seed,
            config={"backend": "LegacyFactorDAGBackend"},
        )
        values = tuple(
            FactorScorePoint(
                event_time=value.event_time,
                symbol=value.symbol,
                score=value.value,
            )
            for value in sorted(result.values, key=lambda item: (item.event_time, item.symbol))
        )
        return FactorScoreArtifact(
            program_hash=request.program.program_hash,
            snapshot_hash=request.snapshot_hash,
            universe_hash=request.universe_hash,
            runtime_hash=request.program.runtime_hash,
            policy_hash=request.program.policy_hash,
            seed=request.seed,
            values=values,
        )
